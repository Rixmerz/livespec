"""v0.6 P2: migration framework — `schema_migrations` table tracks applied
versions; each migration is idempotent and runs at most once per DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_fresh_db_has_spec_not_rf(tmp_path: Path):
    """v0.20: a brand-new DB gets `spec*` tables directly, never `rf*`."""
    conn = connect(tmp_path / "fresh.db")
    tables = _table_names(conn)
    assert {"spec", "spec_symbol", "spec_dependency", "spec_coverage_snapshot"} <= tables
    assert not ({"rf", "rf_symbol", "rf_dependency", "rf_coverage_snapshot"} & tables)
    conn.close()


def test_v11_rename_rf_to_spec_preserves_data(tmp_path: Path):
    """v0.20 P0: a legacy DB with populated `spec*` tables converges to
    `spec*` tables on connect, preserving existing rows (incl. the historic
    `SPEC-042`-style string id, which is NOT rewritten).

    Builds the "legacy" state by connecting normally (so every other table
    is realistic), inserting a spec row, then manually renaming `spec*` back
    to `spec*` (undoing what v11 does) and un-recording v11 — the inverse of
    what the migration performs — rather than hand-rolling a parallel schema
    that could drift from schema.sql.
    """
    db = tmp_path / "legacy.db"
    conn = connect(db)
    conn.execute(
        "INSERT INTO project(id, name, root) VALUES (1, 'p', '/tmp/p')"
    )
    conn.execute(
        "INSERT INTO file(id, project_id, path, language, content_hash, line_count, mtime) "
        "VALUES (1, 1, 'a.py', 'python', 'h', 1, 0.0)"
    )
    conn.execute(
        "INSERT INTO symbol(id, file_id, name, qualified_name, kind, start_line, end_line) "
        "VALUES (1, 1, 's', 's', 'function', 1, 1)"
    )
    conn.execute(
        "INSERT INTO spec(id, project_id, spec_id, title) VALUES (1, 1, 'SPEC-042', 'Legacy Spec')"
    )
    conn.execute("INSERT INTO spec_symbol(spec_id, symbol_id) VALUES (1, 1)")

    # Revert to the pre-v11 (legacy `rf*`) shape and forget that v11 ran.
    conn.execute("ALTER TABLE spec_dependency RENAME TO rf_dependency")
    conn.execute("ALTER TABLE rf_dependency RENAME COLUMN parent_spec_id TO parent_rf_id")
    conn.execute("ALTER TABLE rf_dependency RENAME COLUMN child_spec_id TO child_rf_id")
    conn.execute("ALTER TABLE spec_symbol RENAME TO rf_symbol")
    conn.execute("ALTER TABLE rf_symbol RENAME COLUMN spec_id TO rf_id")
    conn.execute("ALTER TABLE spec RENAME TO rf")
    conn.execute("ALTER TABLE rf RENAME COLUMN spec_id TO rf_id")
    conn.execute("DELETE FROM schema_migrations WHERE version=11")
    conn.close()

    conn = connect(db)
    tables = _table_names(conn)
    assert {"spec", "spec_symbol", "spec_dependency"} <= tables
    assert not ({"rf", "rf_symbol", "rf_dependency"} & tables)

    spec_row = conn.execute("SELECT spec_id, title, kind FROM spec WHERE id=1").fetchone()
    assert spec_row["spec_id"] == "SPEC-042"  # historic ids are NOT rewritten
    assert spec_row["title"] == "Legacy Spec"
    assert spec_row["kind"] == "functional_requirement"

    link = conn.execute("SELECT spec_id, symbol_id FROM spec_symbol WHERE id=1").fetchone()
    assert (link["spec_id"], link["symbol_id"]) == (1, 1)

    # FK still enforced against the renamed parent table.
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO spec_symbol(spec_id, symbol_id) VALUES (999, 1)"
        )
    conn.close()


def test_project_root_unique_collapses_duplicates(tmp_path: Path):
    """v0.20 H5: get_or_create_project must be race-safe — a repeated call and
    a raw duplicate INSERT both collapse to one row via UNIQUE(root)."""
    from livespec_mcp.storage.db import get_or_create_project

    conn = connect(tmp_path / "p.db")
    a = get_or_create_project(conn, name="p", root="/tmp/p")
    b = get_or_create_project(conn, name="p", root="/tmp/p")
    assert a == b
    # The UNIQUE index exists and rejects a second physical row for the root.
    idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_project_root'"
    ).fetchone()
    assert idx is not None
    conn.execute("INSERT OR IGNORE INTO project(name, root) VALUES ('p2', '/tmp/p')")
    n = conn.execute("SELECT COUNT(*) c FROM project WHERE root='/tmp/p'").fetchone()["c"]
    assert n == 1
    conn.close()


def test_m012_dedupes_preexisting_duplicate_projects(tmp_path: Path):
    """A DB that already split into two project rows for one root (created
    before v12) converges: child rows repoint to the surviving id, the
    duplicate project is dropped, and the UNIQUE index is added."""
    db = tmp_path / "dupe.db"
    # Build a v11-era DB (no idx_project_root yet), then inject a duplicate.
    conn = connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_project_root")
    conn.execute("DELETE FROM schema_migrations WHERE version IN (12, 13)")
    conn.execute("INSERT INTO project(id, name, root) VALUES (1, 'p', '/tmp/dup')")
    conn.execute("INSERT INTO project(id, name, root) VALUES (2, 'p', '/tmp/dup')")
    conn.execute("INSERT INTO file(project_id, path, language, content_hash, line_count, mtime)"
                 " VALUES (2, 'a.py', 'python', 'h', 1, 0.0)")
    conn.close()

    conn = connect(db)  # re-run migrations -> v12 dedupes
    projects = conn.execute("SELECT id FROM project WHERE root='/tmp/dup'").fetchall()
    assert len(projects) == 1 and int(projects[0]["id"]) == 1
    # the file row was repointed to the surviving project id
    fp = conn.execute("SELECT project_id FROM file WHERE path='a.py'").fetchone()
    assert int(fp["project_id"]) == 1
    conn.close()


def test_m019_drop_vec_tables_tolerates_missing_vec0_module(tmp_path: Path):
    """Real group DBs may have sqlite-vec virtual tables; DROP without the
    extension loaded raises ``no such module: vec0``. Migration must still
    apply (leave orphan tables; FTS path ignores them)."""
    from livespec_mcp.storage.db import _m019_drop_vector_search

    db = tmp_path / "with-vec-name.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chunk (id INTEGER PRIMARY KEY, embedded_at TEXT)")
    conn.execute("CREATE TABLE chunk_vec_code (id INTEGER)")
    conn.execute("CREATE TABLE chunk_vec_text (id INTEGER)")
    conn.commit()

    # Happy path: ordinary tables drop fine.
    _m019_drop_vector_search(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "chunk_vec_code" not in names
    assert "chunk_vec_text" not in names
    conn.close()

    # Failure path: DROP raises OperationalError (vec0 missing) — swallow.
    class _Vec0Missing:
        def execute(self, sql, *a):
            if isinstance(sql, str) and sql.startswith("DROP TABLE"):
                raise sqlite3.OperationalError("no such module: vec0")
            if isinstance(sql, str) and sql.startswith("PRAGMA table_info"):
                return []
            if isinstance(sql, str) and sql.startswith("ALTER TABLE"):
                return None
            raise AssertionError(f"unexpected SQL: {sql!r}")

    _m019_drop_vector_search(_Vec0Missing())  # type: ignore[arg-type]


def test_reextract_flag_survives_until_cleared(tmp_path: Path):
    """v0.20 M18: peek does not clear; clear is explicit (so a crashed index
    leaves the flag set instead of losing the forced re-extract)."""
    from livespec_mcp.storage.db import (
        _flag_reextract,
        clear_reextract_flag,
        peek_reextract_flag,
    )

    conn = connect(tmp_path / "r.db")
    _flag_reextract(conn)
    assert peek_reextract_flag(conn) is True
    assert peek_reextract_flag(conn) is True  # idempotent, not consumed
    clear_reextract_flag(conn)
    assert peek_reextract_flag(conn) is False
    conn.close()
