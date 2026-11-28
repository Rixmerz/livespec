"""``find_data_access``: which code touches which database table?

Thin MCP surface over ``domain/data_access_store.py``; the extraction itself
(SQL in strings, ORM and query-builder shapes) is ``domain/data_access.py``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.data_access_store import (
    ACCESS_KINDS,
    CALLER_DEPTH,
    declared_tables,
    inventory,
    known_tables,
    split_table_name,
    suggest_tables,
    symbol_tables,
    table_accesses,
)
from livespec_mcp.state import AppState, get_state
from livespec_mcp.tool_params import Cursor, Limit, SummaryOnly
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import _resolve_symbol, _workspace_note, symbol_not_found_error
from livespec_mcp.workspace_param import Workspace

_UNRESOLVED_HINT = (
    "{n} ORM access(es) name a model whose table the source does not state "
    "(Prisma models, JPA entities without @Table, ...): listed under "
    "unresolved_models, never guessed"
)


def _page(
    rows: list[dict[str, Any]], limit: int, cursor: int, summary_only: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {"limit": limit, "cursor": cursor}
    if summary_only:
        out["next_cursor"] = None
        return out
    out["rows"] = rows[cursor : cursor + limit]
    out["next_cursor"] = cursor + limit if cursor + limit < len(rows) else None
    return out


def compute_find_data_access(
    st: AppState,
    table: str | None = None,
    qname: str | None = None,
    access: str | None = None,
    include_callers: bool = False,
    *,
    limit: int = 200,
    cursor: int = 0,
    summary_only: bool = False,
) -> dict[str, Any]:
    """The ``find_data_access`` payload, or an ``mcp_error``."""
    pids = st.group_project_ids()
    if access is not None:
        access = access.strip().lower()
        if access not in ACCESS_KINDS:
            return mcp_error(
                f"Unknown access '{access}': expected read, write or ddl",
                did_you_mean=list(ACCESS_KINDS),
            )
    wanted = split_table_name(table) if table is not None else None
    if wanted is not None and not wanted[1]:
        return mcp_error("table is empty", hint="omit table for the inventory")
    if wanted is not None and wanted[1] not in known_tables(st.conn, pids):
        return mcp_error(
            f"Table '{table}' is not accessed, modelled or declared anywhere in this workspace",
            did_you_mean=suggest_tables(st.conn, pids, table or "") or None,
            hint="omit table for the inventory of every table the code touches",
        )

    if qname:
        sym = _resolve_symbol(st.conn, pids, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pids, qname)
        rows = symbol_tables(
            st.conn,
            pids,
            int(sym["id"]),
            include_callers=include_callers,
            access=access,
            table=wanted,
        )
        direct = [r for r in rows if "via" not in r]
        counts = {
            "total": len(rows),
            "direct": len(direct),
            "via_callees": len(rows) - len(direct),
            "tables": len({r["table"] for r in rows if r["table"]}),
            "unresolved": sum(r["table"] is None for r in rows),
            **{k: sum(r["kind"] == k for r in rows) for k in ACCESS_KINDS},
        }
        payload: dict[str, Any] = {
            "mode": "symbol",
            "symbol": sym["qualified_name"],
            "location": f"{sym['file_path']}:{sym['start_line']}",
            "include_callers": include_callers,
            "counts": counts,
        }
        if include_callers:
            payload["caller_depth"] = CALLER_DEPTH
        page = _page(rows, limit, cursor, summary_only)
        if "rows" in page:
            payload["tables"] = page.pop("rows")
        payload.update(page)
        if not rows:
            payload["hint"] = "no table access found in this symbol" + (
                "" if include_callers else "; include_callers=True also follows its callees"
            )
        return payload

    if wanted is not None:
        rows, by_kind = table_accesses(st.conn, pids, wanted[0], wanted[1], access)
        declared = declared_tables(st.conn, pids).get(wanted[1], [])
        payload = {
            "mode": "table",
            "table": wanted[1],
            "counts": {
                "total": sum(by_kind.values()),
                **by_kind,
                "symbols": len({r["symbol"] for r in rows}) if access is None else None,
                "matched": len(rows),
            },
            "declared_in": declared,
        }
        if payload["counts"]["symbols"] is None:
            del payload["counts"]["symbols"]
        if wanted[0]:
            payload["schema"] = wanted[0]
        page = _page(rows, limit, cursor, summary_only)
        if "rows" in page:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for r in page.pop("rows"):
                grouped.setdefault(r["kind"], []).append(
                    {k: v for k, v in r.items() if k != "kind"}
                )
            payload["accesses"] = grouped
        payload.update(page)
        if not sum(by_kind.values()):
            payload["hint"] = "declared in a .sql file but no indexed code reads or writes it"
        return payload

    rows, counts, unresolved = inventory(st.conn, pids, access)
    payload = {"mode": "inventory", "counts": counts}
    page = _page(rows, limit, cursor, summary_only)
    if "rows" in page:
        payload["tables"] = page.pop("rows")
    payload.update(page)
    payload["counts"]["listed"] = len(rows)
    if unresolved:
        payload["unresolved_models"] = unresolved[:50]
        payload["counts"]["unresolved_models"] = len(unresolved)
        payload["hint"] = _UNRESOLVED_HINT.format(n=counts["unresolved_orm_accesses"])
    if not rows and not unresolved:
        payload["hint"] = "no table access found: re-run index_project if this repo uses a database"
    return payload


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def find_data_access(
        table: str | None = None,
        qname: str | None = None,
        access: str | None = None,
        include_callers: bool = False,
        limit: Limit = 200,
        cursor: Cursor = 0,
        summary_only: SummaryOnly = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Which code touches which database table? Call it BEFORE changing a
        table (column, rename, drop) or a function that persists data, and as
        a first call on an unknown repo to see its tables. Read at index time
        with no annotation, for Python, TypeScript/JavaScript and Java.

        Recognised: SQL (and JPQL) in string literals (multi-line, f-string /
        template holes, concatenated literals; SELECT/JOIN = read, INSERT /
        UPDATE / DELETE / MERGE / REPLACE = write, CREATE/ALTER/DROP TABLE =
        ddl; CTE names are not tables; prose is ignored), and ORM calls in
        unambiguous shapes: SQLAlchemy, Django, Prisma, Knex, Supabase,
        TypeORM, Sequelize, JPA / Spring Data. A table is reported only when
        the source states it (``__tablename__``, ``Meta.db_table``, ``@Table``,
        a string argument) or follows Django's naming convention
        (``confidence: convention``); ``confidence: unresolved`` means the
        model is known and its table is not. Not seen: dynamically built
        names, raw SQL passed from a variable defined elsewhere, ORM calls
        through an untyped parameter.

        Three modes. ``table`` (any case, schema optional): who touches it,
        grouped by kind (write, ddl, read) with ``file:line`` and mechanism.
        ``qname``: the tables that symbol touches; ``include_callers=True`` also
        those reached through its callees (3 hops), each row naming the callee
        in ``via``. Neither: the inventory with exact read / write / ddl counts
        per table, joined with ``.sql`` CREATE TABLE files (``status``: ``ok``
        | ``undeclared`` | ``unaccessed``). ``access`` keeps one kind.
        Unknown table: ``did_you_mean``. ``limit`` / ``cursor`` page the rows;
        counts are exact; ``summary_only`` drops the rows."""
        st = get_state(workspace)
        return compute_find_data_access(
            st,
            table,
            qname,
            access,
            include_callers,
            limit=limit,
            cursor=cursor,
            summary_only=summary_only,
        )
