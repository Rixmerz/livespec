"""Indexing tools: index_project, get_index_status, list_files.

Every tool accepts an optional `workspace` argument. When omitted, the server
falls back to the LIVESPEC_WORKSPACE env var or the current working directory
(P1.1 multi-tenant). `use_workspace` is retained as a deprecated alias.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.indexer import index_project as run_index
from livespec_mcp.state import get_state, use_workspace as _use_ws


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def use_workspace(path: str) -> dict[str, Any]:
        """Set the default workspace for subsequent tool calls.

        Deprecated in favor of passing `workspace=...` to each tool, but kept
        for v0.1 callers and convenience. Sets LIVESPEC_WORKSPACE in the env
        and pre-warms the LRU cache for the path.
        """
        st = _use_ws(path)
        return {
            "workspace": str(st.settings.workspace),
            "db_path": str(st.settings.db_path),
            "state_dir": str(st.settings.state_dir),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False})
    def index_project(force: bool = False, workspace: str | None = None) -> dict[str, Any]:
        """Walk the workspace, parse code, persist symbols + call edges.

        File-incremental via xxh3 content hash; pass force=True to re-extract.
        Use after pulling new commits or when documentation feels stale.
        """
        st = get_state(workspace)
        with st.lock():
            stats = run_index(st.settings, st.conn, force=force)
        return {
            "files_total": stats.files_total,
            "files_changed": stats.files_changed,
            "files_skipped": stats.files_skipped,
            "symbols_total": stats.symbols_total,
            "edges_total": stats.edges_total,
            "rf_links_created": stats.rf_links_created,
            "languages": stats.languages,
            "workspace": str(st.settings.workspace),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_index_status(workspace: str | None = None) -> dict[str, Any]:
        """Report current index status: latest run, totals, freshness."""
        st = get_state(workspace)
        pid = st.project_id
        last = st.conn.execute(
            "SELECT * FROM index_run WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)
        ).fetchone()
        files = st.conn.execute(
            "SELECT COUNT(*) c FROM file WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        syms = st.conn.execute(
            "SELECT COUNT(*) c FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?",
            (pid,),
        ).fetchone()["c"]
        edges = st.conn.execute(
            """SELECT COUNT(*) c FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id
               JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
            (pid,),
        ).fetchone()["c"]
        rfs = st.conn.execute(
            "SELECT COUNT(*) c FROM rf WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        return {
            "workspace": str(st.settings.workspace),
            "project_id": pid,
            "files": int(files),
            "symbols": int(syms),
            "edges": int(edges),
            "requirements": int(rfs),
            "last_run": dict(last) if last else None,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def list_files(
        path_glob: str | None = None,
        language: str | None = None,
        limit: int = 200,
        cursor: int = 0,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """List indexed files with optional filters and pagination."""
        st = get_state(workspace)
        pid = st.project_id
        sql = ["SELECT id, path, language, line_count, content_hash, mtime FROM file WHERE project_id=?"]
        args: list[Any] = [pid]
        if language:
            sql.append("AND language = ?")
            args.append(language)
        if path_glob:
            sql.append("AND path GLOB ?")
            args.append(path_glob)
        sql.append("ORDER BY id LIMIT ? OFFSET ?")
        args.extend([limit + 1, cursor])
        rows = st.conn.execute(" ".join(sql), args).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "files": [dict(r) for r in rows],
            "next_cursor": (cursor + limit) if has_more else None,
        }
