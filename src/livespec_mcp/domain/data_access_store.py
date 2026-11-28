"""Persistence, model resolution and queries of the data-access map.

``data_access.py`` extracts, per file, the places a symbol reads / writes /
defines a table (``ExtractedAccess``) and the model -> table declarations that
resolve the ORM ones (``ExtractedModel``). This module:

- stores them (``persist_data_access``): accesses hang off their symbol, so
  re-extracting a file replaces them through the cascade; model declarations
  hang off the file and are deleted before being re-inserted;
- resolves every ORM access to a table after each index run
  (``resolve_data_access``), never inventing a name: an access stays
  ``unresolved`` (model known, table not) when the source does not state it.
  The same pass DERIVES the rows that depend on another file (an SQL constant
  imported from another module, a call on a write-only collection declared in
  a model): placeholders are stored per file, the rows are rebuilt on every run,
  so editing the defining file never leaves a stale one;
- answers the questions of ``find_data_access``: who touches a table (or a
  model), what does a symbol touch (optionally through its callees), and which
  tables exist, where each is declared (a ``.sql`` file or code) and which ORM
  accesses could not be tied to a table.

Nothing here shapes an MCP error; unknown names come back as ``None`` plus
suggestions so ``domain/`` never imports ``tools/``.
"""

from __future__ import annotations

import difflib
import sqlite3
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from livespec_mcp.domain.data_access import READ, REL_EDGE_OPS, REL_OPS, WRITE
from livespec_mcp.domain.extractors import ExtractResult
from livespec_mcp.domain.graph import INVOCATION_EDGE_TYPES

ACCESS_KINDS: tuple[str, ...] = ("read", "write", "ddl")
#: Call-graph hops `include_callees` walks. Small on purpose: a table reached
#: through five calls is a rumour, not a fact.
CALLEE_DEPTH = 3
CALLER_DEPTH = CALLEE_DEPTH  # old name, kept for importers
#: Catalogue tables, not application data: counted apart from the inventory.
SYSTEM_TABLES = frozenset(
    "sqlite_master sqlite_schema sqlite_temp_master sqlite_temp_schema sqlite_sequence "
    "sqlite_stat1 sqlite_stat4".split()
)
SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog"})
#: Most access locations listed per unresolved model in the inventory.
UNRESOLVED_LOCATIONS = 20


def is_system_table(schema: str | None, table: str | None) -> bool:
    """``sqlite_master``, ``information_schema.*``, ``pg_catalog.*`` and kin."""
    return bool(table) and (
        (schema or "").lower() in SYSTEM_SCHEMAS or (table or "").lower() in SYSTEM_TABLES
    )


#: Same fan-out floor as who_calls: ambiguous name matches (weight 0.5) would
#: attach tables to a function that never reaches them.
MIN_EDGE_WEIGHT = 0.6

CONFIDENCE_LITERAL = "literal"  # the table is written at the site
_ORDER = {"write": 0, "ddl": 1, "read": 2}


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def persist_data_access(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    result: ExtractResult,
    qname_to_id: dict[str, int],
) -> None:
    """Store one file's accesses and model declarations.

    Called right after the file's symbols are inserted, so the symbol cascade
    has already dropped the previous accesses. `data_model` rows hang off the
    file and are replaced here."""
    conn.execute("DELETE FROM data_model WHERE file_id=?", (file_id,))
    conn.executemany(
        """INSERT INTO data_access(symbol_id, table_name, table_schema, model, ref_name,
               ref_kind, access, mechanism, line, confidence)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                qname_to_id[a.src_qname],
                a.table,
                a.schema,
                None,
                a.ref_name,
                a.ref_kind,
                a.access,
                a.mechanism,
                a.line,
                CONFIDENCE_LITERAL if a.table else "unresolved",
            )
            for a in result.accesses
            if a.src_qname in qname_to_id and (a.table or a.ref_name)
        ],
    )
    conn.executemany(
        """INSERT INTO data_model(file_id, symbol_id, name, role, mechanism, table_name,
               table_schema, entity, table_source, line, owner, secondary, detail)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                file_id,
                qname_to_id.get(m.src_qname),
                m.name,
                m.role,
                m.mechanism,
                m.table,
                m.schema,
                m.entity,
                m.table_source,
                m.line,
                m.owner,
                m.secondary,
                m.detail,
            )
            for m in result.models
        ],
    )


@dataclass(frozen=True)
class _ModelRow:
    name: str
    role: str
    mechanism: str
    table: str | None
    schema: str | None
    entity: str | None
    source: str | None
    project_id: int
    owner: str | None = None
    secondary: str | None = None
    detail: str | None = None
    symbol_id: int | None = None
    line: int = 0


