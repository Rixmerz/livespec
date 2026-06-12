"""Indexing tool: index_project.

Every tool accepts ``workspace`` (absolute project root). Pass it on each call
when one MCP server handles multiple repos — no ``LIVESPEC_WORKSPACE`` in
mcp.json and no restart (LRU cache in ``get_state``).

v0.9 P6: `get_index_status` removed (deprecated in v0.8 P3.2). Read the
`project://index/status` resource for the same payload.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.indexer import index_project as run_index
from livespec_mcp.domain.rag import embed_pending, rebuild_chunks
from livespec_mcp.state import AppState, get_state
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace


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


def run_index_pipeline(st: AppState, *, force: bool = False, embed: bool = False) -> dict[str, Any]:
    """Index + idempotent chunk rebuild + optional embed. Shared by the
    `index_project` MCP tool and the `livespec-mcp index` CLI subcommand —
    both surfaces must report the same payload shape."""
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
    return {
        "files_total": stats.files_total,
        "files_changed": stats.files_changed,
        "files_skipped": stats.files_skipped,
        "symbols_total": stats.symbols_total,
        "edges_total": stats.edges_total,
        "rf_links_created": stats.rf_links_created,
        "manual_links_restored": stats.manual_links_restored,
        "languages": stats.languages,
        "languages_unsupported": stats.languages_unsupported,
        "repo_config": stats.repo_config,
        "workspace": str(st.settings.workspace),
        "watcher_started": False,
        "chunks": chunk_stats,
        "embeddings": embed_stats,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False})
    def index_project(
        force: bool = False,
        watch: bool = False,
        embed: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Walk the workspace, parse code, persist symbols + call edges.

        File-incremental via xxh3 content hash; pass force=True to re-extract.
        Respects .gitignore and an optional .livespec.toml at the workspace
        root ([index] table: ignore, languages, max_file_bytes — config
        patterns outrank .gitignore). Pass watch=True to also start a
        filesystem watcher after indexing so subsequent edits trigger
        automatic re-index (debounce 2s).
        Pass embed=True to populate vector embeddings after chunking
        (requires the [embeddings] extra: fastembed + sqlite-vec). First
        run downloads ~200MB of model weights; FTS5 lane works without it.
        Use after pulling new commits or when documentation feels stale.
        """
        st = get_state(workspace)
        result = run_index_pipeline(st, force=force, embed=embed)
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

    # Append the shared workspace note as a real docstring. A bare f-string as
    # the first statement is an expression, not a docstring, so __doc__ would be
    # None and the MCP client would see no description for this flagship tool.
    index_project.__doc__ = (index_project.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE
