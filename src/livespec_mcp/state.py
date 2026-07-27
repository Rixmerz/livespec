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
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from livespec_mcp.config import REPO_CONFIG_FILENAME, Settings
from livespec_mcp.storage.db import connect, get_or_create_project
from livespec_mcp.workspace_param import WorkspaceNotIndexedError, WorkspaceRequiredError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — py3.10 fallback
    import tomli as tomllib

_LRU_MAX = 8


def _read_group_db(workspace: Path) -> Path | None:
    """Tolerantly read ``[workspace] group_db`` from ``.livespec.toml``.

    Returns the resolved shared-DB path, or None when unset/absent/malformed.
    Deliberately tolerant: this runs on **every** ``get_state`` (i.e. every
    tool call), so a typoed config must not brick unrelated tools — full
    validation still happens loudly in ``load_repo_config`` during
    ``index_project``. Relative paths resolve against the workspace root.
    """
    path = workspace / REPO_CONFIG_FILENAME
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        raw = data.get("workspace", {}).get("group_db")
        if not isinstance(raw, str) or not raw.strip():
            return None
        gp = Path(raw).expanduser()
        if not gp.is_absolute():
            gp = (workspace / gp).resolve()
        return gp
    except Exception:
        return None


@dataclass
class AppState:
    settings: Settings
    conn: sqlite3.Connection
    _lock: threading.Lock
    _project_id: int | None = None

    @property
    def project_id(self) -> int:
        # Cache after first resolution: this property is read several times per
        # tool call, and get_or_create_project runs a SELECT (plus a possible
        # INSERT) each time. The (project, root) mapping is stable for the life
        # of an AppState — the workspace path is the cache key.
        if self._project_id is None:
            self._project_id = get_or_create_project(
                self.conn, name=self.settings.workspace.name, root=str(self.settings.workspace)
            )
        return self._project_id

    def lock(self) -> threading.Lock:
        return self._lock

    def group_project_ids(self) -> list[int]:
        """Project ids to search for symbols, home project first.

        For an ungrouped workspace this is just ``[project_id]`` (identical to
        pre-group behaviour). For a shared group DB it is the home project
        followed by every other project in the same database — a shared DB
        *is* the group.
        """
        if not self.settings.grouped:
            return [self.project_id]
        ids = [self.project_id]
        ids.extend(
            int(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM project WHERE id != ? ORDER BY id", (self.project_id,)
            )
        )
        return ids

    def resolve_symbol(self, qname: str) -> sqlite3.Row | None:
        """Resolve a qualified name to a symbol row (id, kind), preferring the
        home project, then the rest of the group. Returns None if unknown.

        Ungrouped workspaces resolve within the home project only — byte-for-
        byte the previous ``WHERE f.project_id=?`` behaviour."""
        ids = self.group_project_ids()
        placeholders = ",".join("?" for _ in ids)
        # Home-project-first ordering so a symbol that exists locally always
        # wins over a same-named symbol in another repo of the group.
        order = " ".join(f"WHEN {pid} THEN {i}" for i, pid in enumerate(ids))
        return self.conn.execute(
            f"""SELECT s.id, s.kind FROM symbol s JOIN file f ON f.id=s.file_id
                WHERE f.project_id IN ({placeholders}) AND s.qualified_name=?
                ORDER BY CASE f.project_id {order} END LIMIT 1""",
            (*ids, qname),
        ).fetchone()


_cache: OrderedDict[Path, AppState] = OrderedDict()
_cache_lock = threading.Lock()


def _resolve_workspace(path: str | Path | None) -> Path:
    if path is None or (isinstance(path, str) and not str(path).strip()):
        raise WorkspaceRequiredError(
            "workspace is required on every tool call. "
            "Pass workspace='/absolute/path/to/project' (your repository root). "
            "LIVESPEC_WORKSPACE and other env defaults are not used."
        )
    return Path(str(path)).expanduser().resolve()


