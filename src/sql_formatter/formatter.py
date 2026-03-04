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

    # Derived subqueries first (needs comment placeholders)
    text = _format_derived_subqueries(text)

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


def _normalize_separators(text: str) -> str:
    text = re.sub(r"\n\s*-{10,}\s*\n", f"\n{_SEP}\n", text)
    text = re.sub(rf"\s*{re.escape(_SEP)}\s*", f"\n{_SEP}\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


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


# -------- Derived subquery formatting (FROM ( SELECT ... ) alias) --------

_DERIVED_OPEN_RE = re.compile(r"\(\s*SELECT\b", re.I)

def _format_derived_subqueries(text: str) -> str:
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
        back = text[max(0, open_pos-50):open_pos].upper()
        if not ("FROM" in back or "APPLY" in back):
            out.append(text[i:m.end()])
            i = m.end()
            continue

        out.append(text[i:open_pos])

        close_pos = _find_matching_paren(text, open_pos)
        if close_pos is None:
            out.append(text[open_pos:])
            break

        inner = text[open_pos+1:close_pos].strip()
        formatted_inner = _format_select_query(inner, base_indent=8)

        j = close_pos + 1
        while j < n and text[j].isspace():
            j += 1
        alias_start = j
        while j < n and (is_word_char(text[j]) or text[j] in '#[]'):
            j += 1
        alias = text[alias_start:j].strip()

        out.append("(\n" + formatted_inner + "\n)" + (" " + alias if alias else ""))
        i = j

    return "".join(out)


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


def _split_top_level_commas_expr(s: str) -> list[str]:
    parts=[]
    cur=[]
    depth=0
    in_str=False
    i=0
    while i<len(s):
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


def _format_select_list(select_body: str, base_indent: int) -> list[str]:
    items=_split_top_level_commas_expr(select_body)
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


def _format_select_query(inner: str, base_indent: int = 8) -> str:
    def norm_ws(s: str) -> str:
        s = re.sub(r"[\t\r\n]+", " ", s)
        s = re.sub(r"\s{2,}", " ", s)
        return s.strip()

    inner = norm_ws(inner)
    if inner.upper().startswith('SELECT'):
        inner_sel = inner[6:].strip()
    else:
        inner_sel = inner

    # split at top-level FROM
    pos_from=None
    depth=0
    in_str=False
    for m in re.finditer(r"\bFROM\b", inner_sel, flags=re.I):
        p=m.start()
        depth=0; in_str=False
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

    if rest:
        # very simple: break keywords by spaces (already normalized)
        # keep FROM and WHERE on their own lines
        rest = rest.replace(' FROM ', ' FROM ')
        out_lines.append(' '*base_indent + 'FROM     ' + rest[4:].strip()) if rest.upper().startswith('FROM') else out_lines.append(' '*base_indent + rest)

    return "\n".join(out_lines)