def _pick(rows: list[_ModelRow], project_id: int, mechanism: str) -> list[_ModelRow]:
    """Narrow candidates: same project first, then same mechanism family."""
    same_project = [r for r in rows if r.project_id == project_id]
    rows = same_project or rows
    same_mech = [r for r in rows if r.mechanism == mechanism]
    return same_mech or rows


def _resolve_model(
    by_name: dict[str, list[_ModelRow]], model: str, pid: int, mechanism: str
) -> tuple[str | None, str | None, str]:
    """(table, schema, confidence) of a model name; a model resolves only when
    ALL the candidates agree on one (table, schema)."""
    cands = _pick([r for r in by_name.get(model, []) if r.role == "model"], pid, mechanism)
    targets = {(r.table, r.schema) for r in cands if r.table}
    if len(targets) != 1:
        return None, None, "unresolved"
    table, schema = next(iter(targets))
    sources = {r.source for r in cands if r.table}
    return table, schema, "convention" if sources == {"convention"} else "model"


def _module_key(name: str) -> str:
    """`pkg.mod.__init__:NAME` -> `pkg.mod:NAME`."""
    module, _, const = name.rpartition(":")
    return f"{module.removesuffix('.__init__')}:{const}"


def resolve_data_access(conn: sqlite3.Connection) -> int:
    """Fill `model`, `table_name`, `table_schema` and `confidence` of every
    access that names a model / entity / repository instead of a table, and
    rebuild the rows that depend on another file.

    DB-wide (a group DB holds several projects; an entity may live in another
    repository than its repository interface). Idempotent: every run re-derives
    every ORM row, so a model that changed or disappeared in another file is
    picked up. A model resolves only when ALL the candidates agree on one
    (table, schema); two different tables behind one name stay unresolved.

    Derived rows (`ref_kind` ``const`` / ``relationship``) come from
    placeholders of ``data_model`` and are deleted and rebuilt here: an SQL
    constant imported from another module expands to one access per table of
    that constant; a call on a write-only / dynamic collection resolves to its
    target model (or association table). A placeholder that matches nothing
    yields nothing. Returns the number of ORM rows examined."""
    rows: list[_ModelRow] = []
    for r in conn.execute(
        """SELECT m.name, m.role, m.mechanism, m.table_name, m.table_schema, m.entity,
                  m.table_source, m.owner, m.secondary, m.detail, m.symbol_id, m.line,
                  f.project_id
           FROM data_model m JOIN file f ON f.id = m.file_id"""
    ):
        rows.append(
            _ModelRow(
                r["name"],
                r["role"],
                r["mechanism"],
                r["table_name"],
                r["table_schema"],
                r["entity"],
                r["table_source"],
                int(r["project_id"]),
                r["owner"],
                r["secondary"],
                r["detail"],
                r["symbol_id"],
                int(r["line"]),
            )
        )
    flask_dbs = {(r.project_id, r.name) for r in rows if r.role == "flask_db"}
    flask_db_names = {r.name for r in rows if r.role == "flask_db"}
    by_name: dict[str, list[_ModelRow]] = {}
    for row in rows:
        if row.role not in ("model", "repository"):
            continue
        if row.source == "flask_candidate":
            # The default name is Flask-SQLAlchemy's convention: it holds only
            # if the base `db` is one of its instances.
            ok = (row.project_id, row.entity) in flask_dbs or row.entity in flask_db_names
            row = _ModelRow(
                row.name, row.role, row.mechanism, row.table if ok else None, row.schema,
                row.entity, "convention", row.project_id,
            )  # fmt: skip
        by_name.setdefault(row.name, []).append(row)

    conn.execute("DELETE FROM data_access WHERE ref_kind IN ('const', 'relationship')")
    _derive_const_rows(conn, rows)
    _derive_relationship_rows(conn, rows, by_name)

    updates: list[tuple[Any, ...]] = []
    examined = 0
    for a in conn.execute(
        """SELECT d.id, d.ref_name, d.ref_kind, d.mechanism, f.project_id
           FROM data_access d JOIN symbol s ON s.id = d.symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE d.ref_name IS NOT NULL AND d.ref_kind IN ('model', 'repository')"""
    ):
        examined += 1
        pid = int(a["project_id"])
        model = a["ref_name"]
        if a["ref_kind"] == "repository":
            repos = _pick(
                [r for r in by_name.get(model, []) if r.role == "repository"], pid, a["mechanism"]
            )
            entities = {r.entity for r in repos if r.entity}
            model = entities.pop() if len(entities) == 1 else None
        table = schema = None
        confidence = "unresolved"
        if model is not None:
            table, schema, confidence = _resolve_model(by_name, model, pid, a["mechanism"])
        updates.append((model, table, schema, confidence, a["id"]))
    conn.executemany(
        "UPDATE data_access SET model=?, table_name=?, table_schema=?, confidence=? WHERE id=?",
        updates,
    )
    _derive_jpa_relationship_rows(conn, rows, by_name)
    return examined


