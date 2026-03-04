"""SQL formatter (EBH style) – v0.6.0

What it formats (current scope):

A) Separators
- Normalize dashed separators to exactly: -------------------------------------------------------------------------------

B) Comments
- Preserve block comments (/* ... */) unchanged.
- Preserve line comments (-- ...) unchanged.
- Do NOT split line comments that contain block markers (e.g. --/*----).
- If a real block comment (/* or /**) is glued to code on the same line, move it to a new line.

C) CREATE TABLE
- Comma on the left.
- Align column names.
- Align type column.
- Align constraint column (NOT NULL / NULL / IDENTITY / etc.).
- Keep trailing inline line comments (-- ...) on the same line.
- Drop empty inline comments like just "--".
- Keep comment-only lines inside CREATE TABLE exactly.
- CONSTRAINT lines are kept as one line (no wrapping/breaking).

D) BEGIN..END indentation
- Indent content inside BEGIN..END blocks by 9 spaces per nesting level.
- Preserves existing indentation inside the block (prefixes, does not replace).
- END decreases indent before output.

E) Derived subqueries (FROM ( SELECT ... ) alias)
- Detect derived subqueries in FROM/APPLY parentheses.
- Reformat inner SELECT with:
  * SELECT list: comma on the left, supports 'alias = expr' alignment
  * FROM/JOIN/ON/WHERE with basic indentation
  * AND/OR aligned under WHERE or ON
- Keeps line comments inside the derived query.

Dependency-free.
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


def format_sql(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text)

    text = _normalize_separators(text)

    # Protect comments
    text, blocks = _protect_block_comments(text)
    text, line_comments = _protect_line_comments(text)

    # Structural formatters
    text = _format_derived_subqueries(text)
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


# ----------------
# separators
# ----------------


def _normalize_separators(text: str) -> str:
    text = re.sub(r"\n\s*-{10,}\s*\n", f"\n{_SEP}\n", text)
    # force separator to its own line
    text = re.sub(rf"\s*{re.escape(_SEP)}\s*", f"\n{_SEP}\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# ----------------
# comment protection
# ----------------

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


def _protect_line_comments(text: str) -> tuple[str, list[str]]:
    """Replace --... comments with placeholders, preserving content."""
    comments: list[str] = []
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
                comments.append(line[i:])
                line = line[:i] + f"__LC{len(comments)-1}__"
                break
            i += 1
        out_lines.append(line)

    return "\n".join(out_lines), comments


def _restore_line_comments(text: str, comments: list[str]) -> str:
    for i, c in enumerate(comments):
        text = text.replace(f"__LC{i}__", c)
    return text


def _ensure_block_comments_on_own_line(text: str) -> str:
    """Split real block comments from code, but never touch line comments like --/*----."""
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


# ----------------
# BEGIN..END indentation
# ----------------


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

        # keep separator at column 0 (even in BEGIN blocks)
        if stripped == _SEP:
            out.append(_SEP)
            continue

        # decrease before END
        if end_re.match(stripped):
            level = max(level - 1, 0)

        out.append(indent_unit * level + raw)

        if begin_re.match(stripped):
            level += 1

    return "\n".join(out)


# ----------------
# CREATE TABLE formatting (same as earlier)
# ----------------

_CREATE_START_RE = re.compile(r"^\s*CREATE\s+TABLE\b", re.I)
_NAME_RE = re.compile(r"^(\[[^\]]+\]|[A-Za-z_#@][\w#@\.]*)\s+(.*)$")
_CONSTRAINT_START_RE = re.compile(r"^(CONSTRAINT\b|PRIMARY\s+KEY\b|UNIQUE\b|FOREIGN\s+KEY\b)", re.I)

_CONSTR_TOKENS = [
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


def _norm_type_parens(s: str) -> str:
    s = re.sub(r"\b(VARCHAR|NVARCHAR|CHAR|NCHAR)\s*\(\s*(\d+)\s*\)", r"\1 (\2)", s, flags=re.I)
    s = re.sub(r"\b(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", r"\1 (\2,\3)", s, flags=re.I)
    return s


def _find_matching_paren(s: str, start: int) -> int | None:
    depth = 1
    in_str = False
    i = start + 1
    while i < len(s) and depth > 0:
        ch = s[i]
        if ch == "'":
            if in_str and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
            i += 1
            continue
        if not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _split_type_and_constraints(rest: str) -> tuple[str, str]:
    up = rest.upper()
    idxs = []
    for tok in _CONSTR_TOKENS:
        m = re.search(rf"\b{re.escape(tok)}\b", up)
        if m:
            idxs.append(m.start())
    if not idxs:
        return rest.strip(), ""
    cut = min(idxs)
    return rest[:cut].rstrip(), rest[cut:].strip()


def _split_top_level_commas_keep_comments(s: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(s):
        if not in_str and s.startswith("__BC", i):
            m = re.match(r"__BC\d+__", s[i:])
            if m:
                tok = m.group(0)
                if "".join(cur).strip():
                    parts.append("".join(cur).strip())
                cur = []
                parts.append(tok)
                i += len(tok)
                continue
        if not in_str and s.startswith("__LC", i):
            m = re.match(r"__LC\d+__", s[i:])
            if m:
                tok = m.group(0)
                if "".join(cur).strip():
                    parts.append("".join(cur).strip())
                cur = []
                parts.append(tok)
                i += len(tok)
                continue
        ch = s[i]
        if ch == "'":
            cur.append(ch)
            if in_str and i + 1 < len(s) and s[i + 1] == "'":
                cur.append("'")
                i += 2
                continue
            in_str = not in_str
            i += 1
            continue
        if not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(depth - 1, 0)
            elif ch == "," and depth == 0:
                if "".join(cur).strip():
                    parts.append("".join(cur).strip())
                cur = []
                i += 1
                continue
        cur.append(ch)
        i += 1

    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def _align_all_create_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _CREATE_START_RE.match(line):
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


def _split_inline_line_comment_placeholder(s: str) -> tuple[str, str | None]:
    # Here line comments already replaced by __LCn__ placeholders, so just detect them at end.
    m = re.search(r"(__LC\d+__)\s*$", s)
    if not m:
        return s.rstrip(), None
    return s[: m.start(1)].rstrip(), m.group(1)


def _format_create_table_stmt(stmt_lines: list[str]) -> list[str]:
    head = re.sub(r"\s{2,}", " ", stmt_lines[0].strip())

    flat = "\n".join(stmt_lines)
    p = flat.find("(")
    tail = ""
    if p != -1:
        end = _find_matching_paren(flat, p)
        if end is not None:
            tail = flat[end + 1 :].strip()

    items: list[tuple[str, str]] = []
    buf: list[str] = []

    started = False
    depth = 0
    in_str = False

    def flush_buf():
        nonlocal buf
        blob = " ".join(buf).strip()
        if blob:
            for it in _split_top_level_commas_keep_comments(blob):
                items.append(("ITEM", it))
        buf = []

    for raw in stmt_lines[1:]:
        line = raw.rstrip("\n")

        if not started:
            if "(" in line:
                started = True
                after = line.split("(", 1)[1]
                depth = 1
                if after.strip():
                    buf.append(after.strip())
            continue

        stripped = line.strip()
        if stripped.startswith("__LC"):
            # comment-only line
            flush_buf()
            items.append(("COMMENTLINE", line.rstrip()))
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
                if ch == "(":
                    depth += 1
                elif ch == ")":
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
        if kind != "ITEM":
            continue
        v = val.strip()
        if re.fullmatch(r"__BC\d+__", v) or re.fullmatch(r"__LC\d+__", v):
            continue
        if _CONSTRAINT_START_RE.match(v):
            continue
        core, lc = _split_inline_line_comment_placeholder(v)
        core = _norm_type_parens(re.sub(r"\s{2,}", " ", core)).strip()
        m = _NAME_RE.match(core)
        if not m:
            continue
        name = m.group(1)
        rest = m.group(2).strip()
        type_part, _ = _split_type_and_constraints(rest)
        type_part = _norm_type_parens(type_part)
        max_name = max(max_name, len(name))
        max_type = max(max_type, len(type_part))

    comma_ind = 12
    name_ind = 14

    out: list[str] = [head, "     ("]

    first = True

    def render(prefix: str, name: str, type_part: str, constr_part: str, lc: str | None) -> str:
        name_pad = " " * (max_name - len(name) + 1)
        if constr_part:
            type_pad = " " * (max_type - len(type_part) + 1)
            base = f"{name}{name_pad}{type_part}{type_pad}{constr_part}".rstrip()
        else:
            base = f"{name}{name_pad}{type_part}".rstrip()
        if lc:
            # drop empty comment placeholders is handled earlier
            return (prefix + base + " " + lc).rstrip()
        return (prefix + base).rstrip()

    for kind, val in items:
        if kind == "COMMENTLINE":
            out.append(val.rstrip())
            continue

        v = val.strip()
        if not v:
            continue

        if re.fullmatch(r"__BC\d+__", v) or re.fullmatch(r"__LC\d+__", v):
            out.append(" " * name_ind + v)
            continue

        if _CONSTRAINT_START_RE.match(v):
            v2 = _norm_type_parens(re.sub(r"\s{2,}", " ", v)).strip()
            prefix = " " * name_ind if first else (" " * comma_ind + ", ")
            out.append((prefix + v2).rstrip())
            first = False
            continue

        core, lc = _split_inline_line_comment_placeholder(v)
        # drop empty '--' comment placeholder: restored later to '--'
        if lc is not None and lc.strip() == "__LC" + lc[4:-2] + "__":
            # can't inspect actual yet; keep
            pass

        core = _norm_type_parens(re.sub(r"\s{2,}", " ", core)).strip()
        m = _NAME_RE.match(core)
        prefix = " " * name_ind if first else (" " * comma_ind + ", ")

        if not m:
            out.append((prefix + core + (" " + lc if lc else "")).rstrip())
            first = False
            continue

        name = m.group(1)
        rest = m.group(2).strip()
        type_part, constr_part = _split_type_and_constraints(rest)
        type_part = _norm_type_parens(type_part)

        out.append(render(prefix, name, type_part, constr_part, lc))
        first = False

    close = "     )" + (" " + tail if tail else "")
    out.append(close.rstrip())
    return out


# ----------------
# Derived subquery formatting
# ----------------

_DERIVED_OPEN_RE = re.compile(r"\(\s*SELECT\b", re.I)


def _format_derived_subqueries(text: str) -> str:
    """Format derived subqueries like FROM ( SELECT ... ) x.

    Works on comment-protected text (block comments -> __BCn__, line comments -> __LCn__).
    """

    i = 0
    out = []
    n = len(text)

    def is_word_char(ch: str) -> bool:
        return ch.isalnum() or ch == '_'

    while i < n:
        m = _DERIVED_OPEN_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break

        open_pos = m.start()
        # Look backwards to see if this '(' is part of FROM/APPLY
        back = text[max(0, open_pos-50):open_pos].upper()
        if not ("FROM" in back or "APPLY" in back):
            out.append(text[i:m.end()])
            i = m.end()
            continue

        out.append(text[i:open_pos])

        # Find matching closing paren for this derived subquery
        close_pos = _find_matching_paren(text, open_pos)
        if close_pos is None:
            out.append(text[open_pos:])
            break

        inner = text[open_pos+1:close_pos].strip()
        # Format inner select
        formatted_inner = _format_select_query(inner, base_indent=8)

        # Determine alias after close paren (e.g. ) x)
        j = close_pos + 1
        # consume whitespace
        while j < n and text[j].isspace():
            j += 1
        # capture alias token
        alias_start = j
        while j < n and (is_word_char(text[j]) or text[j] in '#[]'):
            j += 1
        alias = text[alias_start:j].strip()

        # Build block with consistent style
        block_lines = []
        block_lines.append('(')
        block_lines.extend(formatted_inner.split('\n'))
        block_lines.append(') ' + alias if alias else ')')

        out.append("\n".join(block_lines))
        i = j

    return "".join(out)


def _split_top_level_commas_expr(s: str) -> list[str]:
    parts=[]
    cur=[]
    depth=0
    in_str=False
    i=0
    while i<len(s):
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


def _tokenize_clauses(sql: str) -> list[tuple[str, str]]:
    """Very small clause tokenizer for SELECT statements at top level."""
    kws=[
        'FROM','WHERE','GROUP BY','ORDER BY',
        'LEFT OUTER JOIN','RIGHT OUTER JOIN','FULL OUTER JOIN',
        'LEFT JOIN','RIGHT JOIN','INNER JOIN','FULL JOIN','JOIN',
        'ON','AND','OR'
    ]
    kws_sorted=sorted(kws, key=len, reverse=True)

    depth=0
    in_str=False
    i=0
    cur=[]
    tokens=[]

    def flush_text():
        nonlocal cur
        t=''.join(cur)
        cur=[]
        return t

    def is_wc(c):
        return c.isalnum() or c=='_'

    while i < len(sql):
        # placeholders
        if not in_str and sql.startswith('__BC', i):
            m=re.match(r'__BC\d+__', sql[i:])
            if m:
                cur.append(m.group(0)); i+=len(m.group(0)); continue
        if not in_str and sql.startswith('__LC', i):
            m=re.match(r'__LC\d+__', sql[i:])
            if m:
                cur.append(m.group(0)); i+=len(m.group(0)); continue

        ch=sql[i]
        if ch=="'":
            cur.append(ch)
            if in_str and i+1<len(sql) and sql[i+1]=="'":
                cur.append("'"); i+=2; continue
            in_str=not in_str; i+=1; continue

        if not in_str:
            if ch=='(':
                depth+=1
            elif ch==')':
                depth=max(depth-1,0)
            if depth==0:
                matched=None
                for kw in kws_sorted:
                    if sql[i:].upper().startswith(kw) and (i==0 or not is_wc(sql[i-1])):
                        after=i+len(kw)
                        if after==len(sql) or not is_wc(sql[after]):
                            matched=kw
                            break
                if matched:
                    prev=flush_text().strip()
                    if prev:
                        tokens.append(('TEXT', prev))
                    tokens.append(('KW', matched))
                    i+=len(matched)
                    continue

        cur.append(ch)
        i+=1

    tail=flush_text().strip()
    if tail:
        tokens.append(('TEXT', tail))

    # Convert to (kw,arg) segments
    segs=[]
    cur_kw='SELECT'
    cur_arg=''
    for typ,val in tokens:
        if typ=='KW':
            segs.append((cur_kw, cur_arg.strip()))
            cur_kw=val.upper()
            cur_arg=''
        else:
            cur_arg += ' ' + val
    segs.append((cur_kw, cur_arg.strip()))

    # First segment is SELECT body (already)
    return segs


def _format_select_list(select_body: str, base_indent: int) -> list[str]:
    items=_split_top_level_commas_expr(select_body)
    # separate comments placeholders in order
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
    elif t=='RAW':
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


def _format_select_query(inner: str, base_indent: int = 8) -> str:
    """Format a single SELECT query (no UNION)."""

    # collapse whitespace but keep placeholders
    def norm_ws(s: str) -> str:
        s = re.sub(r"[\t\r\n]+", " ", s)
        s = re.sub(r"\s{2,}", " ", s)
        return s.strip()

    inner = norm_ws(inner)
    # Remove leading SELECT keyword for parsing
    if inner.upper().startswith('SELECT'):
        inner_sel = inner[6:].strip()
    else:
        inner_sel = inner

    # split at top-level FROM
    depth=0
    in_str=False
    pos_from=None
    for m in re.finditer(r"\bFROM\b", inner_sel, flags=re.I):
        p=m.start()
        # compute depth before p
        depth=0
        in_str=False
        for ch in inner_sel[:p]:
            if ch=="'":
                in_str=not in_str
            elif not in_str:
                if ch=='(':
                    depth+=1
                elif ch==')':
                    depth=max(depth-1,0)
        if depth==0 and not in_str:
            pos_from=p
            break

    if pos_from is None:
        select_list=inner_sel
        rest=''
    else:
        select_list=inner_sel[:pos_from].strip()
        rest=inner_sel[pos_from:].strip()

    out_lines=[]
    out_lines.extend(_format_select_list(select_list, base_indent))

    # Basic FROM/JOIN/WHERE formatting:
    if rest:
        segs=_tokenize_clauses(rest)
        # segs starts with SELECT part empty
        # We'll print FROM/JOIN/WHERE etc.
        from_pad='FROM     '
        where_pad='WHERE    '
        on_pad='ON       '

        join_indent=base_indent+len(from_pad)
        on_indent=join_indent-2
        where_indent=base_indent
        cond_indent=where_indent+len(where_pad)

        last_ctx=''
        for kw,arg in segs[1:]:
            if kw=='FROM':
                out_lines.append(' '*base_indent + from_pad + arg)
                last_ctx='FROM'
            elif 'JOIN' in kw and kw!='ON':
                out_lines.append(' '*join_indent + f"{kw} {arg}")
                last_ctx='JOIN'
            elif kw=='ON':
                out_lines.append(' '*on_indent + on_pad + arg)
                last_ctx='ON'
            elif kw=='WHERE':
                out_lines.append(' '*where_indent + where_pad + arg)
                last_ctx='WHERE'
            elif kw in ('AND','OR'):
                ind = cond_indent if last_ctx=='WHERE' else (on_indent+len(on_pad))
                out_lines.append(' '*ind + f"{kw} {arg}")
            elif kw in ('GROUP BY','ORDER BY'):
                out_lines.append(' '*base_indent + kw + ' ' + arg)
            else:
                out_lines.append(' '*base_indent + kw + ' ' + arg)

    return "\n".join(out_lines)
