"""Persistence, model resolution and queries of the data-access map.

``data_access.py`` extracts, per file, the places a symbol reads / writes /
defines a table (``ExtractedAccess``) and the model -> table declarations that
resolve the ORM ones (``ExtractedModel``). This module:

- stores them (``persist_data_access``): accesses hang off their symbol, so
  re-extracting a file replaces them through the cascade; model declarations
  hang off the file and are deleted before being re-inserted;
- resolves every ORM access to a table after each index run
  (``resolve_data_access``), never inventing a name: an access stays
  ``unresolved`` (model known, table not) when the source does not state it;
- answers the three questions of ``find_data_access``: who touches a table, what
  does a symbol touch (optionally through its callees), and which tables exist.

Nothing here shapes an MCP error; unknown names come back as ``None`` plus
suggestions so ``domain/`` never imports ``tools/``.
"""

from __future__ import annotations

import difflib
import sqlite3
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from livespec_mcp.domain.extractors import ExtractResult
from livespec_mcp.domain.graph import INVOCATION_EDGE_TYPES

ACCESS_KINDS: tuple[str, ...] = ("read", "write", "ddl")
#: Call-graph hops `include_callers` walks. Small on purpose: a table reached
#: through five calls is a rumour, not a fact.
CALLER_DEPTH = 3
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
               table_schema, entity, table_source, line)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
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


def _pick(rows: list[_ModelRow], project_id: int, mechanism: str) -> list[_ModelRow]:
    """Narrow candidates: same project first, then same mechanism family."""
    same_project = [r for r in rows if r.project_id == project_id]
    rows = same_project or rows
    same_mech = [r for r in rows if r.mechanism == mechanism]
    return same_mech or rows