def workspace_db_path(workspace: Path) -> Path:
    """Resolve the ``docs.db`` path a workspace would use (honors group_db).

    Pure filesystem lookup — never creates anything. Shared by ``get_state``
    and ``WorkspaceErrorMiddleware`` so both agree on "is this indexed?".
    """
    group_db = _read_group_db(workspace)
    return group_db if group_db is not None else workspace / ".mcp-docs" / "docs.db"


def get_state(workspace: str | Path | None = None, *, create: bool = False) -> AppState:
    """Return the AppState for the given workspace, opening it if needed.

    Requires ``workspace`` on every call (absolute project root).

    ``create`` (default ``False``): when the workspace has never been
    indexed (no ``.mcp-docs/docs.db``), the default is to raise
    ``WorkspaceNotIndexedError`` rather than silently materializing an empty
    database in an arbitrary directory (a typo'd ``workspace`` used to leave
    an orphan ``.mcp-docs/`` behind). Pass ``create=True`` only from
    ``index_project`` / the ``index`` CLI command / other explicit
    bootstrap paths that are SUPPOSED to build a fresh index.

    Once the DB file exists (created earlier via ``create=True``), every
    call — including this function's own default — opens it normally
    (read-write): mutation tools (``create_spec``, ``bulk_link_spec_symbols``,
    ...) share this same default path and need write access to data that
    already exists. ``create`` only gates *first creation*, not connection
    mode — see ``storage.db.connect(..., create=False)`` for the stricter
    read-only primitive this deliberately does not use here.
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
        # New workspace — build state, evict LRU if needed.
        # A `[workspace] group_db` reroutes only the DB to a shared file (each
        # repo keeps its own project_id inside it); docs/explorer stay per-repo.
        group_db = _read_group_db(ws)
        db_path = group_db if group_db is not None else workspace_db_path(ws)
        if not create and not db_path.is_file():
            raise WorkspaceNotIndexedError(
                f"workspace not indexed: {ws}. "
                f"run index_project(workspace='{ws}') first."
            )
        settings = Settings(
            workspace=ws,
            state_dir=ws / ".mcp-docs",
            db_path=db_path,
            docs_dir=ws / ".mcp-docs" / "docs",
            grouped=group_db is not None,
        )
        settings.ensure_dirs()
        conn = connect(settings.db_path)
        new_state = AppState(settings=settings, conn=conn, _lock=threading.Lock())
        _cache[ws] = new_state
        if len(_cache) > _LRU_MAX:
            evicted_ws, evicted = _cache.popitem(last=False)
            # Stop any live watcher FIRST — otherwise its debounced reindex
            # keeps firing against the connection we are about to close and
            # dies with "Cannot operate on a closed database", silently
            # killing the "live" index for that workspace.
            _stop_watcher_for(evicted_ws)
            try:
                evicted.conn.close()
            except Exception:
                pass
        return new_state


def _stop_watcher_for(workspace: Path) -> None:
    """Stop a workspace's watcher on eviction/reset. Imported lazily so the
    watcher module (and watchdog) load only when a watcher was actually
    started."""
    try:
        from livespec_mcp.domain.watcher import stop_watcher

        stop_watcher(workspace)
    except Exception:
        pass


def get_mru_state() -> AppState | None:
    """Most-recently-used workspace state, or None if nothing was opened yet.

    Resources have no parameter channel for ``workspace`` (URI templates
    only), so since multi-tenant v0.12 they bind to the workspace of the
    most recent tool call. Single-repo sessions — the common case for
    resources — always resolve correctly."""
    with _cache_lock:
        if not _cache:
            return None
        return _cache[next(reversed(_cache))]


def reset_state() -> None:
    """For tests: drop every cached workspace."""
    with _cache_lock:
        for ws, st in _cache.items():
            _stop_watcher_for(ws)
            try:
                st.conn.close()
            except Exception:
                pass
        _cache.clear()