def _derive_jpa_relationship_rows(
    conn: sqlite3.Connection, rows: list[_ModelRow], by_name: dict[str, list[_ModelRow]]
) -> None:
    """An access to a JPA entity reaches the tables of the entities its
    relationship fields point to: `owners.save(owner)` writes `pets` and
    `visits` when the cascade says so, reading an owner reads its `pets`.

    One hop for a read (a lazy collection is loaded on first use, and what it
    holds is a second access); a write follows `cascade` (ALL / PERSIST / MERGE
    / REMOVE) transitively. A many-to-many reaches its `@JoinTable` with the
    access that touched the owning side. Rows are `ref_kind='relationship'`,
    mechanism `jpa`, with the field as `through`."""
    rels: dict[str, list[_ModelRow]] = {}
    for r in rows:
        if r.role == "relationship" and r.mechanism == "jpa" and r.entity and r.owner:
            rels.setdefault(r.owner, []).append(r)
    if not rels:
        return
    out: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()

    def emit(a: sqlite3.Row, rel: _ModelRow, access: str, model: str, join: str | None) -> None:
        pid = int(a["project_id"])
        table = schema = None
        confidence = "unresolved"
        if join:
            table, confidence = join, "model"
        else:
            table, schema, confidence = _resolve_model(by_name, model, pid, "jpa")
        key = (a["symbol_id"], a["line"], access, model, table)
        if key in seen:
            return
        seen.add(key)
        out.append(
            (a["symbol_id"], table, schema, None if join else model,
             f"{rel.owner}.{rel.name}:jpa", "relationship", access, "jpa", a["line"], confidence)
        )  # fmt: skip

    for a in conn.execute(
        """SELECT d.symbol_id, d.line, d.access, d.model, f.project_id
           FROM data_access d JOIN symbol s ON s.id = d.symbol_id JOIN file f ON f.id = s.file_id
           WHERE d.mechanism = 'jpa' AND d.ref_kind IN ('model', 'repository')
             AND d.model IS NOT NULL"""
    ).fetchall():
        pid = int(a["project_id"])
        reached = {a["model"]}
        frontier = [a["model"]]
        depth = 0
        while frontier:
            nxt: list[str] = []
            for owner in frontier:
                for rel in _pick(rels.get(owner, []), pid, "jpa"):
                    cascades = rel.detail == "jpa-cascade"
                    access = WRITE if a["access"] == WRITE and cascades else READ
                    if depth == 0 or (a["access"] == WRITE and cascades):
                        emit(a, rel, access, rel.entity or "", None)
                        if rel.secondary and rel.secondary.startswith("=") and depth == 0:
                            emit(a, rel, a["access"], "", rel.secondary[1:].lower())
                    if a["access"] == WRITE and cascades and rel.entity not in reached:
                        reached.add(rel.entity or "")
                        nxt.append(rel.entity or "")
            frontier = nxt
            depth += 1
    conn.executemany(
        """INSERT INTO data_access(symbol_id, table_name, table_schema, model, ref_name,
               ref_kind, access, mechanism, line, confidence)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        out,
    )


def _derive_const_rows(conn: sqlite3.Connection, rows: list[_ModelRow]) -> None:
    """One access per (kind, table) of the SQL constant a placeholder names.

    `constref` rows name the module the code imported the constant from (as
    written, relative levels resolved); `sqlconst` rows are keyed by the file's
    dotted path, which may carry a source-root prefix (``src.pkg.mod``), so a
    match is the same dotted path or one that ends with it. A name defined in
    several modules is resolved only inside the user's project, else skipped."""
    by_const: dict[str, dict[str, list[_ModelRow]]] = {}
    for r in rows:
        if r.role == "sqlconst":
            key = _module_key(r.name)
            module, _, const = key.rpartition(":")
            by_const.setdefault(const, {}).setdefault(module, []).append(r)
    if not by_const:
        return
    out: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for ph in rows:
        if ph.role != "constref" or ph.symbol_id is None:
            continue
        target, _, const = _module_key(ph.name).rpartition(":")
        mods = {
            m: defs
            for m, defs in by_const.get(const, {}).items()
            if m == target or m.endswith("." + target)
        }
        if len(mods) > 1:
            mods = {m: d for m, d in mods.items() if any(x.project_id == ph.project_id for x in d)}
        if len(mods) != 1:
            continue
        for d in next(iter(mods.values())):
            key = (ph.symbol_id, ph.line, d.detail, d.table, d.schema)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                (ph.symbol_id, d.table, d.schema, None, ph.name, "const", d.detail, "sql",
                 ph.line, "literal")
            )  # fmt: skip
    conn.executemany(
        """INSERT INTO data_access(symbol_id, table_name, table_schema, model, ref_name,
               ref_kind, access, mechanism, line, confidence)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        out,
    )


def _secondary_target(secondary: str) -> tuple[str, str | None]:
    """`(model, table)` of a relationship's ``secondary=``: a literal name is the
    table itself, a bare name is a Table / model variable resolved later."""
    if secondary.startswith("="):
        return "", secondary[1:].lower()
    return secondary, None


def _derive_relationship_rows(
    conn: sqlite3.Connection, rows: list[_ModelRow], by_name: dict[str, list[_ModelRow]]
) -> None:
    """Accesses through `owner.attr.<op>()` on a collection a class declares.

    Resolved only when the declaration is unambiguous: `self.attr` inside the
    declaring class matches that class's attribute; a call on any other
    receiver matches only an attribute name declared as a collection by exactly
    one class (so `user.posts.select()` is `User.posts`). The operation must be
    one the collection's mode answers. `add` / `remove` of a many-to-many
    collection write its association table; every other operation reaches the
    target model."""
    rels: dict[str, list[_ModelRow]] = {}
    for r in rows:
        if r.role == "relationship" and r.entity and r.detail in REL_OPS:
            rels.setdefault(r.name, []).append(r)
    if not rels:
        return
    out: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for ph in rows:
        if ph.role != "relref" or ph.symbol_id is None or ph.name not in rels or not ph.detail:
            continue
        op = ph.detail
        cands = [r for r in rels[ph.name] if op in REL_OPS[r.detail or ""]]
        cands = [r for r in cands if r.project_id == ph.project_id] or cands
        if ph.owner and ph.owner != "*":
            cands = [r for r in cands if r.owner == ph.owner]
        if len({(r.owner, r.entity, r.secondary, r.detail) for r in cands}) != 1:
            continue
        rel = cands[0]
        access = REL_OPS[rel.detail or ""][op]
        # (model, table): the target model for most operations, the association
        # table for add / remove. A READ through a many-to-many collection also
        # reads its association table (`select()` joins it), so both rows.
        reached: list[tuple[str, str | None]] = []
        if op in REL_EDGE_OPS and rel.secondary:
            reached.append(_secondary_target(rel.secondary))
        else:
            reached.append((rel.entity or "", None))
            if access == READ and rel.secondary:
                reached.append(_secondary_target(rel.secondary))
        for model, fixed_table in reached:
            table = schema = None
            confidence = "unresolved"
            if fixed_table:
                table, confidence = fixed_table, "model"
            elif model:
                table, schema, confidence = _resolve_model(
                    by_name, model, ph.project_id, "sqlalchemy"
                )
            key = (ph.symbol_id, ph.line, access, model, table)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                (ph.symbol_id, table, schema, model or None, f"{rel.owner}.{ph.name}:{op}",
                 "relationship", access, "sqlalchemy", ph.line, confidence)
            )  # fmt: skip
    conn.executemany(
        """INSERT INTO data_access(symbol_id, table_name, table_schema, model, ref_name,
               ref_kind, access, mechanism, line, confidence)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        out,
    )


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def split_table_name(raw: str) -> tuple[str | None, str]:
    """`"Public"."Users"` / `public.users` / `users` -> (schema|None, table), lower-case."""
    parts = [p.strip().strip('`"[]').lower() for p in raw.strip().split(".") if p.strip()]
    if not parts:
        return None, ""
    return (parts[-2] if len(parts) > 1 else None), parts[-1]


