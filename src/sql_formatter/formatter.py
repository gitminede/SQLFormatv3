"""SQL formatter (EBH style) – v0.7.3

Implements:
- CTE formatting (WITH ... AS ( ... ))
- Top-level SELECT formatting (depth 0)
- JOIN/ON/WHERE alignment (A-style: ON on separate line with pad)

Keeps:
- Separator normalization to -------------------------------------------------------------------------------
- Comment preservation (block and line). Line comments are protected by placeholders during formatting.
- BEGIN..END block indentation (+9 spaces per nesting), preserves existing indentation (prefix).
- CREATE TABLE alignment (name/type/constraint columns; constraint lines not wrapped).
- Derived subqueries in FROM/APPLY: FROM ( SELECT ... ) x, formatted inner SELECT.

Pragmatic formatter: targets common T-SQL patterns for SQL Server 2022.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_SEP = "-------------------------------------------------------------------------------"


@dataclass
class DecodeResult:
    text: str
    encoding: str


def decode_bytes_best_effort(data: bytes) -> DecodeResult:
    candidates = ["utf-8-sig", "utf-8", "cp1250", "iso-8859-2", "latin1"]

    def score(s: str) -> int:
        rep = s.count("\ufffd")
        hu = sum(s.count(ch) for ch in "áéíóöőúüűÁÉÍÓÖŐÚÜŰ")
        return -(rep * 1000) + hu

    best_s = None
    best_enc = candidates[0]
    best_score = -10**9
    for enc in candidates:
        try:
            s = data.decode(enc)
        except UnicodeDecodeError:
            continue
        sc = score(s)
        if sc > best_score:
            best_score = sc
            best_s = s
            best_enc = enc

    if best_s is None:
        best_s = data.decode("utf-8", errors="replace")
        best_enc = "utf-8"

    best_s = best_s.replace("\r\n", "\n").replace("\r", "\n")
    best_s = html.unescape(best_s)
    best_s = best_s.replace("\\>", ">").replace("\\<", "<")
    return DecodeResult(text=best_s, encoding=best_enc)


# =========================
# Public entry
# =========================

def format_sql(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text)

    text = _normalize_separators(text)

    # Protect comments
    text, blocks = _protect_block_comments(text)
    text, line_comments = _protect_line_comments(text)

    # Protect derived subqueries as placeholders (then we can format outer query safely)
    text, derived_blocks = _protect_derived_subqueries(text)

    # Format CTEs (their bodies will be formatted using the query formatter, which understands __DSn__ tokens)
    text = _format_with_ctes(text)

    # Format top-level SELECT statements (depth 0)
    text = _format_top_level_selects(text)

    # Format derived subqueries placeholders into multi-line blocks
    text = _restore_derived_subqueries(text, derived_blocks)

    # Format CREATE TABLE blocks
    text = _align_all_create_tables(text)

    # Restore comments
    text = _restore_line_comments(text, line_comments)
    text = _restore_block_comments(text, blocks)

    # Post-fixes
    text = _ensure_block_comments_on_own_line(text)
    text = _indent_begin_end_blocks(text)

    # keep '= CASE'
    text = re.sub(r"=\s+CASE\b", "= CASE", text, flags=re.I)

    text = _normalize_separators(text)

    return text.strip() + "\n"


# =========================
# Separators
# =========================

def _normalize_separators(text: str) -> str:
    text = re.sub(r"\n\s*-{10,}\s*\n", f"\n{_SEP}\n", text)
    text = re.sub(rf"\s*{re.escape(_SEP)}\s*", f"\n{_SEP}\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# =========================
# Comments protection
# =========================

_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)


def _protect_block_comments(text: str) -> tuple[str, list[str]]:
    blocks: list[str] = []

    def repl(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"__BC{len(blocks)-1}__"

    return _BLOCK_RE.sub(repl, text), blocks


def _restore_block_comments(text: str, blocks: list[str]) -> str:
    for i, c in enumerate(blocks):
        text = text.replace(f"__BC{i}__", c)
    return text


def _protect_line_comments(text: str) -> tuple[str, list[str | None]]:
    """Replace --... comments with placeholders.

    Returns the transformed text and a list of comment strings.
    If a comment is empty (just '--' or '--   '), store None and drop it.
    """

    comments: list[str | None] = []
    out_lines: list[str] = []

    for line in text.split("\n"):
        in_str = False
        i = 0
        while i < len(line) - 1:
            ch = line[i]
            if ch == "'":
                if in_str and i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                in_str = not in_str
                i += 1
                continue
            if not in_str and line[i:i+2] == "--":
                c = line[i:]
                if re.fullmatch(r"--\s*", c):
                    # drop empty line comment
                    comments.append(None)
                    line = line[:i].rstrip()
                else:
                    comments.append(c)
                    line = line[:i].rstrip() + f" __LC{len(comments)-1}__"
                break
            i += 1
        out_lines.append(line)

    return "\n".join(out_lines), comments


def _restore_line_comments(text: str, comments: list[str | None]) -> str:
    for i, c in enumerate(comments):
        token = f"__LC{i}__"
        if c is None:
            text = text.replace(token, "")
        else:
            text = text.replace(token, c)
    # clean trailing spaces
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text


def _ensure_block_comments_on_own_line(text: str) -> str:
    out_lines: list[str] = []
    for line in text.split("\n"):
        idx_line = line.find("--")
        idx_block = line.find("/*")
        if idx_block != -1 and (idx_line == -1 or idx_line > idx_block):
            m = re.search(r"(\S)\s*(/\*\*|/\*)", line)
            if m:
                pos = m.start(2)
                out_lines.append(line[:pos].rstrip())
                out_lines.append(line[pos:])
                continue
        out_lines.append(line)
    return "\n".join(out_lines)


# =========================
# BEGIN..END indentation
# =========================

def _indent_begin_end_blocks(text: str, indent_unit: str = "         ") -> str:
    begin_re = re.compile(r"^BEGIN\b", re.I)
    end_re = re.compile(r"^END\b", re.I)

    out: list[str] = []
    level = 0

    for line in text.split("\n"):
        raw = line.rstrip("\r")
        stripped = raw.lstrip()

        if stripped == "":
            out.append(raw)
            continue

        if stripped == _SEP:
            out.append(_SEP)
            continue

        if end_re.match(stripped):
            level = max(level - 1, 0)

        out.append(indent_unit * level + raw)

        if begin_re.match(stripped):
            level += 1

    return "\n".join(out)


# =========================
# Core helpers
# =========================

def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == '_'


def _find_matching_paren(s: str, start: int) -> int | None:
    depth = 1
    in_str = False
    i = start + 1
    while i < len(s) and depth > 0:
        # skip placeholders
        if not in_str and s.startswith('__LC', i):
            m = re.match(r'__LC\d+__', s[i:])
            if m:
                i += len(m.group(0))
                continue
        if not in_str and s.startswith('__BC', i):
            m = re.match(r'__BC\d+__', s[i:])
            if m:
                i += len(m.group(0))
                continue
        if not in_str and s.startswith('__DS', i):
            m = re.match(r'__DS\d+__', s[i:])
            if m:
                i += len(m.group(0))
                continue

        ch = s[i]
        if ch == "'":
            if in_str and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
            i += 1
            continue
        if not in_str:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _norm_ws(s: str) -> str:
    s = re.sub(r"[\t\r\n]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def _split_top_level_commas_expr(s: str) -> list[str]:
    parts=[]
    cur=[]
    depth=0
    in_str=False
    i=0
    while i<len(s):
        # placeholders
        if not in_str and s.startswith('__LC', i):
            m=re.match(r'__LC\d+__', s[i:])
            if m:
                tok=m.group(0)
                if ''.join(cur).strip():
                    parts.append(''.join(cur).strip())
                cur=[]
                parts.append(tok)
                i+=len(tok)
                continue
        if not in_str and s.startswith('__BC', i):
            m=re.match(r'__BC\d+__', s[i:])
            if m:
                tok=m.group(0)
                if ''.join(cur).strip():
                    parts.append(''.join(cur).strip())
                cur=[]
                parts.append(tok)
                i+=len(tok)
                continue
        if not in_str and s.startswith('__DS', i):
            m=re.match(r'__DS\d+__', s[i:])
            if m:
                tok=m.group(0)
                cur.append(tok)
                i+=len(tok)
                continue

        ch=s[i]
        if ch=="'":
            cur.append(ch)
            if in_str and i+1<len(s) and s[i+1]=="'":
                cur.append("'"); i+=2; continue
            in_str=not in_str; i+=1; continue
        if not in_str:
            if ch=='(':
                depth+=1
            elif ch==')':
                depth=max(depth-1,0)
            elif ch==',' and depth==0:
                if ''.join(cur).strip():
                    parts.append(''.join(cur).strip())
                cur=[]
                i+=1
                continue
        cur.append(ch); i+=1
    if ''.join(cur).strip():
        parts.append(''.join(cur).strip())
    return [p for p in parts if p]


def _split_top_level_equals(s: str) -> tuple[str | None, str | None]:
    depth=0
    in_str=False
    for i,ch in enumerate(s):
        if ch=="'":
            if in_str and i+1<len(s) and s[i+1]=="'":
                continue
            in_str=not in_str
        if not in_str:
            if ch=='(':
                depth+=1
            elif ch==')':
                depth=max(depth-1,0)
            elif ch=='=' and depth==0:
                return s[:i].strip(), s[i+1:].strip()
    return None, None


def _norm_type_parens(s: str) -> str:
    s = re.sub(r"\b(VARCHAR|NVARCHAR|CHAR|NCHAR)\s*\(\s*(\d+)\s*\)", r"\1 (\2)", s, flags=re.I)
    s = re.sub(r"\b(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", r"\1 (\2,\3)", s, flags=re.I)
    return s


# =========================
# Derived subqueries protection/restoration
# =========================

_DERIVED_OPEN_RE = re.compile(r"\(\s*SELECT\b", re.I)


def _protect_derived_subqueries(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace derived subqueries with __DSn__ placeholders.

    Stores (alias, inner_query_text) in a list.
    """

    blocks: list[tuple[str, str]] = []
    i = 0
    out = []
    n = len(text)

    while i < n:
        m = _DERIVED_OPEN_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break

        open_pos = m.start()
        back = text[max(0, open_pos-120):open_pos].upper()
        if not ("FROM" in back or "APPLY" in back):
            out.append(text[i:m.end()])
            i = m.end()
            continue

        out.append(text[i:open_pos])
        close_pos = _find_matching_paren(text, open_pos)
        if close_pos is None:
            out.append(text[open_pos:])
            break

        # alias after close paren
        j = close_pos + 1
        while j < n and text[j].isspace():
            j += 1
        alias_start = j
        while j < n and (_is_word_char(text[j]) or text[j] in '#[]'):
            j += 1
        alias = text[alias_start:j].strip()

        inner = text[open_pos+1:close_pos].strip()
        blocks.append((alias, inner))
        out.append(f"__DS{len(blocks)-1}__")
        i = j

    return ''.join(out), blocks


