"""SQL formatter tailored for internal EBH-style rules.

Goals:
- Preserve block comments (/* ... */) and line comments (-- ...).
- Normalize separator lines to exactly: -------------------------------------------------------------------------------
- Align CREATE TABLE column definitions:
    CREATE TABLE X
         (
                  colname                  TYPE ...
                , other_col                TYPE ...
         )
  * comma on the left
  * types like VARCHAR (50) keep exactly that (no spaces inside parentheses)

This module is intentionally dependency-free to make packaging easy.
You can extend it with additional rules later.
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
    """Best-effort decoding for HU encodings.

    Tries: utf-8-sig, utf-8, cp1250, iso-8859-2, latin1.
    Picks the best score by: fewest replacement chars + most Hungarian accented chars.
    """

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
        # fallback
        best_s = data.decode("utf-8", errors="replace")
        best_enc = "utf-8"

    best_s = best_s.replace("\r\n", "\n").replace("\r", "\n")
    best_s = html.unescape(best_s)
    # some dumps may contain escaped angle brackets
    best_s = best_s.replace("\\>", ">").replace("\\<", "<")
    return DecodeResult(text=best_s, encoding=best_enc)


def format_sql(text: str) -> str:
    """Main entry point."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text)

    # Normalize separators first (safe)
    text = _normalize_separators(text)

    # Preserve block comments while we reflow CREATE TABLE
    text, blocks = _protect_block_comments(text)

    # CREATE TABLE alignment (all CREATE TABLE)
    text = _align_all_create_tables(text)

    # Restore block comments
    text = _restore_block_comments(text, blocks)

    # Ensure comment blocks are not glued to previous tokens
    text = _ensure_block_comments_on_own_line(text)

    # Keep "= CASE" spacing
    text = re.sub(r"=\s+CASE\b", "= CASE", text, flags=re.I)

    # Normalize separators again (in case previous steps introduced spacing)
    text = _normalize_separators(text)

    # Make sure file ends with a newline
    text = text.strip() + "\n"
    return text


# --------------------------
# Internal helpers
# --------------------------


def _normalize_separators(text: str) -> str:
    # Replace any long dashed line variants with our canonical separator.
    text = re.sub(r"\n\s*-{10,}\s*\n", f"\n{_SEP}\n", text)
    # If separator is embedded in a line, force it onto its own line
    text = re.sub(rf"\s*{re.escape(_SEP)}\s*", f"\n{_SEP}\n", text)
    # Collapse too many blank lines
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
    # If a block comment begins after some non-space token on the same line, split.
    text = re.sub(r"(?m)(\S)\s*(/\*\*)", r"\1\n/**", text)
    text = re.sub(r"(?m)(\S)\s*(/\*)", r"\1\n/*", text)
    return text


_CREATE_START_RE = re.compile(r"^\s*CREATE\s+TABLE\b", re.I | re.M)


def _align_all_create_tables(text: str) -> str:
    """Align every CREATE TABLE block in the script."""

    lines = text.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if _CREATE_START_RE.match(line):
            stmt_lines = [line]
            i += 1
            # collect until we have a matching paren for the first '(' after CREATE TABLE
            while i < len(lines):
                flat = " ".join(l.strip() for l in stmt_lines if l.strip())
                p = flat.find("(")
                if p != -1 and _find_matching_paren(flat, p) is not None:
                    break
                stmt_lines.append(lines[i])
                i += 1

            out.extend(_format_create_table_stmt(stmt_lines))
            continue

        out.append(line.rstrip())
        i += 1

    return "\n".join(out)


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


_NAME_RE = re.compile(r"^(\[[^\]]+\]|[A-Za-z_#@][\w#@\.]*)\s+(.*)$")


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


def _norm_type_parens(coldef: str) -> str:
    # VARCHAR (50) / DECIMAL (10,2) with no spaces inside parentheses
    coldef = re.sub(r"\b(VARCHAR|NVARCHAR|CHAR|NCHAR)\s*\(\s*(\d+)\s*\)", r"\1 (\2)", coldef, flags=re.I)
    coldef = re.sub(r"\b(DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", r"\1 (\2,\3)", coldef, flags=re.I)
    return coldef


def _format_create_table_stmt(stmt_lines: list[str]) -> list[str]:
    # Join into one line for robust parsing
    flat = " ".join(ln.strip() for ln in stmt_lines if ln.strip())
    flat = re.sub(r"\s{2,}", " ", flat).strip()

    p = flat.find("(")
    if p == -1:
        return [flat]

    head = flat[:p].strip()
    end = _find_matching_paren(flat, p)
    if end is None:
        return [flat]

    body = flat[p + 1 : end].strip()
    tail = flat[end + 1 :].strip()  # e.g. ';'

    items = _split_top_level_commas_keep_comments(body)

    cols: list[tuple[str, str, str | None]] = []
    maxlen = 0
    for it in items:
        it = it.strip()
        if re.fullmatch(r"__BC\d+__", it):
            cols.append(("COMMENT", it, None))
            continue
        it = _norm_type_parens(it)
        m = _NAME_RE.match(it)
        if m:
            name = m.group(1)
            rest = m.group(2).strip()
        else:
            name = it
            rest = ""
        maxlen = max(maxlen, len(name))
        cols.append(("COL", name, rest))

    # Indentation per your sample
    comma_ind = 12
    name_ind = 14

    out = [head, "     ("]

    first = True
    for typ, name, rest in cols:
        if typ == "COMMENT":
            out.append(" " * name_ind + name)
            continue
        pad = " " * (maxlen - len(name) + 1)
        if first:
            out.append((" " * name_ind + f"{name}{pad}{rest}").rstrip())
            first = False
        else:
            out.append((" " * comma_ind + f", {name}{pad}{rest}").rstrip())

    close = "     )"
    if tail:
        close += " " + tail
    out.append(close)
    return out
