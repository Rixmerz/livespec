"""Indexing tool: index_project.

Every tool accepts an optional `workspace` argument. When omitted, the server
falls back to the LIVESPEC_WORKSPACE env var or the current working directory
(P1.1 multi-tenant).

v0.6: `use_workspace` was removed (deprecated since v0.2). Pass `workspace=`
to every tool, or set LIVESPEC_WORKSPACE in the environment.

v0.9 P6: `get_index_status` removed (deprecated in v0.8 P3.2). Read the
`project://index/status` resource for the same payload.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.indexer import index_project as run_index
from livespec_mcp.domain.rag import embed_pending, rebuild_chunks
from livespec_mcp.state import AppState, get_state


def compute_index_status(st: AppState) -> dict[str, Any]:
    """Module-level so resources.py keeps a stable shape.

    The tool wrapper around this helper was removed in v0.9 P6 — the
    `project://index/status` resource is the canonical surface now.
    """
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


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False})
    def index_project(
        force: bool = False,
        watch: bool = False,
        embed: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Walk the workspace, parse code, persist symbols + call edges.

        File-incremental via xxh3 content hash; pass force=True to re-extract.
        Pass watch=True to also start a filesystem watcher after indexing so
        subsequent edits trigger automatic re-index (debounce 2s).
        Pass embed=True to populate vector embeddings after chunking
        (requires the [embeddings] extra: fastembed + sqlite-vec). First
        run downloads ~200MB of model weights; FTS5 lane works without it.
        Use after pulling new commits or when documentation feels stale.
        """
        st = get_state(workspace)
        with st.lock():
            stats = run_index(st.settings, st.conn, force=force)
            existing = st.conn.execute(
                "SELECT COUNT(*) c FROM chunk WHERE project_id=?", (st.project_id,)
            ).fetchone()["c"]
            if force or stats.files_changed or existing == 0:
                chunk_stats: dict[str, Any] = dict(rebuild_chunks(st.conn, st.project_id))
            else:
                chunk_stats = {"skipped": "no file changes"}
            embed_stats: dict[str, Any] = {"requested": embed}
            if embed:
                embed_stats.update(embed_pending(st.conn, st.project_id))
        result: dict[str, Any] = {
            "files_total": stats.files_total,
            "files_changed": stats.files_changed,
            "files_skipped": stats.files_skipped,
            "symbols_total": stats.symbols_total,
            "edges_total": stats.edges_total,
            "rf_links_created": stats.rf_links_created,
            "languages": stats.languages,
            "workspace": str(st.settings.workspace),
            "watcher_started": False,
            "chunks": chunk_stats,
            "embeddings": embed_stats,
        }
        if watch:
            from livespec_mcp.domain.watcher import Watcher, register_watcher

            def _do_reindex() -> None:
                with st.lock():
                    run_index(st.settings, st.conn)

            ws_path = st.settings.workspace
            w = Watcher(workspace=ws_path, on_reindex=_do_reindex, debounce_seconds=2.0)
            register_watcher(ws_path, w)
            w.start()
            result["watcher_started"] = True
        return result