def _restore_derived_subqueries(text: str, blocks: list[tuple[str, str]]) -> str:
    for i, (alias, inner) in enumerate(blocks):
        token = f"__DS{i}__"
        # format inner query
        inner_lines = _format_query_body(inner, base_indent=8)
        rep = "(\n" + "\n".join(inner_lines) + "\n)" + (" " + alias if alias else "")
        text = text.replace(token, rep)
    return text


# =========================
# Query formatter (SELECT + JOIN/ON/WHERE) A-style
# =========================

_CLAUSE_KWS = [
    'UNION ALL',
    'GROUP BY','ORDER BY',
    'LEFT OUTER JOIN','RIGHT OUTER JOIN','FULL OUTER JOIN',
    'LEFT JOIN','RIGHT JOIN','INNER JOIN','FULL JOIN','JOIN',
    'CROSS APPLY','OUTER APPLY',
    'FROM','WHERE','ON','AND','OR'
]
_CLAUSE_KWS_SORTED = sorted(_CLAUSE_KWS, key=len, reverse=True)


def _tokenize_top_level(sql: str, kws: list[str]) -> list[tuple[str,str]]:
    tokens=[]
    depth=0
    in_str=False
    i=0
    buf=[]

    def flush():
        nonlocal buf
        t=''.join(buf)
        buf=[]
        return t

    while i < len(sql):
        # placeholders
        if not in_str and sql.startswith('__LC', i):
            m=re.match(r'__LC\d+__', sql[i:])
            if m:
                buf.append(m.group(0)); i+=len(m.group(0)); continue
        if not in_str and sql.startswith('__BC', i):
            m=re.match(r'__BC\d+__', sql[i:])
            if m:
                buf.append(m.group(0)); i+=len(m.group(0)); continue
        if not in_str and sql.startswith('__DS', i):
            m=re.match(r'__DS\d+__', sql[i:])
            if m:
                buf.append(m.group(0)); i+=len(m.group(0)); continue

        ch=sql[i]
        if ch=="'":
            buf.append(ch)
            if in_str and i+1<len(sql) and sql[i+1]=="'":
                buf.append("'"); i+=2; continue
            in_str=not in_str; i+=1; continue

        if not in_str:
            if ch=='(':
                depth+=1
            elif ch==')':
                depth=max(depth-1,0)
            if depth==0:
                matched=None
                for kw in kws:
                    if sql[i:].upper().startswith(kw):
                        before_ok = (i==0) or (not _is_word_char(sql[i-1]))
                        after = i+len(kw)
                        after_ok = (after==len(sql)) or (not _is_word_char(sql[after]))
                        if before_ok and after_ok:
                            matched=kw
                            break
                if matched:
                    prev=flush().strip()
                    if prev:
                        tokens.append(('TEXT', prev))
                    tokens.append(('KW', matched))
                    i+=len(matched)
                    continue

        buf.append(ch)
        i+=1

    tail=flush().strip()
    if tail:
        tokens.append(('TEXT', tail))
    return tokens


