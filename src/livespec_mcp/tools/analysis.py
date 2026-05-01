"""Analysis tools.

P1.2 consolidation: `find_references` removed — use
`analyze_impact(target_type='symbol', target=qname, max_depth=1)` and read
the `impacted_callers` list (matches the old shape).
"""

from __future__ import annotations

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
            return {"error": f"Symbol '{identifier}' not found", "isError": True}
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
            return {"error": f"Symbol '{identifier}' not found", "isError": True}
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
                return {"error": f"Symbol '{target}' not found", "isError": True}
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
                return {"error": f"File '{target}' not indexed", "isError": True}
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
                return {"error": f"RF '{target}' not found", "isError": True}
            sids = [
                int(r["symbol_id"])
                for r in st.conn.execute(
                    "SELECT symbol_id FROM rf_symbol WHERE rf_id=?", (rf["id"],)
                )
            ]
            if not sids:
                return {"rf_id": rf["rf_id"], "warning": "RF has no linked symbols", "implementing_symbols": []}
            forward: set[int] = set()
            backward: set[int] = set()
            for sid in sids:
                if sid in view.g:
                    forward |= descendants_within(view.g, sid, max_depth)
                    backward |= ancestors_within(view.g, sid, max_depth)
            return {
                "rf_id": rf["rf_id"],
                "implementing_symbols": [view.sym_meta[n] for n in sids if n in view.sym_meta],
                "downstream": [view.sym_meta[n] for n in forward if n in view.sym_meta],
                "upstream_callers": [view.sym_meta[n] for n in backward if n in view.sym_meta],
            }
        return {"error": f"Unknown target_type '{target_type}'", "isError": True}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_project_overview(workspace: str | None = None) -> dict[str, Any]:
        """High-level snapshot: languages, modules, top symbols by PageRank, RF coverage."""
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
        top = sorted(ranks.items(), key=lambda x: x[1], reverse=True)[:20]
        top_syms = [
            {**view.sym_meta[sid], "pagerank": round(score, 6)}
            for sid, score in top
            if sid in view.sym_meta
        ]
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
