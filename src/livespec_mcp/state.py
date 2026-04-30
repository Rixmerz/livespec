"""Shared server state: settings + DB connection."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from livespec_mcp.config import Settings
from livespec_mcp.storage.db import connect, get_or_create_project


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


_state: AppState | None = None


def get_state() -> AppState:
    global _state
    if _state is None:
        settings = Settings.load()
        settings.ensure_dirs()
        conn = connect(settings.db_path)
        _state = AppState(settings=settings, conn=conn, _lock=threading.Lock())
    return _state


def reset_state() -> None:
    """For tests."""
    global _state
    if _state is not None:
        try:
            _state.conn.close()
        except Exception:
            pass
    _state = None
