"""Read-only SQLite access to an existing index (no bootstrap, no migrations).

`storage.db.connect` is the writer's door: it switches to WAL, runs the schema
script and every pending migration — all writes. A viewer must not do any of
that to someone else's index, so this module opens the file through a
``mode=ro`` URI and sets ``query_only`` on top.

One more write hides in a plain ``mode=ro`` open of a WAL database: SQLite
creates the ``-shm`` / ``-wal`` side files when they are absent and leaves them
behind. When no ``-wal`` file exists (no writer has the index open; the last
one checkpointed and cleaned up), the database is opened ``immutable=1``
instead, which reads the main file and creates nothing. When a writer is
active its side files already exist, so the ``mode=ro`` open creates nothing
either.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote


def _uri(path: Path, *, immutable: bool) -> str:
    q = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"file:{quote(str(path))}?{q}"


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only. Raises FileNotFoundError if absent."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"index database not found: {path}")
    wal = path.with_name(path.name + "-wal")
    conn = sqlite3.connect(
        _uri(path, immutable=not wal.exists()),
        uri=True,
        check_same_thread=False,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


__all__ = ["connect_readonly"]
