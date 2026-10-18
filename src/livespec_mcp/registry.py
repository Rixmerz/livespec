"""User-level registry of indexed workspaces (outside every repository).

Every successful index (`index_project` over MCP, `livespec index` on the CLI)
records the workspace here, so `livespec ui` can show every project this user
has indexed without being told where they are — and without writing anything
into any of them.

Location, first match wins:

    $LIVESPEC_HOME/registry.json
    $XDG_DATA_HOME/livespec/registry.json
    ~/.livespec/registry.json

One JSON document, keyed by absolute workspace path::

    {"version": 1,
     "projects": {"/abs/repo": {"path", "name", "db_path", "group",
                                "last_indexed", "languages", "counts"}}}

``group`` is the shared ``[workspace] group_db`` path (repos sharing one group
DB are one group), or null for a repo with its own ``.mcp-docs/docs.db``.

Entries are never pruned silently. `list_projects` reports an entry whose
database (or workspace directory) is gone with ``status`` ``missing_db`` /
``missing_workspace``; only `forget` removes it, because a DB on an unmounted
disk or a branch checked out elsewhere is not the same as a project the user
abandoned.

Writes are atomic (temp file + ``os.replace``) and serialized across processes
with an ``fcntl`` lock where available, so two indexers finishing at once
cannot lose each other's entry. Recording must never fail an index run: the
caller wraps it, and every error here is reported, never raised into indexing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REGISTRY_VERSION = 1
REGISTRY_FILENAME = "registry.json"


def registry_home() -> Path:
    """Directory that holds the registry (never inside a repository)."""
    raw = os.environ.get("LIVESPEC_HOME")
    if raw and raw.strip():
        return Path(raw).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and xdg.strip():
        return (Path(xdg).expanduser() / "livespec").resolve()
    return (Path.home() / ".livespec").resolve()


def registry_path() -> Path:
    return registry_home() / REGISTRY_FILENAME


def _empty() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "projects": {}}


def load_registry() -> dict[str, Any]:
    """Read the registry; a missing or unreadable file reads as empty."""
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        return _empty()
    return data


@contextmanager
def _locked() -> Iterator[None]:
    home = registry_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows: atomic replace only
        yield
        return
    with open(home / ".registry.lock", "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _write(data: dict[str, Any]) -> None:
    path = registry_path()
    fd, tmp = tempfile.mkstemp(prefix=".registry-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def snapshot_from_db(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """Languages + counts for one project, read from its index."""
    languages = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT language, COUNT(*) FROM file WHERE project_id=? GROUP BY language "
            "ORDER BY COUNT(*) DESC, language",
            (project_id,),
        )
    }

    def one(sql: str) -> int:
        return int(conn.execute(sql, (project_id,)).fetchone()[0])

    counts = {
        "files": sum(languages.values()),
        "symbols": one(
            "SELECT COUNT(*) FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?"
        ),
        "edges": one(
            "SELECT COUNT(*) FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id "
            "JOIN file f ON f.id=s.file_id WHERE f.project_id=?"
        ),
        "specs": one("SELECT COUNT(*) FROM spec WHERE project_id=?"),
    }
    return {"languages": languages, "counts": counts}


def record_workspace(
    workspace: Path,
    db_path: Path,
    *,
    grouped: bool,
    languages: dict[str, int] | None = None,
    counts: dict[str, int] | None = None,
    when: str | None = None,
) -> dict[str, Any]:
    """Insert or refresh one workspace's entry. Returns the stored entry."""
    ws = Path(workspace).resolve()
    entry = {
        "path": str(ws),
        "name": ws.name or str(ws),
        "db_path": str(Path(db_path).resolve()),
        "group": str(Path(db_path).resolve()) if grouped else None,
        "last_indexed": when or _now(),
        "languages": dict(languages or {}),
        "counts": dict(counts or {}),
    }
    with _locked():
        data = load_registry()
        data["version"] = REGISTRY_VERSION
        data["projects"][str(ws)] = entry
        _write(data)
    return entry


def record_state(st: Any) -> dict[str, Any]:
    """Record an indexed ``AppState`` (the shape both index surfaces hold)."""
    snap = snapshot_from_db(st.conn, st.project_id)
    return record_workspace(
        st.settings.workspace,
        st.settings.db_path,
        grouped=bool(st.settings.grouped),
        languages=snap["languages"],
        counts=snap["counts"],
    )


def forget(path: str) -> bool:
    """Remove one entry by workspace path. True when something was removed."""
    key = str(Path(path).expanduser().resolve())
    with _locked():
        data = load_registry()
        projects = data["projects"]
        hit = projects.pop(key, None)
        if hit is None and path in projects:
            hit = projects.pop(path)
        if hit is None:
            return False
        _write(data)
    return True


def entry_status(entry: dict[str, Any]) -> str:
    """``ok`` | ``missing_db`` | ``missing_workspace`` — checked, not stored."""
    if not Path(str(entry.get("db_path", ""))).is_file():
        return "missing_db"
    if not Path(str(entry.get("path", ""))).is_dir():
        return "missing_workspace"
    return "ok"


def list_projects() -> list[dict[str, Any]]:
    """Every registered workspace with its live ``status``, sorted by name."""
    out = []
    for entry in load_registry()["projects"].values():
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        item = dict(entry)
        item["status"] = entry_status(entry)
        out.append(item)
    out.sort(key=lambda e: (str(e.get("name", "")).lower(), str(e["path"])))
    return out


__all__ = [
    "REGISTRY_VERSION",
    "entry_status",
    "forget",
    "list_projects",
    "load_registry",
    "record_state",
    "record_workspace",
    "registry_home",
    "registry_path",
    "snapshot_from_db",
]
