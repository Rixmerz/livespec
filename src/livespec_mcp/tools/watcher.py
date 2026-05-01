"""Watcher tools: start_watcher, stop_watcher, watcher_status."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.indexer import index_project as run_index
from livespec_mcp.domain.watcher import (
    Watcher,
    all_watchers,
    get_watcher,
    register_watcher,
    unregister_watcher,
)
from livespec_mcp.state import get_state


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def start_watcher(
        debounce_seconds: float = 2.0,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Start a filesystem watcher that re-indexes the workspace on file changes.

        Debounce: a burst of events within `debounce_seconds` triggers a single
        re-index after the burst ends. This prevents N reindexes during a
        formatter pass or git checkout.

        Idempotent: calling again replaces the active watcher.
        """
        st = get_state(workspace)
        ws_path = st.settings.workspace

        def _do_reindex() -> None:
            with st.lock():
                run_index(st.settings, st.conn)

        watcher = Watcher(workspace=ws_path, on_reindex=_do_reindex, debounce_seconds=debounce_seconds)
        register_watcher(ws_path, watcher)
        watcher.start()
        return {
            "watching": str(ws_path),
            "debounce_seconds": debounce_seconds,
            "active_watchers": len(all_watchers()),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def stop_watcher(workspace: str | None = None) -> dict[str, Any]:
        """Stop the active watcher for a workspace. Returns whether one existed."""
        st = get_state(workspace)
        ws_path = st.settings.workspace
        stopped = unregister_watcher(ws_path)
        return {
            "workspace": str(ws_path),
            "stopped": stopped,
            "active_watchers": len(all_watchers()),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def watcher_status(workspace: str | None = None) -> dict[str, Any]:
        """Report the current watcher's stats: events seen, reindex runs, last run time."""
        st = get_state(workspace)
        ws_path = st.settings.workspace
        watcher = get_watcher(ws_path)
        if watcher is None:
            return {"workspace": str(ws_path), "active": False}
        s = watcher.stats
        return {
            "workspace": str(ws_path),
            "active": True,
            "started_at": s.started_at,
            "events_received": s.events_received,
            "reindex_runs": s.reindex_runs,
            "last_reindex_at": s.last_reindex_at,
            "debounce_seconds": watcher._debounce,
        }