def _split_union_all(body: str) -> list[str]:
    toks=_tokenize_top_level(body, ['UNION ALL'])
    parts=[]; cur=[]
    for typ,val in toks:
        if typ=='KW':
            if ''.join(cur).strip():
                parts.append(''.join(cur).strip())
            parts.append('UNION ALL')
            cur=[]
        else:
            cur.append(val)
    if ''.join(cur).strip():
        parts.append(''.join(cur).strip())
    return parts


def _split_select_from(stmt: str) -> tuple[str,str]:
    depth=0; in_str=False
    for m in re.finditer(r"\bFROM\b", stmt, flags=re.I):
        p=m.start()
        depth=0; in_str=False
        for ch in stmt[:p]:
            if ch=="'":
                in_str=not in_str
            elif not in_str:
                if ch=='(':
                    depth+=1
                elif ch==')':
                    depth=max(depth-1,0)
        if depth==0 and not in_str:
            return stmt[:p].strip(), stmt[p:].strip()
    return stmt.strip(), ''


def _format_select_list(select_part: str, base_indent: int) -> list[str]:
    items=_split_top_level_commas_expr(select_part)
    parsed=[]
    for it in items:
        if re.fullmatch(r'__LC\d+__', it):
            parsed.append(('COMMENT', it, None))
            continue
        lhs,rhs=_split_top_level_equals(it)
        if lhs is not None:
            parsed.append(('EQ', lhs, rhs))
        else:
            parsed.append(('RAW', it.strip(), None))

    max_lhs=max((len(lhs) for t,lhs,rhs in parsed if t=='EQ'), default=0)
    lines=[]
    first_real=next((i for i,(t,_,__) in enumerate(parsed) if t!='COMMENT'), None)
    if first_real is None:
        return [' '*base_indent + 'SELECT']

    t,lhs,rhs=parsed[first_real]
    if t=='EQ':
        lines.append(' '*base_indent + f"SELECT   {lhs}{' '*(max_lhs-len(lhs))}  = {rhs}")
    else:
        lines.append(' '*base_indent + f"SELECT   {lhs}")

    cont_indent=base_indent+7
    for idx,(t,lhs,rhs) in enumerate(parsed):
        if idx==first_real:
            continue
        if t=='COMMENT':
            lines.append(' '*cont_indent + lhs)
        elif t=='EQ':
            lines.append(' '*cont_indent + f", {lhs}{' '*(max_lhs-len(lhs))}  = {rhs}")
        else:
            lines.append(' '*cont_indent + f", {lhs}")

    return lines