_ROW_SELECT = """SELECT d.table_name, d.table_schema, d.model, d.access, d.mechanism, d.line,
                        d.confidence, d.ref_name, d.ref_kind, s.qualified_name AS qname,
                        s.id AS symbol_id, f.path AS file_path, f.project_id, p.name AS project
                 FROM data_access d
                 JOIN symbol s ON s.id = d.symbol_id
                 JOIN file f ON f.id = s.file_id
                 JOIN project p ON p.id = f.project_id"""


@dataclass(slots=True)
class FileScope:
    """Which accessing files a ``find_data_access`` answer covers: those under
    ``path`` (a directory prefix, or one file), minus test files unless
    ``include_tests``. Test accesses it leaves out are counted in
    ``dropped_tests`` as the queries run."""

    path: str | None = None
    include_tests: bool = False
    is_test: Callable[[str], bool] = lambda _p: False
    dropped_tests: int = 0

    def __post_init__(self) -> None:
        if self.path:
            self.path = self.path.replace("\\", "/").strip().lstrip("./").rstrip("/") or None

    def allows(self, file_path: str) -> bool:
        if self.path and file_path != self.path and not file_path.startswith(f"{self.path}/"):
            return False
        return self.include_tests or not self.is_test(file_path)

    def keep(self, file_path: str) -> bool:
        """`allows`, counting the test accesses it drops."""
        if self.allows(file_path):
            return True
        if (
            not self.include_tests
            and self.is_test(file_path)
            and (not self.path or file_path == self.path or file_path.startswith(f"{self.path}/"))
        ):
            self.dropped_tests += 1
        return False


