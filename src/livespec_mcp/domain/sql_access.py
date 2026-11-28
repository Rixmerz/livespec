"""Which tables does one SQL (or JPQL) string read, write or define?

Built on the lexer of ``sql_schema.py`` (strings, quoted identifiers, comments).
This is a shape recogniser, not a parser: it accepts a string only when it
starts with a statement keyword AND the statement has the shape of that
statement (``SELECT`` needs a ``FROM`` clause that ends the way a clause ends,
``UPDATE`` needs a ``SET``, ``INSERT`` needs ``INTO t`` followed by a column
list / ``VALUES`` / a query). Prose such as "Select a file from the list" or
"Update the record" yields nothing.

An interpolation (f-string ``{x}``, template ``${x}``, a concatenated
non-literal) is passed in as :data:`HOLE`. A hole in a table position gives no
table; anywhere else it is an opaque token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from livespec_mcp.domain.sql_schema import _Tok, _tokenize, _unquote

#: Stands for an interpolated expression inside a SQL string.
HOLE = "__lsq_hole__"

READ, WRITE, DDL = "read", "write", "ddl"

_HEAD_RE = re.compile(
    r"""\s*(?:\(\s*)*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*
        (SELECT|INSERT|UPDATE|DELETE|MERGE|UPSERT|REPLACE|WITH|CREATE|ALTER|DROP|TRUNCATE|FROM)\b""",
    re.I | re.S | re.X,
)

# Unquoted words that are never a table: clause keywords...
_RESERVED = frozenset(
    """SELECT FROM WHERE SET VALUES JOIN ON GROUP ORDER BY HAVING LIMIT UNION AS INTO
    LATERAL ONLY TABLE IF NOT EXISTS AND OR NULL CASE WHEN INNER LEFT RIGHT FULL CROSS
    NATURAL OUTER USING DUAL DEFAULT RETURNING OFFSET FETCH EXCEPT INTERSECT WINDOW WITH
    UNNEST DISTINCT ALL""".split()
)
# ...and the English words a sentence puts where a table would be.
_STOPWORDS = frozenset(
    """the a an this that these those your my our their its it here there each any some
    one which what you we they i to of in for and or is are be""".split()
)
# What may follow `FROM t [alias]` in a real statement (besides the end).
_AFTER_TABLE = frozenset(
    """WHERE JOIN INNER LEFT RIGHT FULL CROSS NATURAL GROUP ORDER LIMIT HAVING UNION EXCEPT
    INTERSECT MINUS OFFSET FETCH FOR WINDOW ON USING RETURNING SET TABLESAMPLE PIVOT WITH
    STRAIGHT_JOIN QUALIFY START CONNECT PARTITION AS""".split()
)
_AFTER_INSERT_TABLE = frozenset(
    "VALUES SELECT DEFAULT SET WITH AS OVERRIDING TABLE PARTITION".split()
)
_INSERT_FILLERS = frozenset(
    "OR IGNORE REPLACE ROLLBACK ABORT FAIL LOW_PRIORITY DELAYED HIGH_PRIORITY".split()
)
_UPDATE_FILLERS = frozenset("ONLY LOW_PRIORITY IGNORE".split())
# `f(x FROM y)` is not a FROM clause inside these.
_FROM_FUNCS = frozenset("EXTRACT SUBSTRING TRIM OVERLAY POSITION".split())
_CREATE_PREFIXES = frozenset("OR REPLACE TEMP TEMPORARY UNLOGGED GLOBAL LOCAL EXTERNAL".split())
_INTO_VERBS = frozenset("INSERT REPLACE UPSERT MERGE".split())


@dataclass(frozen=True)
class SqlTable:
    """One table a statement touches. `name` and `schema` keep the source
    spelling without quotes; `offset` is the name's position in the text."""

    access: str  # read | write | ddl
    schema: str | None
    name: str
    offset: int


def looks_like_sql(text: str) -> bool:
    """Cheap pre-filter: does the string start with a statement keyword?"""
    return _HEAD_RE.match(text) is not None


def sql_tables(text: str, *, jpql: bool = False) -> list[SqlTable]:
    """Tables of every statement in `text`, in source order, deduplicated per
    (access, schema, name). `jpql=True` reads the names as entity names: a
    leading ``FROM Entity e`` is a query and ``JOIN e.items`` is a path."""
    if not looks_like_sql(text):
        return []
    toks = [t for t in _tokenize(text) if t.kind not in ("lcomment", "bcomment")]
    out: list[SqlTable] = []
    seen: set[tuple[str, str | None, str]] = set()
    for stmt in _split(toks):
        for t in _statement(stmt, jpql):
            key = (t.access, (t.schema or "").lower() or None, t.name.lower())
            if key not in seen:
                seen.add(key)
                out.append(t)
    return out


def _split(toks: list[_Tok]) -> list[list[_Tok]]:
    stmts: list[list[_Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.kind == "punct":
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth = max(0, depth - 1)
            elif t.text == ";" and depth == 0:
                stmts.append([])
                continue
        stmts[-1].append(t)
    return [s for s in stmts if s]


def _is_hole(t: _Tok) -> bool:
    return t.kind == "word" and HOLE in t.text


def _part(toks: list[_Tok], p: int, jpql: bool = False) -> tuple[str | None, int] | None:
    """One name part at `p`: (text or None for a hole, next index). In JPQL a
    CamelCase word is an entity even when it spells a keyword (`Order`)."""
    if p >= len(toks):
        return None
    t = toks[p]
    if t.kind == "qident":
        return _unquote(t), p + 1
    if t.kind == "word":
        if _is_hole(t):
            return None, p + 1
        if t.upper in _RESERVED and not (jpql and _is_title(t.text)):
            return None
        return t.text, p + 1
    if t.text == "[":  # T-SQL [dbo].[Order Details]
        for q in range(p + 1, min(p + 8, len(toks))):
            if toks[q].text == "]":
                if q == p + 1:
                    return None
                return " ".join(x.text for x in toks[p + 1 : q]), q + 1
        return None
    return None


@dataclass(frozen=True)
class _Name:
    schema: str | None
    name: str | None  # None: the table itself is interpolated
    offset: int
    end: int  # index after the name
    bare: bool  # one unquoted word (the only form prose can produce)


def _name(toks: list[_Tok], p: int, jpql: bool = False) -> _Name | None:
    first = _part(toks, p, jpql)
    if first is None:
        return None
    parts = [first[0]]
    q = first[1]
    last_at = p
    while q + 1 < len(toks) and toks[q].text == "." and len(parts) < 4:
        nxt = _part(toks, q + 1, jpql)
        if nxt is None:
            break
        parts.append(nxt[0])
        last_at = q + 1
        q = nxt[1]
    bare = len(parts) == 1 and toks[p].kind == "word"
    schema = parts[-2] if len(parts) > 1 else None
    return _Name(schema, parts[-1], toks[last_at].start, q, bare)


def _skip_alias(toks: list[_Tok], p: int) -> int:
    if p < len(toks) and toks[p].upper == "AS":
        p += 1
        return p + 1 if p < len(toks) and toks[p].kind in ("word", "qident") else p
    if p < len(toks) and toks[p].kind == "qident":
        return p + 1
    if (
        p < len(toks)
        and toks[p].kind == "word"
        and not _is_hole(toks[p])
        and toks[p].upper not in _AFTER_TABLE
        and toks[p].upper not in _RESERVED
    ):
        return p + 1
    return p


def _ends_clause(toks: list[_Tok], p: int) -> bool:
    if p >= len(toks):
        return True
    t = toks[p]
    return t.text in (")", ",", ";", "(") or _is_hole(t) or t.upper in _AFTER_TABLE


def _is_title(text: str) -> bool:
    return len(text) > 1 and text[0].isupper() and text[1:].islower()


def _prose_case(verb: _Tok, structural: _Tok) -> bool:
    """`Delete from cart`: a capitalised first word followed by a lower-case
    keyword is a sentence. SQL keeps one case for its keywords."""
    return _is_title(verb.text) and structural.text.islower()


def _stop(n: _Name, toks: list[_Tok]) -> bool:
    """An English word where the table would be. A one-letter name (`a`, `i`)
    is a real table unless another plain word follows it (`from a list`)."""
    if not n.bare or n.name is None or n.name.lower() not in _STOPWORDS:
        return False
    if len(n.name) > 1:
        return True
    nxt = toks[n.end] if n.end < len(toks) else None
    return nxt is not None and nxt.kind == "word" and nxt.upper not in _AFTER_TABLE


def _skip_parens(toks: list[_Tok], p: int) -> int:
    """Index after the `(...)` group opening at `p`."""
    depth = 0
    for q in range(p, len(toks)):
        if toks[q].text == "(":
            depth += 1
        elif toks[q].text == ")":
            depth -= 1
            if depth == 0:
                return q + 1
    return len(toks)


def _cte_names(toks: list[_Tok], i: int) -> tuple[set[str], int] | None:
    """Names defined by the `WITH` at `i` and the index of the main verb."""
    names: set[str] = set()
    p = i + 1
    if p < len(toks) and toks[p].upper == "RECURSIVE":
        p += 1
    while True:
        if p >= len(toks) or toks[p].kind not in ("word", "qident") or _is_hole(toks[p]):
            return None
        names.add(_unquote(toks[p]).lower())
        p += 1
        if p < len(toks) and toks[p].text == "(":
            p = _skip_parens(toks, p)
        if p >= len(toks) or toks[p].upper != "AS":
            return None
        p += 1
        if p < len(toks) and toks[p].upper == "NOT":
            p += 1
        if p < len(toks) and toks[p].upper == "MATERIALIZED":
            p += 1
        if p >= len(toks) or toks[p].text != "(":
            return None
        p = _skip_parens(toks, p)
        if p < len(toks) and toks[p].text == ",":
            p += 1
            continue
        return names, p


class _Scan:
    """Collects reads and nested writes over one statement's tokens."""

    def __init__(self, toks: list[_Tok], ctes: set[str], jpql: bool) -> None:
        self.toks = toks
        self.ctes = ctes
        self.jpql = jpql
        self.out: list[SqlTable] = []

    def add(self, access: str, n: _Name) -> None:
        if n.name is None or not n.name.strip():
            return
        if access == READ and n.schema is None and n.name.lower() in self.ctes:
            return
        self.out.append(SqlTable(access, n.schema, n.name, n.offset))

    def from_list(self, p: int) -> tuple[int, bool]:
        """Table refs after a FROM at `p`. Returns (index after, any ref seen)."""
        toks = self.toks
        seen = False
        while p < len(toks):
            if toks[p].upper in ("ONLY", "LATERAL"):
                p += 1
            if p < len(toks) and toks[p].text == "(":
                p = _skip_alias(toks, _skip_parens(toks, p))
                seen = True
            else:
                n = _name(toks, p, self.jpql)
                if n is None:
                    break
                seen = True
                if n.end < len(toks) and toks[n.end].text == "(":  # table function
                    p = _skip_alias(toks, _skip_parens(toks, n.end))
                else:
                    self.add(READ, n)
                    p = _skip_alias(toks, n.end)
            if p < len(toks) and toks[p].text == ",":
                p += 1
                continue
            break
        return p, seen

    def run(self, start: int, verb: str) -> None:
        toks = self.toks
        stack: list[str] = []
        for k in range(start, len(toks)):
            t = toks[k]
            if t.kind == "punct":
                if t.text == "(":
                    stack.append(toks[k - 1].upper if k else "")
                elif t.text == ")" and stack:
                    stack.pop()
                continue
            up = t.upper
            prev = toks[k - 1] if k else None
            opens = prev is None or prev.text == "("
            if up == "FROM":
                if stack and stack[-1] in _FROM_FUNCS:
                    continue
                if prev is not None and prev.upper in ("DISTINCT", "DELETE"):
                    continue
                self.from_list(k + 1)
            elif up == "JOIN" or up == "STRAIGHT_JOIN":
                n = _name(toks, k + 1, self.jpql)
                if n is None or (self.jpql and n.schema is not None):
                    continue
                if n.end < len(toks) and toks[n.end].text == "(":
                    continue
                self.add(READ, n)
            elif up == "USING" and not stack and verb in ("MERGE", "DELETE"):
                n = _name(toks, k + 1, self.jpql)
                if n is not None:
                    self.add(READ, n)
            elif up == "INTO" and any(x.upper in _INTO_VERBS for x in toks[max(0, k - 4) : k]):
                n = _name(toks, k + 1, self.jpql)
                if n is not None and k != start:
                    self.add(WRITE, n)
            elif opens and k > start and up == "UPDATE":
                n = _name(toks, k + 1, self.jpql)
                if n is not None:
                    self.add(WRITE, n)
            elif opens and k > start and up == "DELETE":
                if k + 1 < len(toks) and toks[k + 1].upper == "FROM":
                    n = _name(toks, k + 2, self.jpql)
                    if n is not None:
                        self.add(WRITE, n)


def _statement(toks: list[_Tok], jpql: bool) -> list[SqlTable]:
    i = 0
    while i < len(toks) and toks[i].text == "(":
        i += 1
    if i >= len(toks) or toks[i].kind != "word":
        return []
    ctes: set[str] = set()
    with_at: int | None = None
    if toks[i].upper == "WITH":
        parsed = _cte_names(toks, i)
        if parsed is None:
            return []
        with_at = i
        ctes, i = parsed
        if i >= len(toks):
            return []
    verb_tok = toks[i]
    verb = verb_tok.upper
    scan = _Scan(toks, ctes, jpql)
    head: list[SqlTable] = []

    def after(p: int, *fillers: frozenset[str]) -> int:
        while p < len(toks) and any(toks[p].upper in f for f in fillers):
            p += 1
        return p

    if verb == "SELECT" or (verb == "FROM" and jpql):
        if verb == "SELECT":
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if nxt is None or nxt.upper == "FROM":
                return []
            after_nxt = toks[i + 2] if i + 2 < len(toks) else None
            if (
                nxt.kind == "word"
                and nxt.text.lower() in _STOPWORDS
                and after_nxt is not None
                and after_nxt.kind == "word"
                and after_nxt.upper not in _RESERVED
                and after_nxt.upper not in ("IS", "IN", "LIKE", "BETWEEN")
            ):
                return []  # "select a file from ..."
            depth = 0
            frm = None
            for k in range(i + 1, len(toks)):
                if toks[k].text == "(":
                    depth += 1
                elif toks[k].text == ")":
                    depth -= 1
                    if depth < 0:
                        break
                elif depth == 0 and toks[k].upper == "FROM" and toks[k - 1].upper != "DISTINCT":
                    frm = k
                    break
            if frm is None or _prose_case(verb_tok, toks[frm]):
                return []
        else:
            frm = i
        first = _name(toks, frm + 1, jpql)
        if first is not None and _stop(first, toks):
            return []
        end, any_ref = _Scan(toks, ctes, jpql).from_list(frm + 1)
        if not any_ref or not _ends_clause(toks, end):
            return []
    elif verb in ("INSERT", "REPLACE", "UPSERT"):
        p = after(i + 1, _INSERT_FILLERS)
        if p >= len(toks) or toks[p].upper != "INTO" or _prose_case(verb_tok, toks[p]):
            return []
        p += 1
        if p < len(toks) and toks[p].upper == "TABLE":
            p += 1
        n = _name(toks, p, jpql)
        if n is None or _stop(n, toks) or n.end >= len(toks):
            return []
        nxt = toks[n.end]
        if not (nxt.text == "(" or _is_hole(nxt) or nxt.upper in _AFTER_INSERT_TABLE):
            # `INSERT INTO t alias (...)` is not SQL; a bare word here is prose.
            return []
        head.append(SqlTable(WRITE, n.schema, n.name or "", n.offset))
    elif verb == "MERGE":
        p = i + 1
        if p < len(toks) and toks[p].upper == "INTO":
            p += 1
        n = _name(toks, p, jpql)
        if n is None or _stop(n, toks) or not any(t.upper == "USING" for t in toks[n.end :]):
            return []
        head.append(SqlTable(WRITE, n.schema, n.name or "", n.offset))
    elif verb == "UPDATE":
        p = after(i + 1, _UPDATE_FILLERS)
        if p + 1 < len(toks) and toks[p].upper == "OR":  # SQLite UPDATE OR IGNORE
            p += 2
        n = _name(toks, p, jpql)
        if n is None or _stop(n, toks):
            return []
        depth = 0
        set_tok = None
        for k in range(n.end, len(toks)):
            if toks[k].text == "(":
                depth += 1
            elif toks[k].text == ")":
                depth -= 1
            elif depth == 0 and toks[k].upper == "SET":
                set_tok = toks[k]
                break
        if set_tok is None or _prose_case(verb_tok, set_tok):
            return []
        head.append(SqlTable(WRITE, n.schema, n.name or "", n.offset))
    elif verb == "DELETE":
        p = i + 1
        if p >= len(toks) or toks[p].upper != "FROM" or _prose_case(verb_tok, toks[p]):
            return []
        p += 1
        if p < len(toks) and toks[p].upper == "ONLY":
            p += 1
        n = _name(toks, p, jpql)
        if n is None or _stop(n, toks) or not _ends_clause(toks, _skip_alias(toks, n.end)):
            return []
        head.append(SqlTable(WRITE, n.schema, n.name or "", n.offset))
    elif verb == "TRUNCATE":
        p = i + 1
        explicit = p < len(toks) and toks[p].upper == "TABLE"
        if explicit:
            p += 1
        if p < len(toks) and toks[p].upper == "ONLY":
            p += 1
        n = _name(toks, p, jpql)
        if n is None or _stop(n, toks):
            return []
        if not explicit and not (
            n.end >= len(toks) or toks[n.end].upper in ("CASCADE", "RESTART", "RESTRICT")
        ):
            return []
        return [SqlTable(WRITE, n.schema, n.name, n.offset)] if n.name else []
    elif verb in ("CREATE", "ALTER", "DROP"):
        p = after(i + 1, _CREATE_PREFIXES) if verb == "CREATE" else i + 1
        if p >= len(toks) or toks[p].upper != "TABLE" or _prose_case(verb_tok, toks[p]):
            return []
        p += 1
        while p < len(toks) and toks[p].upper in ("IF", "NOT", "EXISTS", "ONLY"):
            p += 1
        while True:
            n = _name(toks, p, jpql)
            if n is None or _stop(n, toks):
                return []
            if n.name:
                head.append(SqlTable(DDL, n.schema, n.name, n.offset))
            if verb == "DROP" and n.end < len(toks) and toks[n.end].text == ",":
                p = n.end + 1
                continue
            break
        if verb != "CREATE":
            return head
    else:
        return []

    scan.run(with_at if with_at is not None else i, verb)
    return [t for t in head if t.name] + scan.out