def _format_clauses(rest: str, base_indent: int) -> list[str]:
    if not rest:
        return []

    rest = _norm_ws(rest)

    toks=_tokenize_top_level(rest, _CLAUSE_KWS_SORTED)
    segs=[]
    cur_kw=None
    cur_txt=''
    for typ,val in toks:
        if typ=='KW':
            if cur_kw is None:
                cur_kw=val
                cur_txt=''
            else:
                segs.append((cur_kw.upper(), cur_txt.strip()))
                cur_kw=val
                cur_txt=''
        else:
            cur_txt += ' ' + val
    if cur_kw is not None:
        segs.append((cur_kw.upper(), cur_txt.strip()))

    from_pad='FROM     '
    where_pad='WHERE    '
    on_pad='ON       '

    join_indent=base_indent+len(from_pad)
    on_indent=join_indent-2
    where_indent=base_indent
    cond_indent_where=where_indent+len(where_pad)
    cond_indent_on=on_indent+len(on_pad)

    out=[]
    ctx=''

    for kw,arg in segs:
        if kw=='FROM':
            out.append(' '*base_indent + from_pad + arg)
            ctx=''
        elif kw in ('LEFT OUTER JOIN','RIGHT OUTER JOIN','FULL OUTER JOIN','LEFT JOIN','RIGHT JOIN','INNER JOIN','FULL JOIN','JOIN','CROSS APPLY','OUTER APPLY'):
            out.append(' '*join_indent + f"{kw} {arg}".rstrip())
            ctx='JOIN'
        elif kw=='ON':
            out.append(' '*on_indent + on_pad + arg)
            ctx='ON'
        elif kw=='WHERE':
            out.append(' '*where_indent + where_pad + arg)
            ctx='WHERE'
        elif kw in ('AND','OR'):
            if ctx=='WHERE':
                out.append(' '*cond_indent_where + f"{kw} {arg}")
            elif ctx=='ON':
                out.append(' '*cond_indent_on + f"{kw} {arg}")
            else:
                out.append(' '*base_indent + f"{kw} {arg}")
        elif kw in ('GROUP BY','ORDER BY'):
            cols=_split_top_level_commas_expr(arg)
            if cols:
                out.append(' '*base_indent + f"{kw} {cols[0].strip()}")
                for c in cols[1:]:
                    out.append(' '*(base_indent+7) + ', ' + c.strip())
            else:
                out.append(' '*base_indent + kw)
        elif kw=='UNION ALL':
            out.append(' '*base_indent + 'UNION ALL')
            ctx=''
        else:
            out.append(' '*base_indent + kw + ' ' + arg)

    return out


