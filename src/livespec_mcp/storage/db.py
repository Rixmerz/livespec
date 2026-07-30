"""SQLite connection helpers, schema bootstrap, and migration framework.

v0.6 P2: ad-hoc `_migrate_v1_to_v2` (which had grown into v6) replaced by an
explicit ordered migration list backed by `schema_migrations`. Each
migration is a small idempotent function whose name + version are recorded
on success so subsequent connects skip already-applied work.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

_SCHEMA_CACHE: str | None = None


def _schema_sql() -> str:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with resources.files("livespec_mcp.storage").joinpath("schema.sql").open() as f:
            _SCHEMA_CACHE = f.read()
    return _SCHEMA_CACHE


def connect(db_path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open ``db_path``.

    ``create=True`` (default): today's behaviour — makes the parent dir,
    opens read-write, bootstraps schema + migrations. Used only where a
    missing DB should be materialized (``index_project``, the ``index`` CLI
    command, and any caller that already confirmed the file exists — schema
    bootstrap/migrations are idempotent no-ops on an up-to-date DB).

    ``create=False``: strict read-only primitive for callers that must NEVER
    create a file. Raises ``FileNotFoundError`` if ``db_path`` doesn't exist;
    otherwise opens via SQLite's ``mode=ro`` URI (no schema/migration writes
    attempted). Not currently wired into ``get_state`` — see ``state.py``'s
    ``get_state(..., create=...)`` for why (existing mutation tools share the
    same default connection path and need read-write access to a DB that
    already exists).
    """
    if not create:
        if not db_path.is_file():
            raise FileNotFoundError(f"No index database at {db_path}")
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # WAL gives concurrent readers for free but still allows only one writer.
    # Cross-process writers (the `livespec-mcp index` CLI, `explorer install`)
    # and a cold full index that holds the write lock for minutes would
    # otherwise hit `database is locked` after Python's 5s default. Wait up
    # to 30s for the lock instead of erroring.
    conn.execute("PRAGMA busy_timeout = 30000")
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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
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


def _m008_ts_java_decorators(conn: sqlite3.Connection) -> None:
    """v0.13 P1: extractor now fills symbol.decorators for TS/JS/TSX
    (`decorator` nodes) and Java (annotations). No schema change — existing
    rows for those languages carry NULL until re-extract. Queue it."""
    _flag_reextract(conn)


def _m009_rf_coverage_snapshot(conn: sqlite3.Connection) -> None:
    """v0.16 P D: RF test-coverage trend table.

    Each ``audit_coverage`` run appends one row per RF (its
    ``test_coverage_ratio``) plus one rollup row (``rf_id='__rollup__'``)
    carrying the snapshot avg in ``ratio`` and the verified-RF count in
    ``verified_count``. Read back chronologically via
    ``storage/trends.py:read_trend``. New empty table — no re-extract needed.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS rf_coverage_snapshot (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            ts TEXT NOT NULL,
            rf_id TEXT NOT NULL,
            ratio REAL,
            verified_count INTEGER
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_rf_cov_snap
           ON rf_coverage_snapshot(project_id, rf_id, ts)"""
    )


def _m010_agent_scratch(conn: sqlite3.Connection) -> None:
    """v0.18: per-project agent scratch notes keyed by symbol qname."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_scratch (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            qname TEXT NOT NULL,
            note TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(project_id, qname)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_agent_scratch_project
           ON agent_scratch(project_id)"""
    )


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


