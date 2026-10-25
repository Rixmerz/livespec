"""CREATE TABLE extraction for `.sql` files, in pure Python.

SQL has no tree-sitter grammar in the pack livespec relies on, and a schema
reader must not depend on a download, so this is a small hand-written lexer:
it understands strings, quoted identifiers, `--` and `/* */` comments and
`$tag$` bodies well enough to split statements and the column list of a
`CREATE TABLE`. Everything that is not a plain CREATE TABLE (virtual tables,
indexes, triggers, ALTER TABLE, PRAGMA, ...) is skipped, and so are the
table-level constraints inside one.
"""

from __future__ import annotations

import bisect
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass

from livespec_mcp.domain.extractors import (
    ExtractedField,
    ExtractedSchema,
    ExtractedSymbol,
    ExtractResult,
    make_schema_field,
)
from livespec_mcp.domain.schema_types import sql_type

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<lcomment>--[^\n]*)
    | (?P<bcomment>/\*.*?(?:\*/|\Z))
    | (?P<string>'(?:[^'\\]|\\.|'')*'|'(?:[^']|'')*(?:'|\Z))
    | (?P<qident>"(?:[^"]|"")*(?:"|\Z)|`(?:[^`]|``)*(?:`|\Z))
    | (?P<dollar>\$(?:[A-Za-z_]\w*)?\$)
    | (?P<word>[A-Za-z_][\w$]*)
    | (?P<number>\d+(?:\.\d*)?|\.\d+)
    | (?P<punct>.)
    """,
    re.S | re.X,
)

# Words between CREATE and TABLE that still make it a plain table.
_TABLE_PREFIXES = frozenset({"OR", "REPLACE", "TEMP", "TEMPORARY", "UNLOGGED", "GLOBAL", "LOCAL"})
# Statements whose body holds `;` between BEGIN and END.
_BLOCK_OBJECTS = frozenset({"TRIGGER", "FUNCTION", "PROCEDURE"})
_TABLE_CONSTRAINTS = frozenset({"CONSTRAINT", "PRIMARY", "FOREIGN", "CHECK", "UNIQUE", "EXCLUDE"})
_INDEX_ELEMENTS = frozenset({"KEY", "INDEX", "FULLTEXT", "SPATIAL"})
# A column's type ends at the first of these (outside parentheses).
_TYPE_STOP = frozenset(
    {
        "CONSTRAINT",
        "NOT",
        "NULL",
        "PRIMARY",
        "UNIQUE",
        "CHECK",
        "DEFAULT",
        "REFERENCES",
        "GENERATED",
        "COLLATE",
        "AUTOINCREMENT",
        "AUTO_INCREMENT",
        "IDENTITY",
        "AS",
        "COMMENT",
        "ON",
    }
)
# Columns the database fills in itself (`AS (expr)` is SQLite/MySQL shorthand
# for GENERATED ALWAYS AS).
_GENERATED = frozenset({"GENERATED", "AUTOINCREMENT", "AUTO_INCREMENT", "IDENTITY", "AS"})


@dataclass(frozen=True)
class _Tok:
    kind: str  # word | qident | string | number | punct | lcomment | bcomment
    text: str
    start: int
    end: int

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""


def _tokenize(src: str) -> list[_Tok]:
    """@spec:schemas-schema-field-extraction

    Inside a `'...'` literal a backslash escapes the next character (the
    MySQL/MariaDB default, Postgres `E'...'`) and `''` is a quote. When that
    reading never closes the literal, the standard one (backslash as a plain
    character) is used, so `'C:\\'` still ends where standard SQL ends it.
    The cost: a standard literal ending in an odd backslash, followed later in
    the file by another quote, is read as running on to that quote.
    """
    toks: list[_Tok] = []
    pos, n = 0, len(src)
    while pos < n:
        m = _TOKEN_RE.match(src, pos)
        if m is None:  # unreachable: `punct` matches any character
            break
        kind, end = m.lastgroup or "punct", m.end()
        if kind == "dollar":
            # `$$ ... $$` / `$fn$ ... $fn$`: an opaque string body.
            close = src.find(m.group(), end)
            end = n if close < 0 else close + len(m.group())
            kind = "string"
        if kind != "ws":
            toks.append(_Tok(kind, src[pos:end], pos, end))
        pos = end
    return toks


def _split_statements(toks: list[_Tok]) -> list[list[_Tok]]:
    """Comment-free token lists, one per `;`-terminated statement."""
    stmts: list[list[_Tok]] = []
    cur: list[_Tok] = []
    depth = block = 0
    has_block = False
    for t in toks:
        if t.kind in ("lcomment", "bcomment"):
            continue
        cur.append(t)
        if t.kind == "word":
            up = t.upper
            if up in _BLOCK_OBJECTS and len(cur) <= 6 and cur[0].upper == "CREATE":
                has_block = True
            elif has_block and up in ("BEGIN", "CASE"):
                block += 1
            elif has_block and up == "END":
                block = max(0, block - 1)
        elif t.text == "(":
            depth += 1
        elif t.text == ")":
            depth = max(0, depth - 1)
        elif t.text == ";" and depth == 0 and block == 0:
            if len(cur) > 1:
                stmts.append(cur[:-1])
            cur, has_block = [], False
    if cur:
        stmts.append(cur)
    return stmts


def _unquote(t: _Tok) -> str:
    if t.kind == "qident":
        q = t.text[0]
        body = t.text[1:-1] if len(t.text) > 1 and t.text.endswith(q) else t.text[1:]
        return body.replace(q + q, q)
    return t.text


def _is_name(t: _Tok) -> bool:
    return t.kind in ("word", "qident")


def _qualified_name_end(stmt: list[_Tok], i: int) -> int:
    """Index after `a`, `a.b` or `"a"."b"` starting at `i`."""
    while i < len(stmt) and _is_name(stmt[i]):
        if i + 2 < len(stmt) and stmt[i + 1].text == "." and _is_name(stmt[i + 2]):
            i += 2
            continue
        return i + 1
    return i


def _split_top_level(toks: list[_Tok]) -> list[list[_Tok]]:
    parts: list[list[_Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif t.text == "," and depth == 0:
            parts.append([])
            continue
        parts[-1].append(t)
    return [p for p in parts if p]


def _depth0(toks: list[_Tok]) -> list[_Tok]:
    out: list[_Tok] = []
    depth = 0
    for t in toks:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif depth == 0:
            out.append(t)
    return out


def _paren_names(toks: list[_Tok], start: int) -> list[str]:
    """Names in the first parenthesised group at or after `start`."""
    depth = 0
    names: list[str] = []
    for t in toks[start:]:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            if depth == 1:
                return names
            depth -= 1
        elif depth == 1 and _is_name(t):
            names.append(_unquote(t))
    return names


def _followed_by(words: list[_Tok], first: str, second: str) -> bool:
    return any(a.upper == first and b.upper == second for a, b in itertools.pairwise(words))


def _is_index_element(el: list[_Tok]) -> bool:
    """MySQL `KEY idx (a)` / `INDEX (a)`, as opposed to a column named `key`
    (`key VARCHAR(10)`, whose parenthesis holds numbers, not names)."""
    if el[0].upper not in _INDEX_ELEMENTS or len(el) < 2:
        return False
    if el[1].text == "(":
        return True
    if len(el) > 2 and el[2].text == "(":
        return any(_is_name(t) for t in el[3:] if t.text != ")")
    return False


def _line_starts(src: str) -> list[int]:
    return [0, *(m.end() for m in re.finditer(r"\n", src))]


def _leading_comment(lines: list[str], start_line: int, col_prefix: str) -> str | None:
    """The contiguous `--` / `/* */` lines right above the statement, markers
    stripped. Nothing when code shares the statement's first line."""
    if col_prefix.strip():
        return None
    collected: list[str] = []  # bottom-up
    i = start_line - 2
    while i >= 0:
        s = lines[i].strip()
        if s.startswith("--"):
            collected.append(s.lstrip("-").strip())
            i -= 1
            continue
        if s.endswith("*/"):
            j = i
            while j >= 0 and "/*" not in lines[j]:
                j -= 1
            if j < 0 or not lines[j].strip().startswith("/*"):
                break
            block = "\n".join(lines[j : i + 1]).strip()[2:-2]
            body = [ln.strip().removeprefix("*").strip() for ln in block.splitlines()]
            collected.extend(reversed(body))
            i = j - 1
            continue
        break
    text = "\n".join(reversed(collected)).strip()
    return text or None


def _enum_type_names(stmts: list[list[_Tok]]) -> frozenset[str]:
    """Same-file `CREATE TYPE x AS ENUM (...)` names, casefolded."""
    names: set[str] = set()
    for st in stmts:
        if len(st) < 5 or st[0].upper != "CREATE" or st[1].upper != "TYPE":
            continue
        end = _qualified_name_end(st, 2)
        if end + 1 < len(st) and st[end].upper == "AS" and st[end + 1].upper == "ENUM":
            names.add(_unquote(st[end - 1]).casefold())
    return frozenset(names)


def _table_fields(
    body: list[_Tok], src: str, line_of: Callable[[int], int], enums: frozenset[str]
) -> list[ExtractedField]:
    columns: list[tuple[_Tok, list[_Tok], list[_Tok]]] = []  # name, type toks, rest
    table_pk: list[str] = []
    for el in _split_top_level(body):
        head = el[0]
        if head.upper in _TABLE_CONSTRAINTS:
            words = _depth0(el)
            for k, w in enumerate(words[:-1]):
                if w.upper == "PRIMARY" and words[k + 1].upper == "KEY":
                    table_pk = _paren_names(el, el.index(words[k + 1]))
                    break
            continue
        if not _is_name(head) or _is_index_element(el):
            continue
        type_toks: list[_Tok] = []
        depth = 0
        k = 1
        while k < len(el):
            t = el[k]
            if depth == 0 and t.upper in _TYPE_STOP:
                break
            depth += (t.text == "(") - (t.text == ")")
            type_toks.append(t)
            k += 1
        columns.append((head, type_toks, el[k:]))

    fields: list[ExtractedField] = []
    seen: set[str] = set()
    for head, type_toks, rest in columns:
        name = _unquote(head)
        if name in seen:
            continue
        seen.add(name)
        words = _depth0(rest)
        raw = (
            re.sub(r"\s+", " ", src[type_toks[0].start : type_toks[-1].end]) if type_toks else None
        )
        in_table_pk = name in table_pk
        mapped = sql_type(
            raw,
            enums,
            # A composite primary key makes its columns NOT NULL without
            # making any one of them a rowid alias.
            not_null=_followed_by(words, "NOT", "NULL") or (in_table_pk and len(table_pk) > 1),
            primary_key=_followed_by(words, "PRIMARY", "KEY")
            or (in_table_pk and len(table_pk) == 1),
            has_default=any(w.upper == "DEFAULT" for w in words),
            generated=any(w.upper in _GENERATED for w in words),
        )
        fields.append(make_schema_field(name, mapped, raw, line_of(head.start)))
    return fields


def extract_sql_schemas(source: str, module_name: str) -> ExtractResult:
    """@spec:schemas-schema-field-extraction

    One `table` symbol and one `sql_table` schema per plain CREATE TABLE.
    Never loads a tree-sitter grammar. The body hash seed is the statement's
    tokens joined by single spaces, so reformatting and comment edits leave
    it unchanged.
    """
    out = ExtractResult()
    toks = _tokenize(source)
    stmts = _split_statements(toks)
    enums = _enum_type_names(stmts)
    starts = _line_starts(source)
    lines = source.splitlines()

    def line_of(pos: int) -> int:
        return bisect.bisect_right(starts, pos)

    for st in stmts:
        if st[0].upper != "CREATE":
            continue
        i = 1
        while i < len(st) and st[i].upper in _TABLE_PREFIXES:
            i += 1
        if i >= len(st) or st[i].upper != "TABLE":
            continue  # VIRTUAL TABLE, INDEX, TRIGGER, VIEW, TYPE, ...
        i += 1
        if [t.upper for t in st[i : i + 3]] == ["IF", "NOT", "EXISTS"]:
            i += 3
        end = _qualified_name_end(st, i)
        if end == i or end >= len(st) or st[end].text != "(":
            continue  # CREATE TABLE x AS SELECT ...
        table = _unquote(st[end - 1])
        depth = 0
        close = len(st)
        for k in range(end, len(st)):
            depth += (st[k].text == "(") - (st[k].text == ")")
            if depth == 0:
                close = k
                break
        qname = f"{module_name}.{table}" if module_name else table
        start_line = line_of(st[0].start)
        line_start = starts[start_line - 1]
        out.symbols.append(
            ExtractedSymbol(
                name=table,
                qualified_name=qname,
                kind="table",
                signature=source[st[0].start : st[-1].end].splitlines()[0][:300],
                docstring=_leading_comment(lines, start_line, source[line_start : st[0].start]),
                body_hash_seed=" ".join(t.text for t in st),
                start_line=start_line,
                end_line=line_of(st[-1].end - 1),
            )
        )
        out.schemas.append(
            ExtractedSchema(
                src_qname=qname,
                flavor="sql_table",
                fields=_table_fields(st[end + 1 : close], source, line_of, enums),
            )
        )
    return out