def _format_select_statement(stmt: str, base_indent: int) -> list[str]:
    stmt = _norm_ws(stmt)
    if stmt.upper().startswith('SELECT'):
        stmt=stmt[6:].strip()
    select_part, rest=_split_select_from(stmt)
    lines=_format_select_list(select_part, base_indent)
    lines.extend(_format_clauses(rest, base_indent))
    return lines


def _format_query_body(body: str, base_indent: int) -> list[str]:
    body=_norm_ws(body)
    parts=_split_union_all(body)
    out=[]
    for part in parts:
        if part=='UNION ALL':
            out.append(' '*base_indent + 'UNION ALL')
        else:
            out.extend(_format_select_statement(part, base_indent))
    return out


# =========================
# CTE formatting
# =========================

_WITH_RE = re.compile(r"(^|\n)\s*WITH\b", re.I)


def _format_with_ctes(text: str) -> str:
    pos = 0
    out: list[str] = []

    while True:
        m = _WITH_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            break

        start = m.start(0)
        out.append(text[pos:start])

        with_end = m.end(0)
        work = text[with_end:]
        j = 0
        ctes: list[tuple[str, str]] = []

        def skip_ws(k: int) -> int:
            while k < len(work) and work[k].isspace():
                k += 1
            return k

        while True:
            j = skip_ws(j)
            if j < len(work) and work[j] == ',':
                j += 1
                j = skip_ws(j)

            nm = re.match(r"([A-Za-z_][\w#]*)", work[j:])
            if not nm:
                break
            name = nm.group(1)
            j += len(name)
            j = skip_ws(j)

            if not re.match(r"AS\b", work[j:], re.I):
                break
            j += 2
            j = skip_ws(j)
            if j >= len(work) or work[j] != '(':
                break

            body_start = j
            body_end = _find_matching_paren(work, body_start)
            if body_end is None:
                break
            body = work[body_start+1:body_end].strip()
            ctes.append((name, body))
            j = body_end + 1
            j = skip_ws(j)
            if j < len(work) and work[j] == ',':
                continue
            break

        if not ctes:
            out.append(text[start:with_end])
            pos = with_end
            continue

        lines: list[str] = []
        lines.append('WITH ' + ctes[0][0])
        lines.append('AS (')
        lines.extend(_format_query_body(ctes[0][1], base_indent=8))
        lines.append('   )')
        for name, body in ctes[1:]:
            lines.append('   , ' + name)
            lines.append('AS (')
            lines.extend(_format_query_body(body, base_indent=8))
            lines.append('   )')

        out.append("\n".join(lines))
        pos = with_end + j

    return ''.join(out)