def _m011_rename_rf_to_spec(conn: sqlite3.Connection) -> None:
    """v0.20: RF nomenclature renamed to Spec + new `kind` taxonomy column.

    `connect()` runs `schema.sql` (which now defines `spec`/`spec_symbol`/
    `spec_dependency`/`spec_coverage_snapshot`) via `executescript` BEFORE
    migrations run. On a DB that still has the legacy `rf` tables, that
    leaves empty `spec*` shells sitting next to the populated `rf*` tables.
    Drop those empty shells, then rename the legacy tables/columns in place
    so existing rows (and their ids) survive untouched — only the container
    names change. SQLite (3.25+) rewrites FK/CHECK/index definitions that
    reference a renamed table or column automatically.

    Existing `rf_id` string values (e.g. ``RF-042``) are preserved as-is in
    the renamed `spec_id` column — only newly created specs get the
    ``SPEC-NNN`` format (see `_next_spec_id` in tools/specs.py).

    Note: on a brand-new database, migrations 1-11 all run in the same
    first `connect()` — `_m009_rf_coverage_snapshot` still unconditionally
    creates the legacy-named `rf_coverage_snapshot` table even though `rf`
    itself was never created (schema.sql defines `spec` directly). That's
    why the coverage-snapshot rename below is guarded independently of the
    main `rf` table check.
    """
    if _table_exists(conn, "rf"):
        conn.execute("DROP TABLE IF EXISTS spec_dependency")
        conn.execute("DROP TABLE IF EXISTS spec_symbol")
        conn.execute("DROP TABLE IF EXISTS spec")

        conn.execute("ALTER TABLE rf RENAME TO spec")
        conn.execute("ALTER TABLE spec RENAME COLUMN rf_id TO spec_id")
        _try_add_column(conn, "spec", "kind", "TEXT NOT NULL DEFAULT 'functional_requirement'")

        conn.execute("ALTER TABLE rf_symbol RENAME TO spec_symbol")
        conn.execute("ALTER TABLE spec_symbol RENAME COLUMN rf_id TO spec_id")

        conn.execute("ALTER TABLE rf_dependency RENAME TO spec_dependency")
        conn.execute("ALTER TABLE spec_dependency RENAME COLUMN parent_rf_id TO parent_spec_id")
        conn.execute("ALTER TABLE spec_dependency RENAME COLUMN child_rf_id TO child_spec_id")

        for old_index in (
            "idx_rf_status", "idx_rf_module", "idx_rfsym_rf", "idx_rfsym_sym",
            "idx_rfdep_parent", "idx_rfdep_child",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {old_index}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_spec_status ON spec(project_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_spec_module ON spec(module_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_specsym_spec ON spec_symbol(spec_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_specsym_sym ON spec_symbol(symbol_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_specdep_parent ON spec_dependency(parent_spec_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_specdep_child ON spec_dependency(child_spec_id)")

        conn.execute("UPDATE doc SET target_type='spec' WHERE target_type='requirement'")
        conn.execute("UPDATE chunk SET source_type='spec' WHERE source_type='requirement'")

    if _table_exists(conn, "rf_coverage_snapshot"):
        conn.execute("DROP TABLE IF EXISTS spec_coverage_snapshot")
        conn.execute("ALTER TABLE rf_coverage_snapshot RENAME TO spec_coverage_snapshot")
        conn.execute("ALTER TABLE spec_coverage_snapshot RENAME COLUMN rf_id TO spec_id")
        conn.execute("DROP INDEX IF EXISTS idx_rf_cov_snap")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_spec_cov_snap
               ON spec_coverage_snapshot(project_id, spec_id, ts)"""
        )


def _m012_unique_project_root(conn: sqlite3.Connection) -> None:
    """v0.20: enforce UNIQUE(project.root).

    ``get_or_create_project`` was SELECT-then-INSERT with no constraint, so a
    race (two threads, or the MCP server + `livespec-mcp index` CLI in another
    process) could create two ``project`` rows for the same root — after which
    a bare ``SELECT ... WHERE root=? LIMIT 1`` returned an arbitrary one and
    silently split symbols, specs, and coverage across two project_ids.

    Dedup any existing duplicates first (repoint child rows to the lowest id,
    delete the rest), then add the UNIQUE index so future inserts collapse via
    ``INSERT OR IGNORE``.
    """
    if not _has_column(conn, "project", "root"):
        # Contrived pre-framework DBs may predate the `root` column entirely.
        # Nothing to enforce uniqueness on — record the migration and move on.
        return
    dupes = conn.execute(
        """SELECT root, MIN(id) AS keep_id, COUNT(*) AS n
           FROM project GROUP BY root HAVING n > 1"""
    ).fetchall()
    # Child tables that carry a project_id and could have been split.
    child_tables = (
        "file", "module", "spec", "doc", "chunk", "index_run",
        "agent_scratch", "spec_coverage_snapshot",
    )
    for row in dupes:
        root, keep_id = row["root"], int(row["keep_id"])
        others = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM project WHERE root=? AND id != ?", (root, keep_id)
            )
        ]
        for other_id in others:
            for table in child_tables:
                if _table_exists(conn, table) and _has_column(conn, table, "project_id"):
                    conn.execute(
                        f"UPDATE OR IGNORE {table} SET project_id=? WHERE project_id=?",
                        (keep_id, other_id),
                    )
            conn.execute("DELETE FROM project WHERE id=?", (other_id,))
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_root ON project(root)"
    )


def _m013_chunk_au_guard(conn: sqlite3.Connection) -> None:
    """v0.20: the chunk_au trigger fired the full FTS delete+reinsert on ANY
    UPDATE, so `embed_pending`'s per-chunk `SET embedded_at=...` rewrote the
    entire FTS index thousands of times per embed run. Recreate the trigger
    to only touch FTS when the text actually changed."""
    conn.execute("DROP TRIGGER IF EXISTS chunk_au")
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS chunk_au AFTER UPDATE ON chunk
           WHEN old.text IS NOT new.text BEGIN
               INSERT INTO chunk_fts(chunk_fts, rowid, text)
                   VALUES('delete', old.id, old.text);
               INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
           END"""
    )


def _m014_route_ref(conn: sqlite3.Connection) -> None:
    """v0.21 P2: route_ref table for cross-repo route edges.

    New table (schema.sql already created it on fresh DBs). On an existing DB,
    create it here and queue a forced re-extract so the new client/server
    route rows populate on the next index_project without needing --force."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS route_ref (
            id INTEGER PRIMARY KEY,
            symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            method TEXT,
            path TEXT NOT NULL,
            norm_path TEXT NOT NULL,
            line INTEGER
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_route_ref_symbol ON route_ref(symbol_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_route_ref_match ON route_ref(role, norm_path)"
    )
    # Only queue a re-extract on a DB that already has symbols (an existing
    # project). A brand-new DB runs every migration inside the first connect()
    # before any indexing — no need to force anything there.
    if conn.execute("SELECT 1 FROM symbol LIMIT 1").fetchone() is not None:
        _flag_reextract(conn)


def _m015_spec_scenario(conn: sqlite3.Connection) -> None:
    """v0.22 P1 (OpenSpec interop): spec_scenario table.

    Scenarios are OpenSpec's atomic testable unit (one `#### Scenario:`
    WHEN/THEN block per requirement). Previously they were flattened into
    ``spec.description``; this promotes them to first-class rows so the
    round-trip exporter re-emits them structurally and ``validate_openspec``
    can enforce the requirement-must-have-a-scenario invariant. New empty
    table — no re-extract needed (populated on next spec import)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS spec_scenario (
            id INTEGER PRIMARY KEY,
            spec_id INTEGER NOT NULL REFERENCES spec(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            ordinal INTEGER NOT NULL DEFAULT 0,
            UNIQUE(spec_id, name)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_spec_scenario_spec ON spec_scenario(spec_id)"
    )


def _m016_spec_change(conn: sqlite3.Connection) -> None:
    """v0.22 P2 (OpenSpec interop): spec_change + spec_change_delta tables.

    Models OpenSpec's ``openspec/changes/<name>/`` package (proposal/design/
    tasks + delta requirements) so the propose -> apply -> archive lifecycle is
    representable. New empty tables — no re-extract needed."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS spec_change (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            proposal TEXT,
            design TEXT,
            tasks TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(project_id, name)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_spec_change_project ON spec_change(project_id, status)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS spec_change_delta (
            id INTEGER PRIMARY KEY,
            change_id INTEGER NOT NULL REFERENCES spec_change(id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            capability TEXT,
            spec_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            ordinal INTEGER NOT NULL DEFAULT 0,
            UNIQUE(change_id, spec_id, operation)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_change_delta_change ON spec_change_delta(change_id)"
    )


def _m017_scenario_symbol(conn: sqlite3.Connection) -> None:
    """v0.22 P3 (OpenSpec interop): scenario_symbol table for scenario-level
    traceability (link code symbols to an individual `#### Scenario:`, not just
    the whole requirement). New empty table — no re-extract needed."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scenario_symbol (
            id INTEGER PRIMARY KEY,
            scenario_id INTEGER NOT NULL REFERENCES spec_scenario(id) ON DELETE CASCADE,
            symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
            relation TEXT NOT NULL DEFAULT 'implements',
            confidence REAL NOT NULL DEFAULT 1.0,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(scenario_id, symbol_id, relation)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scenario_symbol_scenario ON scenario_symbol(scenario_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scenario_symbol_symbol ON scenario_symbol(symbol_id)"
    )


def _m018_change_delta_rename_from(conn: sqlite3.Connection) -> None:
    """v0.22 P (Tier 2): spec_change_delta.rename_from for OpenSpec RENAMED deltas.

    A ``## RENAMED Requirements`` delta carries FROM/TO names; this column stores
    the old requirement's slug id so ``apply_spec_change`` can migrate its
    traceability links to the renamed spec and drop the old one. Additive."""
    _try_add_column(conn, "spec_change_delta", "rename_from", "TEXT")


def _m019_drop_vector_search(conn: sqlite3.Connection) -> None:
    """Drop dense-vector search (sqlite-vec + embedded_at).

    Search is FTS5-only. Best-effort DROP of vec0 tables and the unused
    ``chunk.embedded_at`` column. No re-extract required.

    ``DROP TABLE`` on a ``vec0`` virtual table requires the sqlite-vec
    extension to be loaded; without it SQLite raises ``no such module: vec0``.
    Leave those tables in place — nothing in the FTS path reads them.
    """
    for tbl in ("chunk_vec_code", "chunk_vec_text"):
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        except sqlite3.OperationalError:
            # Extension not loaded (or table is a broken vec0 shadow).
            pass
    if _has_column(conn, "chunk", "embedded_at"):
        try:
            conn.execute("ALTER TABLE chunk DROP COLUMN embedded_at")
        except sqlite3.OperationalError:
            # Older SQLite without DROP COLUMN — leave the unused column.
            pass


# Ordered registry. Append-only — never reuse a version number.
MIGRATIONS: list[Migration] = [
    (1, "drop_dead_tables", _m001_drop_dead_tables),
    (2, "drop_dead_columns", _m002_drop_dead_columns),
    (3, "signature_hash", _m003_signature_hash),
    (4, "scope_module", _m004_scope_module),
    (5, "decorators", _m005_decorators),
    (6, "legacy_v02_recover", _m006_legacy_v02_recover),
    (7, "visibility", _m007_visibility),
    (8, "ts_java_decorators_reextract", _m008_ts_java_decorators),
    (9, "rf_coverage_snapshot", _m009_rf_coverage_snapshot),
    (10, "agent_scratch", _m010_agent_scratch),
    (11, "rename_rf_to_spec", _m011_rename_rf_to_spec),
    (12, "unique_project_root", _m012_unique_project_root),
    (13, "chunk_au_guard", _m013_chunk_au_guard),
    (14, "route_ref", _m014_route_ref),
    (15, "spec_scenario", _m015_spec_scenario),
    (16, "spec_change", _m016_spec_change),
    (17, "scenario_symbol", _m017_scenario_symbol),
    (18, "change_delta_rename_from", _m018_change_delta_rename_from),
    (19, "drop_vector_search", _m019_drop_vector_search),
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
        # Each migration + its bookkeeping row is one atomic unit. SQLite has
        # transactional DDL, so a crash mid-migration rolls the whole step
        # back — critical for destructive renames like _m011 whose guard is
        # skip-on-rerun (a half-applied rename would strand data forever).
        with transaction(conn):
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES(?, ?)",
                (version, name),
            )


def consume_reextract_flag(conn: sqlite3.Connection) -> bool:
    """Return True (and clear) if a migration queued a forced re-extract.

    Prefer :func:`peek_reextract_flag` + :func:`clear_reextract_flag` in the
    indexer so the flag survives a crashed/rolled-back run — clearing it up
    front used to leave migration-added columns (visibility, decorators) NULL
    forever if the forced run then failed.
    """
    if peek_reextract_flag(conn):
        clear_reextract_flag(conn)
        return True
    return False


def peek_reextract_flag(conn: sqlite3.Connection) -> bool:
    """True if a migration queued a forced re-extract, WITHOUT clearing it."""
    row = conn.execute(
        "SELECT value FROM _migration_state WHERE key='needs_reextract'"
    ).fetchone()
    return bool(row and row["value"] == "1")


def clear_reextract_flag(conn: sqlite3.Connection) -> None:
    """Clear the forced-re-extract flag (call only after a successful index)."""
    conn.execute("DELETE FROM _migration_state WHERE key='needs_reextract'")


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
        "SELECT id FROM project WHERE root = ? ORDER BY id LIMIT 1", (root,)
    ).fetchone()
    if row:
        return int(row["id"])
    # INSERT OR IGNORE + re-SELECT: with the UNIQUE(root) index (migration v12)
    # a lost race collapses to the winning row instead of creating a duplicate
    # project that would silently split all project-scoped data. ORDER BY id
    # makes the resolution deterministic even on a pre-v12 DB with existing
    # duplicates that the migration hasn't yet deduped.
    conn.execute(
        "INSERT OR IGNORE INTO project(name, root) VALUES (?, ?)", (name, root)
    )
    row = conn.execute(
        "SELECT id FROM project WHERE root = ? ORDER BY id LIMIT 1", (root,)
    ).fetchone()
    return int(row["id"])
