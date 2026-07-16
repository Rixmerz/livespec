"""Indexing tool: index_project.

Every tool accepts ``workspace`` (absolute project root). Pass it on each call
when one MCP server handles multiple repos — no ``LIVESPEC_WORKSPACE`` in
mcp.json and no restart (LRU cache in ``get_state``).

v0.9 P6: `get_index_status` removed (deprecated in v0.8 P3.2). Read the
`project://index/status` resource for the same payload.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.indexer import index_project as run_index
from livespec_mcp.domain.rag import embed_pending, rebuild_chunks
from livespec_mcp.state import AppState, get_state
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

_log = logging.getLogger("livespec.indexing")


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
    specs = st.conn.execute(
        "SELECT COUNT(*) c FROM spec WHERE project_id=?", (pid,)
    ).fetchone()["c"]
    return {
        "workspace": str(st.settings.workspace),
        "project_id": pid,
        "files": int(files),
        "symbols": int(syms),
        "edges": int(edges),
        "specs": int(specs),
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
        # A deletion-only change increments nothing in files_changed, but its
        # chunks (FTS + vectors) must be pruned — otherwise search keeps
        # returning hits for deleted files. stats.files_deleted covers that.
        if force or stats.files_changed or stats.files_deleted or existing == 0:
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
        "files_deleted": stats.files_deleted,
        "symbols_total": stats.symbols_total,
        "edges_total": stats.edges_total,
        "spec_links_created": stats.spec_links_created,
        "manual_links_restored": stats.manual_links_restored,
        "languages": stats.languages,
        "languages_unsupported": stats.languages_unsupported,
        "repo_config": stats.repo_config,
        "workspace": str(st.settings.workspace),
        "watcher_started": False,
        "chunks": chunk_stats,
        "embeddings": embed_stats,
    }


def _should_build_explorer(st: AppState, explorer: bool) -> bool:
    """Return whether ``index_project`` should (re)generate the Spec Explorer bundle.

    True when any of:

    * ``explorer=True`` (explicit opt-in),
    * the bundle already exists (freshness — keep it from going stale),
    * the workspace looks like a FastAPI app (``main.py`` / ``app.py`` with
      ``app = FastAPI(...)``) and no bundle exists yet (first-index autodetect).
    """
    if explorer:
        return True
    explorer_dir = st.settings.state_dir / "explorer"
    if explorer_dir.exists():
        return True
    from livespec_mcp.explorer.autowire import find_fastapi_entrypoints

    return bool(find_fastapi_entrypoints(st.settings.workspace))


def _maybe_regenerate_explorer(st: AppState, explorer: bool) -> bool:
    """Refresh the static Spec Explorer bundle to keep it from going stale.

    Regenerates when :func:`_should_build_explorer` is true (explicit flag,
    existing bundle, or FastAPI entry autodetect on first index). Any failure
    is logged and swallowed — a bad explorer build must never break the index
    pipeline. Returns whether the bundle was (re)written.

    Refreshes on EVERY index (not gated on code changes) because specs/links
    can change without any file changing; the cost of the refresh itself was
    cut by the v0.20 coverage-BFS inversion (H3) and the single
    compute_endpoints pass, so an always-fresh bundle stays affordable.
    """
    if not _should_build_explorer(st, explorer):
        return False
    try:
        # Imported here (not at module top) so explorer.py — owned by another
        # surface — is only loaded when a bundle refresh is actually needed.
        from livespec_mcp.tools.explorer import write_explorer_bundle

        write_explorer_bundle(st)
        return True
    except Exception:
        _log.exception("Spec Explorer bundle regeneration failed; skipping")
        return False


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False})
    def index_project(
        force: bool = False,
        watch: bool = False,
        embed: bool = False,
        explorer: bool = False,
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
        Pass explorer=True to (re)generate the static Spec Explorer bundle
        (.mcp-docs/explorer/) after indexing; it is also auto-refreshed
        whenever that bundle already exists, so the viewer never goes stale.
        When ``[specs].sync_from`` is set in ``.livespec.toml``, markdown
        specs are re-imported after each index (idempotent). Optional
        ``[specs].links_seed`` replays ``bulk_link_spec_symbols`` from JSON.
        On a first index, a FastAPI workspace (``app = FastAPI(...)`` in
        ``main.py`` / ``app.py``) auto-builds the bundle and autowires
        ``mount_explorer(app)`` when ``[explorer] auto_mount = true``.
        A bundle-regeneration failure never breaks indexing — it is logged
        and skipped. The payload reports `explorer_regenerated`.
        Use after pulling new commits or when documentation feels stale.
        """
        st = get_state(workspace)
        result = run_index_pipeline(st, force=force, embed=embed)
        result["explorer_regenerated"] = _maybe_regenerate_explorer(st, explorer)
        try:
            from livespec_mcp.domain.specs_sync import sync_specs_from_config

            specs_sync = sync_specs_from_config(st)
            if specs_sync is not None:
                result["specs_sync"] = specs_sync
        except Exception:
            _log.exception("specs sync failed; skipping")
        if watch:
            from livespec_mcp.domain.watcher import Watcher, register_watcher

            settings = st.settings
            db_path = settings.db_path

            def _do_reindex() -> None:
                # Run on a DEDICATED connection, not st.conn. The watcher fires
                # from a background thread; sharing the tool threads' connection
                # let its BEGIN/COMMIT interleave with concurrent tool calls —
                # dirty reads mid-index and, worse, unlocked writes joining the
                # indexer transaction and being silently rolled back. A private
                # WAL connection isolates the reindex; other connections see the
                # result once it commits. Invalidate the graph cache after so the
                # freshly-indexed graph is picked up on the next analysis call.
                from livespec_mcp.domain.graph import invalidate_graph_cache
                from livespec_mcp.storage.db import connect

                conn = connect(db_path)
                try:
                    stats = run_index(settings, conn)
                finally:
                    conn.close()
                if stats.files_changed or stats.files_deleted:
                    invalidate_graph_cache()

            ws_path = settings.workspace
            w = Watcher(workspace=ws_path, on_reindex=_do_reindex, debounce_seconds=2.0)
            register_watcher(ws_path, w)
            w.start()
            result["watcher_started"] = True
        return result

    # Append the shared workspace note as a real docstring. A bare f-string as
    # the first statement is an expression, not a docstring, so __doc__ would be
    # None and the MCP client would see no description for this flagship tool.
    index_project.__doc__ = (index_project.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE
