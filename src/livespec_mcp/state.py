"""Multi-tenant per-workspace state cache.

Design (P1.1): the server is a long-running process that may be asked to
analyze multiple workspaces in a single session — Claude Code shells, multi-
repo agents, parallel pytest workers. We keep an LRU cache of `AppState`
keyed by absolute workspace path. Each AppState owns its own SQLite
connection against the corresponding `.mcp-docs/docs.db`.

Multi-repo (required):
- Pass ``workspace="/abs/path/to/project"`` on **every** tool call. The server
  keeps an LRU cache of up to 8 open workspaces — no MCP restart between repos.

There is **no** ``LIVESPEC_WORKSPACE`` (or cwd) fallback. Omitting ``workspace``
returns an error.

v0.6: the ``use_workspace`` MCP tool was removed.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from livespec_mcp.config import Settings
from livespec_mcp.storage.db import connect, get_or_create_project
from livespec_mcp.workspace_param import WorkspaceRequiredError

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
    if path is None or (isinstance(path, str) and not str(path).strip()):
        raise WorkspaceRequiredError(
            "workspace is required on every tool call. "
            "Pass workspace='/absolute/path/to/project' (your repository root). "
            "LIVESPEC_WORKSPACE and other env defaults are not used."
        )
    return Path(str(path)).expanduser().resolve()


def get_state(workspace: str | Path | None = None) -> AppState:
    """Return the AppState for the given workspace, opening it if needed.

    Requires ``workspace`` on every call (absolute project root).
    """
    ws = _resolve_workspace(workspace)
    if not ws.is_dir():
        raise FileNotFoundError(
            f"Workspace directory not found: {ws}. "
            "Pass workspace='/absolute/path/to/project' on the tool call."
        )
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


