"""SQL formatter (EBH style) – v0.5.0

Implemented rules:

1) Separators
- Normalize dashed separators to exactly:
  -------------------------------------------------------------------------------

2) Comments
- Preserve block comments (/* ... */) unchanged.
- Preserve line comments (-- ...) unchanged.
- Do NOT split line comments that contain block markers (e.g. --/*----).
- If a real block comment (/* or /**) is glued to code on the same line, move it to a new line.

3) CREATE TABLE
- Comma on the left.
- Align column names.
- Align type column.
- Align constraint column (NOT NULL / NULL / IDENTITY / etc.).
- Keep trailing inline line comments (-- ...) on the same line.
- Drop empty inline comments like just "--".
- Keep comment-only lines inside CREATE TABLE exactly (e.g. "--            , ...").
- CONSTRAINT lines are kept as one line (no wrapping/breaking).

4) BEGIN..END indentation
- Indent content inside BEGIN..END blocks by 9 spaces per nesting level.
- Preserves existing indentation inside the block (prefixes, does not replace).
- END decreases indent before output.

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

    text, blocks = _protect_block_comments(text)

    text = _align_all_create_tables(text)

    text = _restore_block_comments(text, blocks)

    text = _ensure_block_comments_on_own_line(text)

    text = _indent_begin_end_blocks(text)

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


def _indent_begin_end_blocks(text: str, indent_unit: str = "         ") -> str:
    """Indent content between BEGIN..END blocks (nesting aware).

    This prefixes the ORIGINAL line (including its own leading spaces) by
    `indent_unit * level`.
    """

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


# -------- CREATE TABLE formatting --------

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


def _split_inline_line_comment(s: str) -> tuple[str, str | None]:
    in_str = False
    i = 0
    while i < len(s) - 1:
        ch = s[i]
        if ch == "'":
            if in_str and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
            i += 1
            continue
        if not in_str and s[i:i+2] == "--":
            return s[:i].rstrip(), s[i:].rstrip()
        i += 1
    return s.rstrip(), None


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
        if stripped.startswith("--"):
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
        if re.fullmatch(r"__BC\d+__", v):
            continue
        if _CONSTRAINT_START_RE.match(v):
            continue
        core, cmt = _split_inline_line_comment(v)
        if cmt is not None and cmt.strip() == "--":
            cmt = None
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

    def render(prefix: str, name: str, type_part: str, constr_part: str, cmt: str | None) -> str:
        name_pad = " " * (max_name - len(name) + 1)
        if constr_part:
            type_pad = " " * (max_type - len(type_part) + 1)
            base = f"{name}{name_pad}{type_part}{type_pad}{constr_part}".rstrip()
        else:
            base = f"{name}{name_pad}{type_part}".rstrip()
        if cmt:
            return (prefix + base + " " + cmt).rstrip()
        return (prefix + base).rstrip()

    for kind, val in items:
        if kind == "COMMENTLINE":
            out.append(val.rstrip())
            continue

        v = val.strip()
        if not v:
            continue

        if re.fullmatch(r"__BC\d+__", v):
            out.append(" " * name_ind + v)
            continue

        if _CONSTRAINT_START_RE.match(v):
            v2 = _norm_type_parens(re.sub(r"\s{2,}", " ", v)).strip()
            prefix = " " * name_ind if first else (" " * comma_ind + ", ")
            out.append((prefix + v2).rstrip())
            first = False
            continue

        core, cmt = _split_inline_line_comment(v)
        if cmt is not None and cmt.strip() == "--":
            cmt = None

        core = _norm_type_parens(re.sub(r"\s{2,}", " ", core)).strip()
        m = _NAME_RE.match(core)
        prefix = " " * name_ind if first else (" " * comma_ind + ", ")

        if not m:
            out.append((prefix + core + (" " + cmt if cmt else "")).rstrip())
            first = False
            continue

        name = m.group(1)
        rest = m.group(2).strip()
        type_part, constr_part = _split_type_and_constraints(rest)
        type_part = _norm_type_parens(type_part)

        out.append(render(prefix, name, type_part, constr_part, cmt))
        first = False

    close = "     )" + (" " + tail if tail else "")
    out.append(close.rstrip())
    return out