def _in(ids: Iterable[int]) -> tuple[str, tuple[int, ...]]:
    t = tuple(ids)
    return ",".join("?" for _ in t), t


def _row_payload(r: sqlite3.Row, *, with_symbol: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if with_symbol:
        out["symbol"] = r["qname"]
        out["file"] = f"{r['file_path']}:{r['line']}"
    else:
        out["line"] = r["line"]
    out["kind"] = r["access"]
    out["mechanism"] = r["mechanism"]
    if r["ref_kind"] == "const":
        out["constant"] = r["ref_name"]  # `module:NAME`, imported from another file
    elif r["model"] or r["ref_name"]:
        out["model"] = r["model"] or r["ref_name"]
    if r["ref_kind"] == "relationship":
        out["through"] = r["ref_name"]  # `Owner.attr:op`
    if r["confidence"] != CONFIDENCE_LITERAL:
        out["confidence"] = r["confidence"]
    return out


def known_tables(conn: sqlite3.Connection, pids: list[int]) -> dict[str, str]:
    """lower-case table name -> display name, over accesses, model declarations,
    tables declared in code and tables declared in indexed `.sql` files."""
    ph, args = _in(pids)
    names: dict[str, str] = {}
    for sql in (
        f"""SELECT DISTINCT d.table_name AS n FROM data_access d JOIN symbol s ON s.id=d.symbol_id
            JOIN file f ON f.id=s.file_id
            WHERE f.project_id IN ({ph}) AND d.table_name IS NOT NULL""",
        f"""SELECT DISTINCT m.table_name AS n FROM data_model m JOIN file f ON f.id=m.file_id
            WHERE f.project_id IN ({ph}) AND m.table_name IS NOT NULL
              AND m.role IN ('model', 'table') AND m.table_source IS NOT 'flask_candidate'""",
        f"""SELECT DISTINCT s.name AS n FROM symbol s JOIN schema_decl sd ON sd.symbol_id=s.id
            JOIN file f ON f.id=s.file_id
            WHERE f.project_id IN ({ph}) AND sd.flavor='sql_table'""",
    ):
        for r in conn.execute(sql, args):
            if r["n"]:
                names.setdefault(str(r["n"]).lower(), str(r["n"]))
    return names


def suggest_tables(conn: sqlite3.Connection, pids: list[int], table: str) -> list[str]:
    _, t = split_table_name(table)
    names = sorted(known_tables(conn, pids))
    close = difflib.get_close_matches(t, names, n=5, cutoff=0.5)
    subs = [n for n in names if t and t in n and n not in close]
    return (close + subs)[:5]


def declared_tables(conn: sqlite3.Connection, pids: list[int]) -> dict[str, list[dict[str, Any]]]:
    """lower-case table -> [{project, file, source}] for every declaration:
    ``source`` is ``sql_file`` (a CREATE TABLE in an indexed `.sql` file) or
    ``code`` (a CREATE TABLE in a string of the program, a SQL constant
    included, or a query builder's ``createTable``)."""
    ph, args = _in(pids)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        f"""SELECT s.name, s.start_line, f.path, p.name AS project
            FROM symbol s JOIN schema_decl sd ON sd.symbol_id=s.id
            JOIN file f ON f.id=s.file_id JOIN project p ON p.id=f.project_id
            WHERE f.project_id IN ({ph}) AND sd.flavor='sql_table'
            ORDER BY f.path, s.start_line""",
        args,
    ):
        out.setdefault(str(r["name"]).lower(), []).append(
            {
                "project": r["project"],
                "file": f"{r['path']}:{r['start_line']}",
                "source": "sql_file",
            }
        )
    for r in conn.execute(
        f"""SELECT m.table_name, m.line, f.path, p.name AS project
            FROM data_model m JOIN file f ON f.id=m.file_id JOIN project p ON p.id=f.project_id
            WHERE f.project_id IN ({ph}) AND m.role='table' AND m.table_name IS NOT NULL
            ORDER BY f.path, m.line""",
        args,
    ):
        out.setdefault(str(r["table_name"]).lower(), []).append(
            {"project": r["project"], "file": f"{r['path']}:{r['line']}", "source": "code"}
        )
    return out


