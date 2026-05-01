"""Analysis tools.

P1.2 consolidation: `find_references` removed — use
`analyze_impact(target_type='symbol', target=qname, max_depth=1)` and read
the `impacted_callers` list (matches the old shape).
v0.3 P1.1 adds `git_diff_impact` for CI/PR-review use cases.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.domain.graph import (
    ancestors_within,
    descendants_within,
    load_graph,
    page_rank,
    subgraph_edges,
)
from livespec_mcp.state import get_state
from livespec_mcp.tools._errors import mcp_error


_INFRA_NAME_SUFFIXES = ("_state", "_settings", "_config", "_session")

# v0.5 P1: framework decorator names that imply hidden callers (HTTP routers,
# CLI dispatchers, test frameworks, plugin systems, message brokers, MCP).
# We match on the LAST dotted segment so `app.route`, `router.get`,
# `bp.before_request`, `mcp.tool` all qualify. Keep this list short and well-
# known; users can opt out via include_infrastructure=True.
_ENTRY_POINT_DECORATOR_LASTSEG = frozenset({
    # HTTP verbs (Flask/FastAPI/Bottle/etc.)
    "route", "get", "post", "put", "delete", "patch", "head", "options",
    "api_route", "websocket",
    # Flask/FastAPI hooks
    "before_request", "after_request", "errorhandler", "teardown_appcontext",
    "before_first_request", "context_processor",
    # CLI dispatchers
    "command", "group",
    # Task brokers
    "task", "shared_task",
    # Test frameworks
    "fixture",
    # FastMCP / Anthropic agent SDK
    "tool", "resource", "prompt",
    # Plugin systems / event dispatch
    "hookimpl", "event", "event_handler", "handler", "listener",
    # Cron / schedules
    "cron", "schedule", "scheduled",
})

# Per-framework decorator presets for `find_endpoints(framework=...)`.
_FRAMEWORK_DECORATOR_PATTERNS: dict[str, tuple[str, ...]] = {
    "flask": (
        "route", "get", "post", "put", "delete", "patch",
        "before_request", "after_request", "errorhandler",
    ),
    "fastapi": (
        "route", "get", "post", "put", "delete", "patch", "head", "options",
        "api_route", "websocket",
    ),
    "click": ("command", "group"),
    "pytest": ("fixture",),
    "fastmcp": ("tool", "resource", "prompt"),
    "celery": ("task", "shared_task"),
    "django": ("login_required", "permission_required", "staff_member_required"),
}


def _decorator_lastseg(name: str) -> str:
    """Return the last dotted segment of a decorator name, lowercase."""
    return name.rsplit(".", 1)[-1].lower()


def _has_entry_point_decorator(decorators_json: str | None) -> bool:
    if not decorators_json:
        return False
    try:
        names = json.loads(decorators_json)
    except (json.JSONDecodeError, TypeError):
        return False
    return any(_decorator_lastseg(n) in _ENTRY_POINT_DECORATOR_LASTSEG for n in names)


def _decorator_matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    """True if `name` equals or has-as-last-segment any of `patterns`."""
    last = _decorator_lastseg(name)
    return last in {p.lower() for p in patterns}


def _is_implicit_entry_point(meta: dict) -> bool:
    """Stricter subset of `_is_infrastructure`: only the cases where a symbol
    has invisible callers (Python protocol dunders, FastMCP `register`, DI
    helpers). Excludes the tiny-wrapper rule because a 1-line wrapper that
    nobody calls IS a dead-code candidate."""
    name = meta.get("name") or ""
    qname = meta.get("qualified_name") or ""
    kind = meta.get("kind") or ""
    if name.startswith("__") and name.endswith("__"):
        return True
    if any(seg.startswith("__") and seg.endswith("__") for seg in qname.split(".")):
        return True
    if name == "register" and kind == "function":
        return True
    if kind in ("function", "method") and any(
        name.endswith(suf) for suf in _INFRA_NAME_SUFFIXES
    ):
        return True
    return False


def _is_infrastructure(meta: dict) -> bool:
    """Heuristic for symbols that rank high by PageRank but carry little
    semantic weight: DI helpers, FastMCP `register` outers, dunders, tiny
    wrappers. P0.3."""
    qname = meta.get("qualified_name") or ""
    name = meta.get("name") or ""
    kind = meta.get("kind") or ""
    start = meta.get("start_line") or 0
    end = meta.get("end_line") or 0
    line_count = max(0, end - start)

    # Dunders (anywhere in the name path, e.g. Foo.__init__)
    if name.startswith("__") and name.endswith("__"):
        return True
    if any(seg.startswith("__") and seg.endswith("__") for seg in qname.split(".")):
        return True
    # FastMCP `register` outer functions live at module scope and contain inner tools.
    if name == "register" and kind == "function":
        return True
    # Common DI / config helpers
    if kind in ("function", "method") and any(name.endswith(suf) for suf in _INFRA_NAME_SUFFIXES):
        return True
    # One-line wrappers: function/method whose body is shorter than 5 lines
    if kind in ("function", "method") and 0 < line_count < 5:
        return True
    return False


def _resolve_symbol(conn, project_id: int, identifier: str) -> dict | None:
    """Resolve a symbol by qualified_name (exact) or short name (best match)."""
    row = conn.execute(
        """SELECT s.*, f.path as file_path FROM symbol s
           JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
        (project_id, identifier),
    ).fetchone()
    if row:
        return dict(row)
    rows = conn.execute(
        """SELECT s.*, f.path as file_path FROM symbol s
           JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND s.name=? LIMIT 5""",
        (project_id, identifier),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    return None


def did_you_mean_symbols(conn, project_id: int, identifier: str, limit: int = 3) -> list[dict]:
    """Top-N symbol suggestions for a misspelled or partial identifier.

    Used by tools that raise 'Symbol not found' to surface likely intended
    targets in the error payload (P2.D3). Combines two passes:
      1. SQL substring match on name / qualified_name (catches partials,
         prefix mistypes).
      2. difflib SequenceMatcher ratio on the short name (catches typos
         where the substring path doesn't fire — e.g. 'logn' ≈ 'login').
    Ranked by ratio descending. Project-scoped.
    """
    short = identifier.split(".")[-1]
    needle = f"%{short}%"
    rows = conn.execute(
        """SELECT s.qualified_name, s.kind, f.path AS file_path, s.name
           FROM symbol s JOIN file f ON f.id=s.file_id
           WHERE f.project_id=?""",
        (project_id,),
    ).fetchall()
    if not rows:
        return []

    name_to_rows: dict[str, list] = {}
    for r in rows:
        name_to_rows.setdefault(r["name"], []).append(r)

    candidates = list(name_to_rows.keys())
    matches = difflib.get_close_matches(short, candidates, n=limit * 2, cutoff=0.55)

    seen: set[str] = set()
    out: list[dict] = []
    short_lower = short.lower()
    # Substring hits first (treated as ratio=0.99 for ranking ties)
    for r in rows:
        if len(out) >= limit:
            break
        if short_lower in (r["name"] or "").lower() or short_lower in (r["qualified_name"] or "").lower():
            qn = r["qualified_name"]
            if qn in seen:
                continue
            seen.add(qn)
            out.append(
                {"qualified_name": qn, "kind": r["kind"], "file_path": r["file_path"]}
            )
    for m in matches:
        if len(out) >= limit:
            break
        for r in name_to_rows.get(m, []):
            qn = r["qualified_name"]
            if qn in seen:
                continue
            seen.add(qn)
            out.append(
                {"qualified_name": qn, "kind": r["kind"], "file_path": r["file_path"]}
            )
            if len(out) >= limit:
                break
    return out


def symbol_not_found_error(conn, project_id: int, identifier: str) -> dict:
    """Build the standard 'Symbol not found' error payload with did_you_mean."""
    return mcp_error(
        f"Symbol '{identifier}' not found",
        did_you_mean=did_you_mean_symbols(conn, project_id, identifier),
        hint="run `find_symbol(query=<short_name>)` to discover qualified names",
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_symbol(
        query: str,
        kind: str | None = None,
        limit: int = 50,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Search symbols by name substring or qualified name.

        Returns lightweight refs (qualified_name, file, line, signature, kind).
        Use `get_symbol_info` for full details on a single match.
        """
        st = get_state(workspace)
        pid = st.project_id
        sql = [
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.signature,
                      s.start_line, s.end_line, f.path as file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND (s.name LIKE ? OR s.qualified_name LIKE ?)"""
        ]
        like = f"%{query}%"
        args: list[Any] = [pid, like, like]
        if kind:
            sql.append("AND s.kind = ?")
            args.append(kind)
        sql.append("ORDER BY length(s.qualified_name) LIMIT ?")
        args.append(limit)
        rows = st.conn.execute(" ".join(sql), args).fetchall()
        return {"matches": [dict(r) for r in rows]}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_symbol_info(
        identifier: str,
        detail: Literal["summary", "full"] = "summary",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Detail for a single symbol by qualified_name (preferred) or short name.

        `summary`: metadata + counts. `full`: also includes source body, callers,
        callees, and linked RFs.
        """
        st = get_state(workspace)
        pid = st.project_id
        sym = _resolve_symbol(st.conn, pid, identifier)
        if not sym:
            return symbol_not_found_error(st.conn, pid, identifier)
        callers_n = st.conn.execute(
            "SELECT COUNT(*) c FROM symbol_edge WHERE dst_symbol_id=? AND edge_type='calls'",
            (sym["id"],),
        ).fetchone()["c"]
        callees_n = st.conn.execute(
            "SELECT COUNT(*) c FROM symbol_edge WHERE src_symbol_id=? AND edge_type='calls'",
            (sym["id"],),
        ).fetchone()["c"]
        rfs = st.conn.execute(
            """SELECT r.rf_id, r.title, rs.relation, rs.confidence
               FROM rf_symbol rs JOIN rf r ON r.id=rs.rf_id WHERE rs.symbol_id=?""",
            (sym["id"],),
        ).fetchall()
        out: dict[str, Any] = {
            "id": sym["id"],
            "name": sym["name"],
            "qualified_name": sym["qualified_name"],
            "kind": sym["kind"],
            "signature": sym["signature"],
            "docstring": sym["docstring"],
            "file_path": sym["file_path"],
            "start_line": sym["start_line"],
            "end_line": sym["end_line"],
            "body_hash": sym["body_hash"],
            "callers_count": int(callers_n),
            "callees_count": int(callees_n),
            "requirements": [dict(r) for r in rfs],
        }
        if detail == "full":
            callers = st.conn.execute(
                """SELECT s.qualified_name, f.path, s.start_line
                   FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id
                   JOIN file f ON f.id=s.file_id
                   WHERE e.dst_symbol_id=? AND e.edge_type='calls' LIMIT 200""",
                (sym["id"],),
            ).fetchall()
            callees = st.conn.execute(
                """SELECT s.qualified_name, f.path, s.start_line
                   FROM symbol_edge e JOIN symbol s ON s.id=e.dst_symbol_id
                   JOIN file f ON f.id=s.file_id
                   WHERE e.src_symbol_id=? AND e.edge_type='calls' LIMIT 200""",
                (sym["id"],),
            ).fetchall()
            out["callers"] = [dict(r) for r in callers]
            out["callees"] = [dict(r) for r in callees]
            try:
                fp = st.settings.workspace / sym["file_path"]
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                start = max(sym["start_line"] - 1, 0)
                end = min(sym["end_line"], len(lines))
                out["source"] = "\n".join(lines[start:end])
            except OSError:
                out["source"] = None
        return out

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_call_graph(
        identifier: str,
        direction: Literal["forward", "backward", "both"] = "both",
        max_depth: int = 3,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Subgraph of calls around a symbol up to `max_depth`.

        forward = what this calls; backward = what calls this; both = union.
        """
        st = get_state(workspace)
        pid = st.project_id
        sym = _resolve_symbol(st.conn, pid, identifier)
        if not sym:
            return symbol_not_found_error(st.conn, pid, identifier)
        view = load_graph(st.conn, pid)
        sid = int(sym["id"])
        if sid not in view.g:
            return {"nodes": [], "edges": [], "root": sym["qualified_name"]}
        nodes: set[int] = {sid}
        if direction in ("forward", "both"):
            nodes |= descendants_within(view.g, sid, max_depth)
        if direction in ("backward", "both"):
            nodes |= ancestors_within(view.g, sid, max_depth)
        return {
            "root": sym["qualified_name"],
            "nodes": [view.sym_meta[n] for n in nodes if n in view.sym_meta],
            "edges": subgraph_edges(view, nodes),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def analyze_impact(
        target_type: Literal["symbol", "file", "requirement"],
        target: str,
        max_depth: int = 5,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Topological impact analysis: what changes if `target` changes.

        - symbol: backward cone of callers + RFs that touch any reached symbol.
          Set max_depth=1 to get the equivalent of a "find references".
        - file:   union of impacts from every symbol in the file.
        - requirement: forward cone from every symbol implementing the RF + their callers.
        """
        st = get_state(workspace)
        pid = st.project_id
        view = load_graph(st.conn, pid)

        def rfs_for_symbols(ids: set[int]) -> list[dict]:
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            return [
                dict(r)
                for r in st.conn.execute(
                    f"""SELECT DISTINCT r.rf_id, r.title, r.status, r.priority
                        FROM rf_symbol rs JOIN rf r ON r.id=rs.rf_id
                        WHERE rs.symbol_id IN ({placeholders})""",
                    list(ids),
                ).fetchall()
            ]

        if target_type == "symbol":
            sym = _resolve_symbol(st.conn, pid, target)
            if not sym:
                return symbol_not_found_error(st.conn, pid, target)
            sid = int(sym["id"])
            impacted = ancestors_within(view.g, sid, max_depth) if sid in view.g else set()
            forward = descendants_within(view.g, sid, max_depth) if sid in view.g else set()
            return {
                "root": sym["qualified_name"],
                "impacted_callers": [view.sym_meta[n] for n in impacted if n in view.sym_meta],
                "calls_into": [view.sym_meta[n] for n in forward if n in view.sym_meta],
                "affected_requirements": rfs_for_symbols(impacted | {sid}),
            }
        if target_type == "file":
            sids = [
                int(r["id"])
                for r in st.conn.execute(
                    """SELECT s.id FROM symbol s JOIN file f ON f.id=s.file_id
                       WHERE f.project_id=? AND f.path=?""",
                    (pid, target),
                )
            ]
            if not sids:
                return mcp_error(
                    f"File '{target}' not indexed",
                    hint="run `index_project()` or check `list_files(path_glob=...)` for the correct path",
                )
            impacted: set[int] = set()
            for sid in sids:
                if sid in view.g:
                    impacted |= ancestors_within(view.g, sid, max_depth)
            impacted -= set(sids)
            return {
                "file": target,
                "symbols_in_file": len(sids),
                "impacted_callers": [view.sym_meta[n] for n in impacted if n in view.sym_meta],
                "affected_requirements": rfs_for_symbols(impacted | set(sids)),
            }
        if target_type == "requirement":
            rf = st.conn.execute(
                "SELECT id, rf_id FROM rf WHERE project_id=? AND rf_id=?", (pid, target)
            ).fetchone()
            if not rf:
                return mcp_error(
                    f"RF '{target}' not found",
                    hint="check `list_requirements()` for known RF ids",
                )

            # v0.5 P2: include backward RFs in the dependency graph (RFs that
            # require / extend this one). A change to RF-001 ripples to RF-042
            # if RF-042 requires RF-001. Walk rf_dependency backward.
            dependent_rf_ids: set[int] = set()
            frontier = [int(rf["id"])]
            while frontier:
                cur_id = frontier.pop()
                for r in st.conn.execute(
                    "SELECT parent_rf_id FROM rf_dependency WHERE child_rf_id=?",
                    (cur_id,),
                ):
                    pid_dep = int(r["parent_rf_id"])
                    if pid_dep in dependent_rf_ids:
                        continue
                    dependent_rf_ids.add(pid_dep)
                    frontier.append(pid_dep)

            # All RF ids whose impact contributes to this analysis: target +
            # the set of RFs that transitively depend on it (cascade).
            all_rf_ids = {int(rf["id"])} | dependent_rf_ids
            placeholders = ",".join("?" * len(all_rf_ids))
            sid_rows = st.conn.execute(
                f"SELECT DISTINCT symbol_id FROM rf_symbol WHERE rf_id IN ({placeholders})",
                list(all_rf_ids),
            ).fetchall()
            sids = [int(r["symbol_id"]) for r in sid_rows]

            if not sids:
                return {
                    "rf_id": rf["rf_id"],
                    "warning": "RF (and its dependents) have no linked symbols",
                    "implementing_symbols": [],
                    "dependent_requirements": [],
                }
            forward: set[int] = set()
            backward: set[int] = set()
            for sid in sids:
                if sid in view.g:
                    forward |= descendants_within(view.g, sid, max_depth)
                    backward |= ancestors_within(view.g, sid, max_depth)

            dep_rf_meta: list[dict[str, Any]] = []
            if dependent_rf_ids:
                dep_placeholders = ",".join("?" * len(dependent_rf_ids))
                dep_rf_meta = [
                    dict(r)
                    for r in st.conn.execute(
                        f"""SELECT rf_id, title, status, priority FROM rf
                            WHERE id IN ({dep_placeholders})""",
                        list(dependent_rf_ids),
                    )
                ]

            return {
                "rf_id": rf["rf_id"],
                "dependent_requirements": dep_rf_meta,
                "implementing_symbols": [view.sym_meta[n] for n in sids if n in view.sym_meta],
                "downstream": [view.sym_meta[n] for n in forward if n in view.sym_meta],
                "upstream_callers": [view.sym_meta[n] for n in backward if n in view.sym_meta],
            }
        return mcp_error(
            f"Unknown target_type '{target_type}'",
            hint="target_type must be one of: 'symbol', 'file', 'requirement'",
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_project_overview(
        include_infrastructure: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """High-level snapshot: languages, modules, top symbols by PageRank, RF coverage.

        By default the top-symbols list filters out infrastructure noise (DI
        helpers, FastMCP `register` outer fns, dunders, one-line wrappers).
        Pass `include_infrastructure=True` to see the unfiltered ranking.
        """
        st = get_state(workspace)
        pid = st.project_id
        langs = [
            dict(r)
            for r in st.conn.execute(
                "SELECT language, COUNT(*) files FROM file WHERE project_id=? GROUP BY language",
                (pid,),
            )
        ]
        view = load_graph(st.conn, pid)
        ranks = page_rank(view.g)
        ordered = sorted(ranks.items(), key=lambda x: x[1], reverse=True)
        top_syms: list[dict[str, Any]] = []
        for sid, score in ordered:
            meta = view.sym_meta.get(sid)
            if meta is None:
                continue
            if not include_infrastructure and _is_infrastructure(meta):
                continue
            top_syms.append({**meta, "pagerank": round(score, 6)})
            if len(top_syms) >= 20:
                break
        rf_total = st.conn.execute(
            "SELECT COUNT(*) c FROM rf WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        rf_linked = st.conn.execute(
            """SELECT COUNT(DISTINCT r.id) c FROM rf r
               JOIN rf_symbol rs ON rs.rf_id=r.id WHERE r.project_id=?""",
            (pid,),
        ).fetchone()["c"]
        return {
            "workspace": str(st.settings.workspace),
            "languages": langs,
            "top_symbols": top_syms,
            "requirements_total": int(rf_total),
            "requirements_linked": int(rf_linked),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_dead_code(
        include_infrastructure: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Symbols with zero callers and zero RF links — removal candidates.

        Filters out, by default:
        - Files under `tests/`, `scripts/`, `bin/`; `__main__.py`; `manage.py`
        - Infrastructure (DI helpers, dunders, FastMCP `register` fns, ≤4-line
          wrappers). Pass `include_infrastructure=True` to keep them.

        Useful sanity check before a refactor: anything in the result is unreachable
        from in-project callers AND not traceably implementing any RF.
        """
        st = get_state(workspace)
        pid = st.project_id
        rows = st.conn.execute(
            """SELECT s.id, s.qualified_name, s.name, s.kind, s.decorators,
                      s.start_line, s.end_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=?
                 AND NOT EXISTS (
                   SELECT 1 FROM symbol_edge e WHERE e.dst_symbol_id=s.id
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM rf_symbol rs WHERE rs.symbol_id=s.id
                 )
               ORDER BY f.path, s.start_line""",
            (pid,),
        ).fetchall()

        def is_entry_point_path(p: str) -> bool:
            return (
                p.startswith(("tests/", "bin/", "scripts/"))
                or "/tests/" in p
                or "/bin/" in p
                or "/scripts/" in p
                or p.endswith("/__main__.py")
                or p == "__main__.py"
                or p.endswith("/manage.py")
                or p == "manage.py"
            )

        dead: list[dict[str, Any]] = []
        for r in rows:
            meta = dict(r)
            if is_entry_point_path(meta["file_path"]):
                continue
            if not include_infrastructure and _is_implicit_entry_point(meta):
                continue
            # v0.5 P1: framework-decorated symbols have invisible callers
            # (HTTP requests, CLI invocation, pytest collection, message
            # brokers, MCP). Skip unless explicitly opted in.
            if not include_infrastructure and _has_entry_point_decorator(
                meta.get("decorators")
            ):
                continue
            dead.append({
                "qualified_name": meta["qualified_name"],
                "kind": meta["kind"],
                "file_path": meta["file_path"],
                "start_line": meta["start_line"],
                "end_line": meta["end_line"],
            })
        return {"dead_symbols": dead, "count": len(dead)}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_endpoints(
        framework: Literal[
            "flask", "fastapi", "click", "pytest", "fastmcp", "celery", "django"
        ] | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Symbols decorated with framework entry-point markers.

        Useful as a reverse-engineering aid: "what HTTP routes does this app
        expose?", "what CLI commands does this script support?", "which
        pytest fixtures live in this repo?".

        Pass `framework=None` (default) to surface every recognized
        entry-point decorator across the project. Pass a specific framework
        to filter to its decorator set (matched against the LAST dotted
        segment of each decorator, so aliasing like `from flask import Flask
        as App; @App().route(...)` still resolves).
        """
        st = get_state(workspace)
        pid = st.project_id

        rows = st.conn.execute(
            """SELECT s.qualified_name, s.kind, s.decorators, s.start_line, s.end_line,
                      f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.decorators IS NOT NULL
               ORDER BY f.path, s.start_line""",
            (pid,),
        ).fetchall()

        if framework is not None:
            patterns = _FRAMEWORK_DECORATOR_PATTERNS.get(framework, ())

            def keep(decs: list[str]) -> list[str]:
                return [d for d in decs if _decorator_matches_any(d, patterns)]
        else:
            def keep(decs: list[str]) -> list[str]:
                return [d for d in decs if _decorator_lastseg(d) in _ENTRY_POINT_DECORATOR_LASTSEG]

        endpoints: list[dict[str, Any]] = []
        for r in rows:
            try:
                decs = json.loads(r["decorators"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            matching = keep(decs)
            if not matching:
                continue
            endpoints.append({
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file_path": r["file_path"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "decorators": matching,
            })
        return {
            "framework": framework,
            "endpoints": endpoints,
            "count": len(endpoints),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def audit_coverage(workspace: str | None = None) -> dict[str, Any]:
        """RF coverage audit: what's missing / under-confident.

        Five signals:
        - `modules_without_rf`: files whose symbols have no DIRECT `rf_symbol` link
        - `modules_implicitly_covered`: subset of `modules_without_rf` whose
          symbols are called transitively by an rf-linked symbol — covered
          indirectly through the call graph (e.g. a data layer reached via
          API handlers that carry the `@rf:` annotation)
        - `modules_truly_orphan`: subset of `modules_without_rf` with NO direct
          link AND no transitive coverage — the actually-actionable list
        - `rfs_without_implementation`: RFs with no `rf_symbol` row at all
        - `rfs_low_confidence`: RFs whose avg(rf_symbol.confidence) < 0.7
          (typically means only verb-anchored matches, no `@rf:` annotation)
        """
        st = get_state(workspace)
        pid = st.project_id

        modules_no_rf = [
            r["path"]
            for r in st.conn.execute(
                """SELECT f.path FROM file f
                   WHERE f.project_id=?
                     AND NOT EXISTS (
                       SELECT 1 FROM symbol s
                       JOIN rf_symbol rs ON rs.symbol_id=s.id
                       WHERE s.file_id=f.id
                     )
                   ORDER BY f.path""",
                (pid,),
            )
        ]

        # Split direct-orphan into implicitly-covered vs truly-orphan via the
        # call graph: a file is implicitly covered if any of its symbols has
        # an rf-linked symbol in its ancestor cone (someone calls in here from
        # an annotated entry point).
        modules_implicit: list[str] = []
        modules_truly_orphan: list[str] = []
        if modules_no_rf:
            view = load_graph(st.conn, pid)
            rf_linked_sids: set[int] = {
                int(r["symbol_id"])
                for r in st.conn.execute(
                    """SELECT DISTINCT rs.symbol_id FROM rf_symbol rs
                       JOIN symbol s ON s.id=rs.symbol_id
                       JOIN file f ON f.id=s.file_id
                       WHERE f.project_id=?""",
                    (pid,),
                )
            }
            for path in modules_no_rf:
                file_sids = {
                    int(r["id"])
                    for r in st.conn.execute(
                        """SELECT s.id FROM symbol s
                           JOIN file f ON f.id=s.file_id
                           WHERE f.project_id=? AND f.path=?""",
                        (pid, path),
                    )
                }
                covered = False
                if rf_linked_sids and file_sids:
                    for sid in file_sids:
                        if sid not in view.g:
                            continue
                        if ancestors_within(view.g, sid, 10) & rf_linked_sids:
                            covered = True
                            break
                (modules_implicit if covered else modules_truly_orphan).append(path)

        rfs_no_impl = [
            dict(r)
            for r in st.conn.execute(
                """SELECT r.rf_id, r.title, r.status, r.priority FROM rf r
                   WHERE r.project_id=?
                     AND NOT EXISTS (
                       SELECT 1 FROM rf_symbol rs WHERE rs.rf_id=r.id
                     )
                   ORDER BY r.rf_id""",
                (pid,),
            )
        ]

        rfs_low_conf = [
            {
                "rf_id": r["rf_id"],
                "title": r["title"],
                "avg_confidence": round(float(r["avg_confidence"]), 3),
                "link_count": int(r["link_count"]),
            }
            for r in st.conn.execute(
                """SELECT r.rf_id, r.title,
                          AVG(rs.confidence) AS avg_confidence,
                          COUNT(rs.id) AS link_count
                   FROM rf r JOIN rf_symbol rs ON rs.rf_id=r.id
                   WHERE r.project_id=?
                   GROUP BY r.id
                   HAVING avg_confidence < 0.7
                   ORDER BY avg_confidence ASC""",
                (pid,),
            )
        ]

        return {
            "modules_without_rf": modules_no_rf,
            "modules_implicitly_covered": modules_implicit,
            "modules_truly_orphan": modules_truly_orphan,
            "rfs_without_implementation": rfs_no_impl,
            "rfs_low_confidence": rfs_low_conf,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_orphan_tests(
        max_depth: int = 10,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Test functions whose descendant cone never reaches production code.

        Heuristic: any function/method in a `tests/` folder (or matching
        `*_test.*` / `test_*.*` naming) whose forward call graph contains
        zero non-test symbols. Either disconnected fixtures, helpers used only
        by other tests, or actually orphaned tests.
        """
        st = get_state(workspace)
        pid = st.project_id
        view = load_graph(st.conn, pid)

        def is_test_path(p: str) -> bool:
            base = p.rsplit("/", 1)[-1]
            return (
                p.startswith("tests/")
                or "/tests/" in p
                or base.startswith("test_")
                or base.endswith("_test.py")
                or base.endswith("_test.go")
                or "_test." in base
            )

        test_rows = st.conn.execute(
            """SELECT s.id, s.qualified_name, s.kind, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.kind IN ('function', 'method')""",
            (pid,),
        ).fetchall()
        test_syms = [dict(r) for r in test_rows if is_test_path(r["file_path"])]

        orphans: list[dict[str, Any]] = []
        for r in test_syms:
            sid = int(r["id"])
            descendants = (
                descendants_within(view.g, sid, max_depth) if sid in view.g else set()
            )
            reaches_prod = False
            for did in descendants:
                meta = view.sym_meta.get(did)
                if meta and not is_test_path(meta.get("file_path", "")):
                    reaches_prod = True
                    break
            if not reaches_prod:
                orphans.append({
                    "qualified_name": r["qualified_name"],
                    "file_path": r["file_path"],
                    "kind": r["kind"],
                    "reason": (
                        "no outgoing calls" if not descendants
                        else "descendant cone never escapes test files"
                    ),
                })
        return {"orphan_tests": orphans, "count": len(orphans)}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def git_diff_impact(
        base_ref: str = "HEAD~1",
        head_ref: str = "HEAD",
        max_depth: int = 5,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Topological impact of a git diff: changed files -> RFs + callers + suggested tests.

        The CI/PR-review entry point. Given a base..head git range, this tool:
        1. lists changed files via `git diff --name-only`
        2. resolves each one against the indexed symbols
        3. unions the backward cone of callers across them
        4. unions the affected RFs
        5. suggests test files: any file under `tests/` (or `*_test.*`) whose
           symbols call any impacted symbol — those are likely to break.

        Returns an empty result with `error` if either ref is unknown to git.
        Run `index_project` first if results look stale.
        """
        st = get_state(workspace)
        pid = st.project_id
        ws_root = str(st.settings.workspace)

        try:
            proc = subprocess.run(
                ["git", "-C", ws_root, "diff", "--name-only", f"{base_ref}..{head_ref}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except FileNotFoundError:
            return mcp_error(
                "git not found on PATH",
                hint="install git and ensure it is on PATH for this MCP server process",
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            # Boil down common git failure modes to a one-line summary so the
            # agent (and the user) don't get drowned in `git diff --help`.
            stderr_lower = stderr.lower()
            if "not a git repository" in stderr_lower:
                msg = (
                    f"workspace is not a git repository: {ws_root}. "
                    "git_diff_impact requires git history; run `git init` "
                    "and at least one commit first."
                )
            elif "unknown revision" in stderr_lower or "bad revision" in stderr_lower:
                msg = (
                    f"unknown git ref(s): base_ref='{base_ref}', "
                    f"head_ref='{head_ref}'. Check `git rev-parse` for both."
                )
            elif "ambiguous argument" in stderr_lower:
                msg = (
                    f"ambiguous ref: '{base_ref}..{head_ref}'. "
                    "Use full SHAs or branch names that exist locally."
                )
            else:
                # Truncate to keep payloads agent-friendly. First non-empty
                # line of stderr (or stdout) is almost always the real cause;
                # the rest is git's --help dump.
                first_line = next(
                    (ln for ln in (stderr or stdout).splitlines() if ln.strip()),
                    "",
                )
                msg = f"git diff failed: {first_line[:200]}" if first_line else "git diff failed (no diagnostic output)"
            return mcp_error(msg)
        except subprocess.TimeoutExpired:
            return mcp_error(
                "git diff timed out after 10s",
                hint="narrow the ref range or check for a runaway git hook",
            )

        changed_paths = [p for p in proc.stdout.splitlines() if p.strip()]
        if not changed_paths:
            return {
                "base_ref": base_ref,
                "head_ref": head_ref,
                "changed_files": [],
                "changed_files_indexed": [],
                "changed_files_unindexed": [],
                "changed_symbols": [],
                "impacted_callers": [],
                "affected_requirements": [],
                "suggested_tests": [],
            }

        view = load_graph(st.conn, pid)

        # Resolve changed files to indexed symbol ids
        changed_sym_ids: set[int] = set()
        changed_symbol_meta: list[dict[str, Any]] = []
        indexed_paths: set[str] = set()
        for path in changed_paths:
            rows = st.conn.execute(
                """SELECT s.id, s.qualified_name, s.kind, s.start_line, s.end_line
                   FROM symbol s JOIN file f ON f.id = s.file_id
                   WHERE f.project_id=? AND f.path=?""",
                (pid, path),
            ).fetchall()
            if rows:
                indexed_paths.add(path)
            for r in rows:
                sid = int(r["id"])
                changed_sym_ids.add(sid)
                changed_symbol_meta.append({
                    "id": sid,
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file_path": path,
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                })

        # Backward cone: every symbol that transitively calls a changed symbol
        impacted: set[int] = set()
        for sid in changed_sym_ids:
            if sid in view.g:
                impacted |= ancestors_within(view.g, sid, max_depth)
        impacted -= changed_sym_ids

        # Affected RFs: any rf_symbol whose symbol_id is in changed | impacted
        all_touched = changed_sym_ids | impacted
        affected_rfs: list[dict[str, Any]] = []
        if all_touched:
            placeholders = ",".join("?" * len(all_touched))
            for r in st.conn.execute(
                f"""SELECT DISTINCT r.rf_id, r.title, r.status, r.priority
                    FROM rf_symbol rs JOIN rf r ON r.id = rs.rf_id
                    WHERE rs.symbol_id IN ({placeholders})""",
                list(all_touched),
            ):
                affected_rfs.append(dict(r))

        # Suggested tests: files under a tests/ folder OR matching *_test.* /
        # test_*.* whose symbols are in `impacted` (i.e. test functions that call
        # something we touched).
        suggested_tests_set: set[str] = set()
        if all_touched:
            placeholders = ",".join("?" * len(all_touched))
            for r in st.conn.execute(
                f"""SELECT DISTINCT f.path FROM symbol_edge e
                    JOIN symbol s ON s.id = e.src_symbol_id
                    JOIN file f ON f.id = s.file_id
                    WHERE f.project_id=? AND e.dst_symbol_id IN ({placeholders})""",
                [pid, *list(all_touched)],
            ):
                p = r["path"]
                if (
                    p.startswith("tests/")
                    or "/tests/" in p
                    or "test_" in p.rsplit("/", 1)[-1]
                    or "_test." in p
                ):
                    suggested_tests_set.add(p)
        suggested_tests = sorted(suggested_tests_set)

        return {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "changed_files": changed_paths,
            "changed_files_indexed": sorted(indexed_paths),
            "changed_files_unindexed": sorted(set(changed_paths) - indexed_paths),
            "changed_symbols": changed_symbol_meta,
            "impacted_callers": [
                view.sym_meta[n] for n in impacted if n in view.sym_meta
            ],
            "affected_requirements": affected_rfs,
            "suggested_tests": suggested_tests,
        }
