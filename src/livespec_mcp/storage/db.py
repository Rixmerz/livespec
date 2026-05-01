"""SQLite connection helpers and schema bootstrap."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

_SCHEMA_CACHE: str | None = None


def _schema_sql() -> str:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with resources.files("livespec_mcp.storage").joinpath("schema.sql").open() as f:
            _SCHEMA_CACHE = f.read()
    return _SCHEMA_CACHE


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(_schema_sql())
    _migrate_v1_to_v2(conn)
    return conn


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Drop dead tables/columns from v1 schemas. Idempotent."""
    # commit_snapshot was never written; simply drop if present.
    conn.execute("DROP TABLE IF EXISTS commit_snapshot")
    # unresolved_ref is now resolved in-memory per run (P1.3); drop the persisted table.
    conn.execute("DROP TABLE IF EXISTS unresolved_ref")

    # file.size_bytes — drop column if present (SQLite supports DROP COLUMN since 3.35).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(file)")}
    if "size_bytes" in cols:
        try:
            conn.execute("ALTER TABLE file DROP COLUMN size_bytes")
        except sqlite3.OperationalError:
            pass  # older sqlite — leave it; schema CREATE IF NOT EXISTS won't add it back

    # rf.source
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rf)")}
    if "source" in cols:
        try:
            conn.execute("ALTER TABLE rf DROP COLUMN source")
        except sqlite3.OperationalError:
            pass

    # index_run.error
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(index_run)")}
    if "error" in cols:
        try:
            conn.execute("ALTER TABLE index_run DROP COLUMN error")
        except sqlite3.OperationalError:
            pass

    # P2.4: add signature_hash columns if missing
    sym_cols = {r["name"] for r in conn.execute("PRAGMA table_info(symbol)")}
    if "signature_hash" not in sym_cols:
        try:
            conn.execute("ALTER TABLE symbol ADD COLUMN signature_hash TEXT")
        except sqlite3.OperationalError:
            pass
    doc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(doc)")}
    if "signature_hash_at_write" not in doc_cols:
        try:
            conn.execute("ALTER TABLE doc ADD COLUMN signature_hash_at_write TEXT")
        except sqlite3.OperationalError:
            pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_or_create_project(conn: sqlite3.Connection, name: str, root: str) -> int:
    row = conn.execute(
        "SELECT id FROM project WHERE root = ? LIMIT 1", (root,)
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO project(name, root) VALUES (?, ?)", (name, root)
    )
    return int(cur.lastrowid)