def table_accesses(
    conn: sqlite3.Connection,
    pids: list[int],
    schema: str | None,
    table: str,
    access: str | None,
    scope: FileScope | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rows touching `table` (any schema unless given) and exact per-kind counts
    over ALL kinds, so a kind filter never hides what else is there."""
    ph, args = _in(pids)
    where = f"f.project_id IN ({ph}) AND d.table_name = ?"
    params: list[Any] = [*args, table]
    if schema:
        # An unqualified access may live in that schema (search_path); only a
        # different explicit schema rules a row out.
        where += " AND (d.table_schema IS NULL OR d.table_schema = ?)"
        params.append(schema)
    return _accesses(conn, where, params, access, scope)


def model_accesses(
    conn: sqlite3.Connection,
    pids: list[int],
    model: str,
    access: str | None,
    scope: FileScope | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], set[str]]:
    """Rows whose ORM model (case-insensitive) is `model`, the exact per-kind
    counts, and the tables those rows resolved to (empty if none did)."""
    ph, args = _in(pids)
    where = f"f.project_id IN ({ph}) AND (lower(d.model) = ? OR lower(d.ref_name) = ?)"
    params: list[Any] = [*args, model.lower(), model.lower()]
    rows, counts = _accesses(conn, where, params, access, scope)
    tables = {
        str(r["table_name"])
        for r in conn.execute(f"{_ROW_SELECT} WHERE {where} AND d.table_name IS NOT NULL", params)
        if scope is None or scope.allows(r["file_path"])
    }
    return rows, counts, tables


def _accesses(
    conn: sqlite3.Connection,
    where: str,
    params: list[Any],
    access: str | None,
    scope: FileScope | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = conn.execute(
        f"{_ROW_SELECT} WHERE {where} ORDER BY f.path, d.line, d.access", params
    ).fetchall()
    if scope is not None:
        rows = [r for r in rows if scope.keep(r["file_path"])]
    counts = {k: 0 for k in ACCESS_KINDS}
    for r in rows:
        counts[r["access"]] += 1
    wanted = [r for r in rows if access is None or r["access"] == access]
    wanted.sort(key=lambda r: (_ORDER[r["access"]], r["file_path"], r["line"]))
    return [_row_payload(r, with_symbol=True) | _table_cols(r) for r in wanted], counts


def _table_cols(r: sqlite3.Row) -> dict[str, Any]:
    # Qualified only when the source said so; kept off the common row.
    out: dict[str, Any] = {"schema": r["table_schema"]} if r["table_schema"] else {}
    if r["table_name"] is None:
        out["table"] = None
    if is_system_table(r["table_schema"], r["table_name"]):
        out["system"] = True
    return out


def symbol_tables(
    conn: sqlite3.Connection,
    pids: list[int],
    symbol_id: int,
    *,
    include_callees: bool,
    access: str | None,
    table: tuple[str | None, str] | None,
) -> list[dict[str, Any]]:
    """Tables `symbol_id` touches; with `include_callees` also those reached
    through its callees up to ``CALLEE_DEPTH`` hops, each row naming the callee
    that holds the access (`via`) and the hop count (`depth`). A symbol that is
    reached by several paths is reported at its shallowest depth. Every row
    carries ``file`` (`path:line` of the access); catalogue tables
    (``sqlite_master``, ``information_schema.*``) carry ``system: true``."""
    # symbol id -> (hops from the root, first callee on the way there)
    holders: dict[int, tuple[int, int | None]] = {symbol_id: (0, None)}
    if include_callees:
        ph, _ = _in(sorted(INVOCATION_EDGE_TYPES))
        seen = {symbol_id}
        queue: deque[tuple[int, int]] = deque([(symbol_id, 0)])
        while queue:
            cur, depth = queue.popleft()
            if depth >= CALLEE_DEPTH:
                continue
            for e in conn.execute(
                f"""SELECT e.dst_symbol_id AS dst FROM symbol_edge e
                    WHERE e.src_symbol_id=? AND e.weight >= ? AND e.edge_type IN ({ph})""",
                (cur, MIN_EDGE_WEIGHT, *sorted(INVOCATION_EDGE_TYPES)),
            ):
                dst = int(e["dst"])
                if dst not in seen:
                    seen.add(dst)
                    holders[dst] = (depth + 1, dst if depth == 0 else holders[cur][1])
                    queue.append((dst, depth + 1))
    ids = list(holders)
    firsts = sorted({f for _, f in holders.values() if f is not None})
    names: dict[int, str] = {}
    if firsts:
        fph, fargs = _in(firsts)
        names = {
            int(r["id"]): r["qualified_name"]
            for r in conn.execute(
                f"SELECT id, qualified_name FROM symbol WHERE id IN ({fph})", fargs
            )
        }
    out: list[dict[str, Any]] = []
    for chunk in (ids[i : i + 500] for i in range(0, len(ids), 500)):
        ph, args = _in(chunk)
        for r in conn.execute(f"{_ROW_SELECT} WHERE d.symbol_id IN ({ph})", args):
            if access is not None and r["access"] != access:
                continue
            if table is not None and (
                r["table_name"] != table[1]
                or (table[0] and r["table_schema"] not in (None, table[0]))
            ):
                continue
            depth, first = holders[int(r["symbol_id"])]
            row: dict[str, Any] = {"table": r["table_name"]}
            if r["table_schema"]:
                row["schema"] = r["table_schema"]
            if is_system_table(r["table_schema"], r["table_name"]):
                row["system"] = True
            row |= _row_payload(r, with_symbol=False)
            row["file"] = f"{r['file_path']}:{r['line']}"
            if depth:
                row["via"] = r["qname"]
                row["depth"] = depth
                if depth > 1 and first is not None:
                    row["through"] = names.get(first) or ""
            out.append(row)
    out.sort(
        key=lambda x: (
            x.get("depth", 0),
            x.get("via", ""),
            x["table"] is None,
            x["table"] or "",
            _ORDER[x["kind"]],
            x["line"],
        )
    )
    return out


def inventory(
    conn: sqlite3.Connection,
    pids: list[int],
    access: str | None,
    *,
    include_system: bool = False,
    unresolved_cursor: int = 0,
    unresolved_limit: int = 200,
    scope: FileScope | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """(table rows, exact counts, unresolved ORM models).

    A table row carries read / write / ddl counts, the number of distinct
    symbols touching it, and ``status``: ``ok`` (accessed and declared),
    ``undeclared`` (accessed, no CREATE TABLE seen) or ``unaccessed``
    (declared, nothing in the code reaches it). A declaration is a CREATE TABLE
    in an indexed `.sql` file or in code (a SQL string or constant);
    ``declared_in`` lists up to three ``file:line`` and ``declared_source`` says
    which kind they are (``sql_file``, ``code`` or ``sql_file+code``). The kind
    filter narrows which tables are listed, never the per-table counts.

    Catalogue tables (``sqlite_master``, ``information_schema.*``,
    ``pg_catalog.*``) are left out of the rows and the counts, and counted in
    ``system_tables_hidden``, unless `include_system`; then they are listed
    with ``system: true``.

    Unresolved ORM models (accessed, table not stated by the source) come with
    the ``file:line`` of their accesses (at most ``UNRESOLVED_LOCATIONS`` each,
    the rest counted in ``accesses_more``); the list is sliced by
    `unresolved_cursor` / `unresolved_limit` and its exact length is
    ``counts['unresolved_models']``.

    `scope` keeps only the accesses made from files under its path, without
    test files unless it includes them; a table whose every access was left out
    by it is not listed unless declared (then ``test_only`` or ``status``
    ``unaccessed`` says why)."""
    ph, args = _in(pids)
    declared = declared_tables(conn, pids)
    left_out: set[str] = set()
    acc: dict[str, dict[str, Any]] = {}
    unresolved: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(
        f"""SELECT d.table_name, d.table_schema, d.access, d.mechanism, d.model, d.ref_name,
                   d.symbol_id, d.line, s.qualified_name AS qname, f.path AS file_path
            FROM data_access d JOIN symbol s ON s.id=d.symbol_id JOIN file f ON f.id=s.file_id
            WHERE f.project_id IN ({ph}) ORDER BY f.path, d.line""",
        args,
    ):
        if scope is not None:
            before = scope.dropped_tests
            if not scope.keep(r["file_path"]):
                if scope.dropped_tests > before and r["table_name"] is not None:
                    left_out.add(r["table_name"])
                continue
        if r["table_name"] is None:
            model = r["model"] or r["ref_name"]
            u = unresolved.setdefault(
                (model, r["mechanism"]),
                {
                    "model": model,
                    "mechanism": r["mechanism"],
                    "read": 0,
                    "write": 0,
                    "ddl": 0,
                    "accesses": [],
                },
            )
            u[r["access"]] += 1
            if len(u["accesses"]) < UNRESOLVED_LOCATIONS:
                u["accesses"].append(
                    {
                        "symbol": r["qname"],
                        "file": f"{r['file_path']}:{r['line']}",
                        "kind": r["access"],
                    }
                )
            continue
        t = acc.setdefault(
            r["table_name"],
            {"schemas": set(), "read": 0, "write": 0, "ddl": 0, "symbols": set(), "mech": set()},
        )
        if r["table_schema"]:
            t["schemas"].add(r["table_schema"])
        t[r["access"]] += 1
        t["symbols"].add(r["symbol_id"])
        t["mech"].add(r["mechanism"])

    rows: list[dict[str, Any]] = []
    hidden = 0
    # Under a path only the tables its code touches are of interest; a table
    # merely declared somewhere else says nothing about it.
    names = set(acc) if scope is not None and scope.path else set(acc) | set(declared)
    for name in sorted(names):
        a = acc.get(name)
        system = is_system_table(next(iter(sorted(a["schemas"])), None) if a else None, name)
        if system and not include_system:
            hidden += 1
            continue
        status = "unaccessed" if a is None else "ok" if name in declared else "undeclared"
        row: dict[str, Any] = {
            "table": name,
            "read": a["read"] if a else 0,
            "write": a["write"] if a else 0,
            "ddl": a["ddl"] if a else 0,
            "symbols": len(a["symbols"]) if a else 0,
            "status": status,
        }
        if system:
            row["system"] = True
        if a is None and name in left_out:
            row["test_only"] = True  # only test code touches it
        if a and a["schemas"]:
            row["schema"] = (
                sorted(a["schemas"])[0] if len(a["schemas"]) == 1 else sorted(a["schemas"])
            )
        if a:
            row["mechanisms"] = sorted(a["mech"])
        if name in declared:
            decls = sorted(declared[name], key=lambda d: d["source"] != "sql_file")
            row["declared_in"] = [d["file"] for d in decls][:3]
            row["declared_source"] = "+".join(sorted({d["source"] for d in decls}))
        rows.append(row)

    skipped = conn.execute(
        f"""SELECT COUNT(*) AS n FROM data_model m JOIN file f ON f.id=m.file_id
            WHERE f.project_id IN ({ph}) AND m.role='skip'""",
        args,
    ).fetchone()["n"]
    counts = {
        "tables": len(rows),
        "read": sum(r["read"] for r in rows),
        "write": sum(r["write"] for r in rows),
        "ddl": sum(r["ddl"] for r in rows),
        "accessed_undeclared": sum(r["status"] == "undeclared" for r in rows),
        "declared_unaccessed": sum(r["status"] == "unaccessed" for r in rows),
        "declared_in_code": sum("code" in r.get("declared_source", "") for r in rows),
        "unresolved_orm_accesses": sum(
            u["read"] + u["write"] + u["ddl"] for u in unresolved.values()
        ),
        "system_tables_hidden": hidden,
        "skipped": {"dynamic_table": int(skipped)},
    }
    if scope is not None and scope.dropped_tests:
        counts["filtered_out"] = {
            "tests": scope.dropped_tests,
            "test_only_tables": len(left_out - set(acc)),
        }
    if access is not None:
        rows = [r for r in rows if r[access] > 0]
    unresolved_rows = sorted(unresolved.values(), key=lambda u: (u["model"] or "", u["mechanism"]))
    counts["unresolved_models"] = len(unresolved_rows)
    for u in unresolved_rows:
        total = u["read"] + u["write"] + u["ddl"]
        if total > len(u["accesses"]):
            u["accesses_more"] = total - len(u["accesses"])
    page = unresolved_rows[unresolved_cursor : unresolved_cursor + unresolved_limit]
    return rows, counts, page
