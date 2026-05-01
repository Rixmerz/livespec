"""Multi-tenant per-workspace state cache.

Design (P1.1): the server is a long-running process that may be asked to
analyze multiple workspaces in a single session — Claude Code shells, multi-
repo agents, parallel pytest workers. We keep an LRU cache of `AppState`
keyed by absolute workspace path. Each AppState owns its own SQLite
connection against the corresponding `.mcp-docs/docs.db`.

Backward compatibility:
- `get_state()` (no args) resolves to the workspace from the env var
  `LIVESPEC_WORKSPACE` or the current working directory, matching v0.1.
- `get_state(workspace=path)` returns the state for a specific workspace.

v0.6: the `use_workspace` MCP tool was removed (deprecated since v0.2). The
internal `use_workspace()` helper is also gone — set LIVESPEC_WORKSPACE in
the environment if you need a default, or pass `workspace=` to every tool.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from livespec_mcp.config import Settings
from livespec_mcp.storage.db import connect, get_or_create_project

_LRU_MAX = 8


@dataclass
class AppState:
    settings: Settings
    conn: sqlite3.Connection
    _lock: threading.Lock

    @property
    def project_id(self) -> int:
        return get_or_create_project(
            self.conn, name=self.settings.workspace.name, root=str(self.settings.workspace)
        )

    def lock(self) -> threading.Lock:
        return self._lock


_cache: "OrderedDict[Path, AppState]" = OrderedDict()
_cache_lock = threading.Lock()


def _resolve_workspace(path: str | Path | None) -> Path:
    if path is None:
        path = os.environ.get("LIVESPEC_WORKSPACE") or os.environ.get(
            "DOCS_BRAIN_WORKSPACE", os.getcwd()
        )
    return Path(str(path)).expanduser().resolve()


def get_state(workspace: str | Path | None = None) -> AppState:
    """Return the AppState for the given workspace, opening it if needed.

    With workspace=None, resolves via env var or cwd (v0.1 behaviour).
    """
    ws = _resolve_workspace(workspace)
    with _cache_lock:
        st = _cache.get(ws)
        if st is not None:
            _cache.move_to_end(ws)  # mark as most-recent
            return st
        # New workspace — build state, evict LRU if needed
        settings = Settings(
            workspace=ws,
            state_dir=ws / ".mcp-docs",
            db_path=ws / ".mcp-docs" / "docs.db",
            docs_dir=ws / ".mcp-docs" / "docs",
            models_dir=ws / ".mcp-docs" / "models",
        )
        settings.ensure_dirs()
        conn = connect(settings.db_path)
        new_state = AppState(settings=settings, conn=conn, _lock=threading.Lock())
        _cache[ws] = new_state
        if len(_cache) > _LRU_MAX:
            _, evicted = _cache.popitem(last=False)
            try:
                evicted.conn.close()
            except Exception:
                pass
        return new_state


def reset_state() -> None:
    """For tests: drop every cached workspace."""
    with _cache_lock:
        for st in _cache.values():
            try:
                st.conn.close()
            except Exception:
                pass
        _cache.clear()


