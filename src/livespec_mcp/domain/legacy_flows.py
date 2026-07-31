"""Detect likely-unused HTTP flows from ``route_ref`` + ``invokes_route``.

Primary signal (polyrepo / ``group_db``): a **server** route with zero
incoming ``invokes_route`` edges from indexed clients. That is *graph*
evidence ("nothing in this index calls this path"), not production traffic.

Also surfaces **client** routes with no matching server hop (dead front
call or missing SA in the group).
"""

from __future__ import annotations

import sqlite3
from typing import Any

# Shared with Flow Explorer — infra / docs / UI paths cross-match every service
# and are not product flows. Exact match OR prefix (see ``is_infra_route_path``).
INFRA_ROUTE_PATHS = frozenset({
    "/health",
    "/liveness",
    "/readiness",
    "/ready",
    "/ping",
    "/metrics",
    "/actuator/health",
    "/actuator/info",
    # Docs / operator UI (audit: dominated legacy_server noise on a real polyrepo)
    "/api-docs",
    "/v3/api-docs",
    "/openapi.yaml",
    "/openapi.json",
    "/swagger",
    "/swagger-ui",
    "/ui",
    "/playground",
    "/info",
})

# Prefixes: `/metrics/cache`, `/actuator/...`, swagger subpaths.
INFRA_ROUTE_PREFIXES = frozenset({
    "/metrics/",
    "/actuator/",
    "/api-docs/",
    "/v3/api-docs/",
    "/swagger-ui/",
    "/swagger/",
})

_HINT = (
    "Likely-unused = no invokes_route hop from indexed clients in this DB. "
    "Incomplete client extract or missing repos → false positives. "
    "Confirm with traffic (APM/logs) before deleting."
)


def _norm_display_path(path: str | None) -> str:
    raw = path or "/"
    return (raw.split("?", 1)[0].rstrip("/") or "/").lower()


def is_infra_route_path(path: str | None) -> bool:
    """True for health/metrics/docs/UI routes that are not product flows."""
    n = _norm_display_path(path)
    if n in INFRA_ROUTE_PATHS:
        return True
    return any(n.startswith(p) for p in INFRA_ROUTE_PREFIXES)


def live_server_symbol_ids(conn: sqlite3.Connection) -> set[int]:
    """Server symbol ids that are ``dst`` of at least one ``invokes_route``."""
    try:
        rows = conn.execute(
            """SELECT DISTINCT edge.dst_symbol_id AS sid
               FROM symbol_edge edge
               WHERE edge.edge_type='invokes_route'"""
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {int(r["sid"]) for r in rows}


def live_client_symbol_ids(conn: sqlite3.Connection) -> set[int]:
    """Client symbol ids that are ``src`` of at least one ``invokes_route``."""
    try:
        rows = conn.execute(
            """SELECT DISTINCT edge.src_symbol_id AS sid
               FROM symbol_edge edge
               WHERE edge.edge_type='invokes_route'"""
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {int(r["sid"]) for r in rows}


def _list_server_routes(
    conn: sqlite3.Connection,
    *,
    project_name: str | None,
    include_infra: bool,
) -> list[dict[str, Any]]:
    sql = """
        SELECT rr.method, rr.path, rr.norm_path, rr.symbol_id,
               s.qualified_name, s.start_line, s.end_line,
               f.path AS file_path, p.name AS project_name, p.root AS project_root
        FROM route_ref rr
        JOIN symbol s ON s.id=rr.symbol_id
        JOIN file f ON f.id=s.file_id
        JOIN project p ON p.id=f.project_id
        WHERE rr.role='server'
    """
    params: list[Any] = []
    if project_name:
        sql += " AND (p.name=? OR p.root LIKE ?)"
        params.extend([project_name, f"%/{project_name}"])
    sql += " ORDER BY p.name, rr.norm_path, rr.method, s.qualified_name"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        path = r["path"] or r["norm_path"] or "/"
        if not include_infra and is_infra_route_path(path):
            continue
        out.append({
            "kind": "server",
            "project": r["project_name"],
            "project_root": r["project_root"],
            "qualified_name": r["qualified_name"],
            "symbol_id": int(r["symbol_id"]),
            "file_path": r["file_path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "method": r["method"],
            "path": r["path"],
            "norm_path": r["norm_path"],
        })
    return out


def _list_client_routes(
    conn: sqlite3.Connection,
    *,
    project_name: str | None,
    include_infra: bool,
) -> list[dict[str, Any]]:
    sql = """
        SELECT rr.method, rr.path, rr.norm_path, rr.symbol_id,
               s.qualified_name, s.start_line, s.end_line,
               f.path AS file_path, p.name AS project_name, p.root AS project_root
        FROM route_ref rr
        JOIN symbol s ON s.id=rr.symbol_id
        JOIN file f ON f.id=s.file_id
        JOIN project p ON p.id=f.project_id
        WHERE rr.role='client'
    """
    params: list[Any] = []
    if project_name:
        sql += " AND (p.name=? OR p.root LIKE ?)"
        params.extend([project_name, f"%/{project_name}"])
    sql += " ORDER BY p.name, rr.norm_path, rr.method, s.qualified_name"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        path = r["path"] or r["norm_path"] or "/"
        if not include_infra and is_infra_route_path(path):
            continue
        out.append({
            "kind": "client",
            "project": r["project_name"],
            "project_root": r["project_root"],
            "qualified_name": r["qualified_name"],
            "symbol_id": int(r["symbol_id"]),
            "file_path": r["file_path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "method": r["method"],
            "path": r["path"],
            "norm_path": r["norm_path"],
        })
    return out


def compute_legacy_flows(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    include_infra: bool = False,
    include_orphan_clients: bool = True,
) -> dict[str, Any]:
    """Set-diff server/client ``route_ref`` against live ``invokes_route`` hops."""
    live_servers = live_server_symbol_ids(conn)
    live_clients = live_client_symbol_ids(conn)
    servers = _list_server_routes(
        conn, project_name=project, include_infra=include_infra
    )
    clients = _list_client_routes(
        conn, project_name=project, include_infra=include_infra
    )

    legacy_servers: list[dict[str, Any]] = []
    for row in servers:
        if row["symbol_id"] in live_servers:
            continue
        item = {k: v for k, v in row.items() if k != "symbol_id"}
        item["reason"] = "no_indexed_client_hop"
        item["confidence"] = "low"
        legacy_servers.append(item)

    orphan_clients: list[dict[str, Any]] = []
    if include_orphan_clients:
        for row in clients:
            if row["symbol_id"] in live_clients:
                continue
            item = {k: v for k, v in row.items() if k != "symbol_id"}
            item["reason"] = "no_indexed_server_hop"
            item["confidence"] = "low"
            orphan_clients.append(item)

    return {
        "server_route_count": len(servers),
        "client_route_count": len(clients),
        "live_server_count": len(live_servers),
        "live_client_count": len(live_clients),
        "legacy_server_count": len(legacy_servers),
        "orphan_client_count": len(orphan_clients),
        "legacy_servers": legacy_servers,
        "orphan_clients": orphan_clients,
        "hint": _HINT,
    }