# =========================
# Top-level SELECT formatting
# =========================

_TOP_SELECT_RE = re.compile(r"(^|\n)(\s*)SELECT\b", re.I)


def _format_top_level_selects(text: str) -> str:
    def depth_at(pos: int) -> int:
        depth=0
        in_str=False
        i=0
        while i < pos and i < len(text):
            if not in_str and text.startswith('__LC', i):
                m=re.match(r'__LC\d+__', text[i:])
                if m:
                    i += len(m.group(0));
                    continue
            if not in_str and text.startswith('__BC', i):
                m=re.match(r'__BC\d+__', text[i:])
                if m:
                    i += len(m.group(0));
                    continue
            if not in_str and text.startswith('__DS', i):
                m=re.match(r'__DS\d+__', text[i:])
                if m:
                    i += len(m.group(0));
                    continue
            ch=text[i]
            if ch=="'":
                in_str=not in_str
            elif not in_str:
                if ch=='(':
                    depth+=1
                elif ch==')':
                    depth=max(depth-1,0)
            i+=1
        return depth

    out=[]
    pos=0
    while True:
        m=_TOP_SELECT_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            break

        sel_pos = m.start(0) + (1 if m.group(1) == '\n' else 0)
        if depth_at(sel_pos) != 0:
            out.append(text[pos:m.end(0)])
            pos = m.end(0)
            continue

        out.append(text[pos:sel_pos])

        end = len(text)
        nxt = re.search(r"\n\s*(WITH\b|SELECT\b|INSERT\b|UPDATE\b|DELETE\b|CREATE\b|DROP\b|DECLARE\b|IF\b|BEGIN\b|END\b|RETURN\b|GO\b|" + re.escape(_SEP) + r")", text[m.end(0):], re.I)
        if nxt:
            end = m.end(0) + nxt.start()

        stmt = text[sel_pos:end].strip()
        fmt_lines = _format_query_body(stmt, base_indent=0)
        out.append('\n'.join(fmt_lines))

        pos = end

    return ''.join(out)


