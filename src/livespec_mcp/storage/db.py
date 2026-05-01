"""SQLite connection helpers, schema bootstrap, and migration framework.

v0.6 P2: ad-hoc `_migrate_v1_to_v2` (which had grown into v6) replaced by an
explicit ordered migration list backed by `schema_migrations`. Each
migration is a small idempotent function whose name + version are recorded
on success so subsequent connects skip already-applied work.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Callable, Iterator

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
    _run_migrations(conn)
    return conn


# ---------- Migration framework ----------

# A migration is a function (conn) -> None. Keep them small and idempotent.
# The `version` is a monotonically increasing integer; `name` is human
# readable. Once applied, a row is recorded in `schema_migrations`. Re-runs
# of `_run_migrations` skip already-recorded entries.

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})")
    )


def _try_drop_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    if not _has_column(conn, table, column):
        return
    try:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    except sqlite3.OperationalError:
        pass  # older sqlite without DROP COLUMN — leave alone


def _try_add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if _has_column(conn, table, column):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError:
        pass


def _flag_reextract(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO _migration_state(key, value) VALUES('needs_reextract', '1')"
    )


# --- Individual migrations ---


def _m001_drop_dead_tables(conn: sqlite3.Connection) -> None:
    """v1 -> v2: drop tables that were never written to."""
    conn.execute("DROP TABLE IF EXISTS commit_snapshot")
    conn.execute("DROP TABLE IF EXISTS unresolved_ref")


def _m002_drop_dead_columns(conn: sqlite3.Connection) -> None:
    """v1 -> v2: drop columns the application never reads."""
    _try_drop_column(conn, "file", "size_bytes")
    _try_drop_column(conn, "rf", "source")
    _try_drop_column(conn, "index_run", "error")


def _m003_signature_hash(conn: sqlite3.Connection) -> None:
    """P2.4: signature drift detection requires a separate hash."""
    _try_add_column(conn, "symbol", "signature_hash", "TEXT")
    _try_add_column(conn, "doc", "signature_hash_at_write", "TEXT")


def _m004_scope_module(conn: sqlite3.Connection) -> None:
    """P0.4: scope_module on symbol_ref for import-aware resolution."""
    _try_add_column(conn, "symbol_ref", "scope_module", "TEXT")


def _m005_decorators(conn: sqlite3.Connection) -> None:
    """v0.5 P1: symbol.decorators (JSON array). Queue re-extract so the field
    populates without the user having to remember --force."""
    if _has_column(conn, "symbol", "decorators"):
        return
    _try_add_column(conn, "symbol", "decorators", "TEXT")
    _flag_reextract(conn)


def _m007_visibility(conn: sqlite3.Connection) -> None:
    """v0.7 B4: symbol.visibility for Rust pub-aware dead code detection.

    Existing rows get NULL until next re-extract. Queue forced re-extract."""
    if _has_column(conn, "symbol", "visibility"):
        return
    _try_add_column(conn, "symbol", "visibility", "TEXT")
    _flag_reextract(conn)


def _m006_legacy_v02_recover(conn: sqlite3.Connection) -> None:
    """P0.2: detect a v0.2-era DB whose symbol_ref is empty even though edges
    exist. That happens when the project was indexed before the persistent
    ref table was introduced — partial reindex from such a state silently
    loses edges. Queue a one-time forced reextract."""
    has_edges = conn.execute("SELECT COUNT(*) c FROM symbol_edge").fetchone()["c"]
    has_refs = conn.execute("SELECT COUNT(*) c FROM symbol_ref").fetchone()["c"]
    has_symbols = conn.execute("SELECT COUNT(*) c FROM symbol").fetchone()["c"]
    if has_edges and has_symbols and not has_refs:
        _flag_reextract(conn)


# Ordered registry. Append-only — never reuse a version number.
MIGRATIONS: list[Migration] = [
    (1, "drop_dead_tables", _m001_drop_dead_tables),
    (2, "drop_dead_columns", _m002_drop_dead_columns),
    (3, "signature_hash", _m003_signature_hash),
    (4, "scope_module", _m004_scope_module),
    (5, "decorators", _m005_decorators),
    (6, "legacy_v02_recover", _m006_legacy_v02_recover),
    (7, "visibility", _m007_visibility),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    _ensure_migrations_table(conn)
    applied = {
        int(r["version"])
        for r in conn.execute("SELECT version FROM schema_migrations")
    }
    for version, name, fn in MIGRATIONS:
        if version in applied:
            continue
        fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES(?, ?)",
            (version, name),
        )


def consume_reextract_flag(conn: sqlite3.Connection) -> bool:
    """Return True (and clear) if a migration queued a forced re-extract."""
    row = conn.execute(
        "SELECT value FROM _migration_state WHERE key='needs_reextract'"
    ).fetchone()
    if row and row["value"] == "1":
        conn.execute("DELETE FROM _migration_state WHERE key='needs_reextract'")
        return True
    return False


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
