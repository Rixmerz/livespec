"""v0.6 P2: migration framework — `schema_migrations` table tracks applied
versions; each migration is idempotent and runs at most once per DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from livespec_mcp.storage.db import MIGRATIONS, _run_migrations, connect


def test_migrations_recorded_on_first_connect(tmp_path: Path):
    db = tmp_path / "x.db"
    conn = connect(db)
    rows = conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    versions = [r["version"] for r in rows]
    assert versions == [v for v, _, _ in MIGRATIONS], (
        f"every migration should be recorded on first connect. got {versions}"
    )
    conn.close()


def test_migrations_are_idempotent(tmp_path: Path):
    """Calling _run_migrations a second time should not duplicate rows or run
    individual migration functions twice."""
    db = tmp_path / "x.db"
    conn = connect(db)
    before = conn.execute("SELECT COUNT(*) c FROM schema_migrations").fetchone()["c"]
    # Re-run manually
    _run_migrations(conn)
    after = conn.execute("SELECT COUNT(*) c FROM schema_migrations").fetchone()["c"]
    assert before == after == len(MIGRATIONS)
    conn.close()


def test_migration_order_is_monotonic():
    """Versions must be strictly increasing — no reuse, no out-of-order."""
    versions = [v for v, _, _ in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions)), "duplicate version numbers"


def test_legacy_db_picks_up_missing_migrations(tmp_path: Path):
    """A DB that was created before the framework existed (no
    schema_migrations table) should converge on first connect: framework
    creates the tracking table, then runs every registered migration."""
    db = tmp_path / "legacy.db"
    # Simulate an old DB: schema only, no schema_migrations.
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE project (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()

    conn = connect(db)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert len(rows) == len(MIGRATIONS)
    conn.close()