# =========================
# CREATE TABLE formatting
# =========================

_CTABLE_START_RE = re.compile(r"^\s*CREATE\s+TABLE\b", re.I)
_CTABLE_COL_RE = re.compile(r"^(\[[^\]]+\]|[A-Za-z_#@][\w#@\.]*)\s+(.*)$")
_CTABLE_CONSTRAINT_RE = re.compile(r"^(CONSTRAINT\b|PRIMARY\s+KEY\b|UNIQUE\b|FOREIGN\s+KEY\b)", re.I)

_CTABLE_CONSTR_TOKENS = [
    "NOT NULL",
    "NULL",
    "IDENTITY",
    "PRIMARY KEY",
    "DEFAULT",
    "COLLATE",
    "REFERENCES",
    "CHECK",
    "UNIQUE",
    "SPARSE",
    "ROWGUIDCOL",
]


def _split_type_and_constraints_ct(rest: str) -> tuple[str, str]:
    up = rest.upper()
    idxs = []
    for tok in _CTABLE_CONSTR_TOKENS:
        m = re.search(rf"\b{re.escape(tok)}\b", up)
        if m:
            idxs.append(m.start())
    if not idxs:
        return rest.strip(), ""
    cut = min(idxs)
    return rest[:cut].rstrip(), rest[cut:].strip()


def _split_inline_lc_placeholder(s: str) -> tuple[str, str | None]:
    m = re.search(r"(__LC\d+__)\s*$", s)
    if not m:
        return s.rstrip(), None
    return s[:m.start(1)].rstrip(), m.group(1)


def _align_all_create_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if _CTABLE_START_RE.match(line):
            stmt_lines = [line]
            i += 1
            started = False
            depth = 0
            in_str = False
            while i < len(lines):
                stmt_lines.append(lines[i])
                ln = lines[i]
                j = 0
                while j < len(ln):
                    ch = ln[j]
                    if ch == "'":
                        if in_str and j + 1 < len(ln) and ln[j + 1] == "'":
                            j += 2
                            continue
                        in_str = not in_str
                        j += 1
                        continue
                    if not in_str:
                        if ch == "(":
                            if not started:
                                started = True
                                depth = 1
                            else:
                                depth += 1
                        elif ch == ")" and started:
                            depth -= 1
                            if depth == 0:
                                break
                    j += 1
                i += 1
                if started and depth == 0:
                    break

            out.extend(_format_create_table_stmt(stmt_lines))
            continue

        out.append(line.rstrip())
        i += 1

    return "\n".join(out)