def resolve_data_access(conn: sqlite3.Connection) -> int:
    """Fill `model`, `table_name`, `table_schema` and `confidence` of every
    access that names a model / entity / repository instead of a table.

    DB-wide (a group DB holds several projects; an entity may live in another
    repository than its repository interface). Idempotent: every run re-derives
    every ORM row, so a model that changed or disappeared in another file is
    picked up. A model resolves only when ALL the candidates agree on one
    (table, schema); two different tables behind one name stay unresolved.
    Returns the number of ORM rows examined."""
    by_name: dict[str, list[_ModelRow]] = {}
    for r in conn.execute(
        """SELECT m.name, m.role, m.mechanism, m.table_name, m.table_schema, m.entity,
                  m.table_source, f.project_id
           FROM data_model m JOIN file f ON f.id = m.file_id"""
    ):
        row = _ModelRow(
            r["name"],
            r["role"],
            r["mechanism"],
            r["table_name"],
            r["table_schema"],
            r["entity"],
            r["table_source"],
            int(r["project_id"]),
        )
        by_name.setdefault(row.name, []).append(row)

    updates: list[tuple[Any, ...]] = []
    examined = 0
    for a in conn.execute(
        """SELECT d.id, d.ref_name, d.ref_kind, d.mechanism, f.project_id
           FROM data_access d JOIN symbol s ON s.id = d.symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE d.ref_name IS NOT NULL"""
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
            cands = _pick(
                [r for r in by_name.get(model, []) if r.role == "model"], pid, a["mechanism"]
            )
            targets = {(r.table, r.schema) for r in cands if r.table}
            if len(targets) == 1:
                table, schema = next(iter(targets))
                sources = {r.source for r in cands if r.table}
                confidence = "convention" if sources == {"convention"} else "model"
        updates.append((model, table, schema, confidence, a["id"]))
    conn.executemany(
        "UPDATE data_access SET model=?, table_name=?, table_schema=?, confidence=? WHERE id=?",
        updates,
    )
    return examined


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
                        d.confidence, d.ref_name, s.qualified_name AS qname, s.id AS symbol_id,
                        f.path AS file_path, f.project_id, p.name AS project
                 FROM data_access d
                 JOIN symbol s ON s.id = d.symbol_id
                 JOIN file f ON f.id = s.file_id
                 JOIN project p ON p.id = f.project_id"""


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
    if r["model"] or r["ref_name"]:
        out["model"] = r["model"] or r["ref_name"]
    if r["confidence"] != CONFIDENCE_LITERAL:
        out["confidence"] = r["confidence"]
    return out


def known_tables(conn: sqlite3.Connection, pids: list[int]) -> dict[str, str]:
    """lower-case table name -> display name, over accesses, model declarations
    and tables declared in indexed `.sql` files."""
    ph, args = _in(pids)
    names: dict[str, str] = {}
    for sql in (
        f"""SELECT DISTINCT d.table_name AS n FROM data_access d JOIN symbol s ON s.id=d.symbol_id
            JOIN file f ON f.id=s.file_id
            WHERE f.project_id IN ({ph}) AND d.table_name IS NOT NULL""",
        f"""SELECT DISTINCT m.table_name AS n FROM data_model m JOIN file f ON f.id=m.file_id
            WHERE f.project_id IN ({ph}) AND m.table_name IS NOT NULL""",
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
    """lower-case table -> [{project, file, line}] for every `.sql` CREATE TABLE."""
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
            {"project": r["project"], "file": f"{r['path']}:{r['start_line']}"}
        )
    return out


def table_accesses(
    conn: sqlite3.Connection,
    pids: list[int],
    schema: str | None,
    table: str,
    access: str | None,
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
    rows = conn.execute(
        f"{_ROW_SELECT} WHERE {where} ORDER BY f.path, d.line, d.access", params
    ).fetchall()
    counts = {k: 0 for k in ACCESS_KINDS}
    for r in rows:
        counts[r["access"]] += 1
    wanted = [r for r in rows if access is None or r["access"] == access]
    wanted.sort(key=lambda r: (_ORDER[r["access"]], r["file_path"], r["line"]))
    return [_row_payload(r, with_symbol=True) | _table_cols(r) for r in wanted], counts


def _table_cols(r: sqlite3.Row) -> dict[str, Any]:
    # Qualified only when the source said so; kept off the common row.
    return {"schema": r["table_schema"]} if r["table_schema"] else {}


def symbol_tables(
    conn: sqlite3.Connection,
    pids: list[int],
    symbol_id: int,
    *,
    include_callers: bool,
    access: str | None,
    table: tuple[str | None, str] | None,
) -> list[dict[str, Any]]:
    """Tables `symbol_id` touches; with `include_callers` also those reached
    through its callees up to ``CALLER_DEPTH`` hops, each row naming the callee
    that holds the access (`via`) and the hop count (`depth`). A symbol that is
    reached by several paths is reported at its shallowest depth."""
    # symbol id -> (hops from the root, first callee on the way there)
    holders: dict[int, tuple[int, int | None]] = {symbol_id: (0, None)}
    if include_callers:
        ph, _ = _in(sorted(INVOCATION_EDGE_TYPES))
        seen = {symbol_id}
        queue: deque[tuple[int, int]] = deque([(symbol_id, 0)])
        while queue:
            cur, depth = queue.popleft()
            if depth >= CALLER_DEPTH:
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
            row |= _row_payload(r, with_symbol=False)
            if depth:
                row["via"] = r["qname"]
                row["depth"] = depth
                row["file"] = f"{r['file_path']}:{r['line']}"
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
    conn: sqlite3.Connection, pids: list[int], access: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """(table rows, exact counts, unresolved ORM models).

    A table row carries read / write / ddl counts, the number of distinct
    symbols touching it, and ``status``: ``ok`` (accessed and declared in a
    `.sql` file), ``undeclared`` (accessed, no `.sql` CREATE TABLE indexed) or
    ``unaccessed`` (declared, nothing in the code reaches it). The kind filter
    narrows which tables are listed, never the per-table counts."""
    ph, args = _in(pids)
    declared = declared_tables(conn, pids)
    acc: dict[str, dict[str, Any]] = {}
    unresolved: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(
        f"""SELECT d.table_name, d.table_schema, d.access, d.mechanism, d.model, d.ref_name,
                   d.symbol_id
            FROM data_access d JOIN symbol s ON s.id=d.symbol_id JOIN file f ON f.id=s.file_id
            WHERE f.project_id IN ({ph})""",
        args,
    ):
        if r["table_name"] is None:
            model = r["model"] or r["ref_name"]
            u = unresolved.setdefault(
                (model, r["mechanism"]),
                {"model": model, "mechanism": r["mechanism"], "read": 0, "write": 0, "ddl": 0},
            )
            u[r["access"]] += 1
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
    for name in sorted(set(acc) | set(declared)):
        a = acc.get(name)
        status = "unaccessed" if a is None else "ok" if name in declared else "undeclared"
        row: dict[str, Any] = {
            "table": name,
            "read": a["read"] if a else 0,
            "write": a["write"] if a else 0,
            "ddl": a["ddl"] if a else 0,
            "symbols": len(a["symbols"]) if a else 0,
            "status": status,
        }
        if a and a["schemas"]:
            row["schema"] = (
                sorted(a["schemas"])[0] if len(a["schemas"]) == 1 else sorted(a["schemas"])
            )
        if a:
            row["mechanisms"] = sorted(a["mech"])
        if name in declared:
            row["declared_in"] = [d["file"] for d in declared[name]][:3]
        rows.append(row)

    counts = {
        "tables": len(rows),
        "read": sum(r["read"] for r in rows),
        "write": sum(r["write"] for r in rows),
        "ddl": sum(r["ddl"] for r in rows),
        "accessed_undeclared": sum(r["status"] == "undeclared" for r in rows),
        "declared_unaccessed": sum(r["status"] == "unaccessed" for r in rows),
        "unresolved_orm_accesses": sum(
            u["read"] + u["write"] + u["ddl"] for u in unresolved.values()
        ),
    }
    if access is not None:
        rows = [r for r in rows if r[access] > 0]
    unresolved_rows = sorted(unresolved.values(), key=lambda u: (u["model"] or "", u["mechanism"]))
    return rows, counts, unresolved_rows