def _format_create_table_stmt(stmt_lines: list[str]) -> list[str]:
    # head may contain '('
    head_line = stmt_lines[0]
    if '(' in head_line:
        head_line = head_line.split('(', 1)[0]
    head = re.sub(r"\s{2,}", " ", head_line.strip())

    flat = "\n".join(stmt_lines)
    p = flat.find('(')
    tail = ''
    if p != -1:
        end = _find_matching_paren(flat, p)
        if end is not None:
            tail = flat[end + 1:].strip()

    items: list[tuple[str, str]] = []
    buf: list[str] = []

    started = False
    depth = 0
    in_str = False

    def flush_buf():
        nonlocal buf
        blob = " ".join(buf).strip()
        if blob:
            for it in _split_top_level_commas_expr(blob):
                items.append(("ITEM", it))
        buf = []

    # start scanning at first line containing '('
    for raw in stmt_lines:
        line = raw.rstrip("\n")
        if not started:
            if '(' in line:
                started = True
                after = line.split('(', 1)[1]
                depth = 1
                if after.strip():
                    buf.append(after.strip())
            continue

        stripped = line.strip()
        if re.fullmatch(r"__LC\d+__", stripped):
            flush_buf()
            items.append(("COMMENTLINE", stripped))
            continue

        j = 0
        out_chars = []
        while j < len(line):
            ch = line[j]
            if ch == "'":
                out_chars.append(ch)
                if in_str and j + 1 < len(line) and line[j + 1] == "'":
                    out_chars.append("'")
                    j += 2
                    continue
                in_str = not in_str
                j += 1
                continue
            if not in_str:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
            out_chars.append(ch)
            j += 1

        if "".join(out_chars).strip():
            buf.append("".join(out_chars).strip())

        if depth == 0:
            break

    flush_buf()

    max_name = 0
    max_type = 0

    for kind, val in items:
        if kind != 'ITEM':
            continue
        v = val.strip()
        if re.fullmatch(r"__BC\d+__", v) or re.fullmatch(r"__LC\d+__", v):
            continue
        if _CTABLE_CONSTRAINT_RE.match(v):
            continue

        core, lc = _split_inline_lc_placeholder(v)
        core = _norm_type_parens(re.sub(r"\s{2,}", " ", core)).strip()
        m = _CTABLE_COL_RE.match(core)
        if not m:
            continue
        name = m.group(1)
        rest = m.group(2).strip()
        type_part, _ = _split_type_and_constraints_ct(rest)
        type_part = _norm_type_parens(type_part)
        max_name = max(max_name, len(name))
        max_type = max(max_type, len(type_part))

    comma_ind = 12
    name_ind = 14

    out: list[str] = [head, '     (']
    first = True

    def render(prefix: str, name: str, type_part: str, constr_part: str, lc: str | None) -> str:
        name_pad = ' ' * (max_name - len(name) + 1)
        if constr_part:
            type_pad = ' ' * (max_type - len(type_part) + 1)
            base = f"{name}{name_pad}{type_part}{type_pad}{constr_part}".rstrip()
        else:
            base = f"{name}{name_pad}{type_part}".rstrip()
        if lc:
            return (prefix + base + ' ' + lc).rstrip()
        return (prefix + base).rstrip()

    for kind, val in items:
        if kind == 'COMMENTLINE':
            out.append(' ' * name_ind + val)
            continue

        v = val.strip()
        if not v:
            continue

        if re.fullmatch(r"__BC\d+__", v) or re.fullmatch(r"__LC\d+__", v):
            out.append(' ' * name_ind + v)
            continue

        if _CTABLE_CONSTRAINT_RE.match(v):
            v2 = _norm_type_parens(re.sub(r"\s{2,}", " ", v)).strip()
            prefix = ' ' * name_ind if first else (' ' * comma_ind + ', ')
            out.append((prefix + v2).rstrip())
            first = False
            continue

        core, lc = _split_inline_lc_placeholder(v)
        core = _norm_type_parens(re.sub(r"\s{2,}", " ", core)).strip()
        m = _CTABLE_COL_RE.match(core)
        prefix = ' ' * name_ind if first else (' ' * comma_ind + ', ')

        if not m:
            out.append((prefix + core + (' ' + lc if lc else '')).rstrip())
            first = False
            continue

        name = m.group(1)
        rest = m.group(2).strip()
        type_part, constr_part = _split_type_and_constraints_ct(rest)
        type_part = _norm_type_parens(type_part)

        out.append(render(prefix, name, type_part, constr_part, lc))
        first = False

    close = '     )' + (' ' + tail if tail else '')
    out.append(close.rstrip())
    return out
