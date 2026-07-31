-- livespec-mcp schema v2
-- Four blocks: project, code (file/symbol), graph (edges), Specs+docs.
-- v2 changes: dropped commit_snapshot (unused), file.size_bytes, rf.source,
-- index_run.error (write-only / never written).
-- v0.20: RF renamed to Spec (broader taxonomy via `kind`); see migration
-- _m011_rename_rf_to_spec in storage/db.py for the upgrade path.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One project per root. The UNIQUE index is created by migration v12 (which
-- dedupes legacy rows first); it is intentionally NOT declared here because
-- this schema runs via executescript before migrations, and a pre-framework
-- DB whose `project` table predates the `root` column would fail the index
-- create. get_or_create_project relies on it for a race-safe INSERT OR IGNORE.

-- Migration state: persistent flags so a one-time re-extract can be queued
-- by a schema migration and consumed by the next index_project run.
CREATE TABLE IF NOT EXISTS _migration_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ===== Code =====
CREATE TABLE IF NOT EXISTS file (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    mtime REAL NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_file_project ON file(project_id);
CREATE INDEX IF NOT EXISTS idx_file_lang ON file(project_id, language);

CREATE TABLE IF NOT EXISTS symbol (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    parent_symbol_id INTEGER REFERENCES symbol(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,            -- function | class | method | module | variable
    signature TEXT,
    signature_hash TEXT,           -- xxh3 of signature; drift trigger independent of body
    docstring TEXT,
    body_hash TEXT,
    decorators TEXT,               -- JSON array of decorator/annotation names (Python, TS/JS/TSX, Java)
    visibility TEXT,               -- v0.7: pub/pub(crate)/private/exported/public/...
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    UNIQUE(file_id, qualified_name, start_line)
);

CREATE INDEX IF NOT EXISTS idx_symbol_file ON symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_symbol_qname ON symbol(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbol_name ON symbol(name);
CREATE INDEX IF NOT EXISTS idx_symbol_parent ON symbol(parent_symbol_id);

-- ===== Graph =====
CREATE TABLE IF NOT EXISTS symbol_edge (
    id INTEGER PRIMARY KEY,
    src_symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    dst_symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,       -- calls | imports | inherits | references
    weight REAL NOT NULL DEFAULT 1.0,
    UNIQUE(src_symbol_id, dst_symbol_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_edge_src ON symbol_edge(src_symbol_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON symbol_edge(dst_symbol_id, edge_type);

-- Persistent refs: every call/reference site captured during extraction.
-- We keep them on disk (rather than in-memory only) so a partial re-index
-- can re-resolve refs from UNCHANGED files when the file they target
-- changes. Without this, edges where dst is in the changed file would
-- vanish permanently. Cascade on symbol delete keeps this table consistent.
CREATE TABLE IF NOT EXISTS symbol_ref (
    id INTEGER PRIMARY KEY,
    src_symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    target_name TEXT NOT NULL,
    ref_type TEXT NOT NULL DEFAULT 'call',
    scope_module TEXT,             -- v0.6 P0.4: import-aware resolution hint (added by migration v4)
    line INTEGER
);

CREATE INDEX IF NOT EXISTS idx_symref_target ON symbol_ref(target_name);
CREATE INDEX IF NOT EXISTS idx_symref_src ON symbol_ref(src_symbol_id);

-- v0.21 P2: cross-repo route edges. HTTP routes are the join key between a
-- frontend call site (`fetch('/api/x')`) and a backend handler
-- (`@app.get('/api/x')`). Both are captured as route_ref rows (role='client'
-- vs 'server'); `_resolve_routes` matches them by `norm_path` DB-wide and
-- writes `invokes_route` edges into symbol_edge. DB-wide matching is what
-- makes it cross-project: a shared group DB (`[workspace] group_db`) holds
-- several repos, so a client in one repo links a server in another; a
-- per-repo DB has one project, so it links a monorepo's own front+back.
-- Cascade on symbol delete keeps this consistent across re-extract.
CREATE TABLE IF NOT EXISTS route_ref (
    id INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    role TEXT NOT NULL,             -- 'client' (fetch/axios/requests call) | 'server' (route handler)
    method TEXT,                    -- GET/POST/... or NULL (unknown/any)
    path TEXT NOT NULL,             -- raw route/URL string as written
    norm_path TEXT NOT NULL,        -- normalized join key: params -> {}, leading '/', no trailing '/'
    line INTEGER
);

CREATE INDEX IF NOT EXISTS idx_route_ref_symbol ON route_ref(symbol_id);
CREATE INDEX IF NOT EXISTS idx_route_ref_match ON route_ref(role, norm_path);

-- ===== Specs =====
CREATE TABLE IF NOT EXISTS module (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    UNIQUE(project_id, name)
);

-- v0.20: Spec supersedes RF. `kind` is free-text (no CHECK, same style as
-- status/priority): functional_requirement (default) | non_functional_requirement
-- | adr | design | constraint | epic | other.
CREATE TABLE IF NOT EXISTS spec (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    spec_id TEXT NOT NULL,           -- e.g. SPEC-042
    kind TEXT NOT NULL DEFAULT 'functional_requirement',
    title TEXT NOT NULL,
    description TEXT,
    module_id INTEGER REFERENCES module(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | active | deprecated
    priority TEXT NOT NULL DEFAULT 'medium',-- low | medium | high | critical
    source TEXT,                            -- openspec | markdown | NULL (manual/legacy)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, spec_id)
);

CREATE INDEX IF NOT EXISTS idx_spec_status ON spec(project_id, status);
CREATE INDEX IF NOT EXISTS idx_spec_module ON spec(module_id);

CREATE TABLE IF NOT EXISTS spec_symbol (
    id INTEGER PRIMARY KEY,
    spec_id INTEGER NOT NULL REFERENCES spec(id) ON DELETE CASCADE,
    symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'implements',  -- implements | tests | references
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'manual',        -- manual | annotation | embedding | llm
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(spec_id, symbol_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_specsym_spec ON spec_symbol(spec_id);
CREATE INDEX IF NOT EXISTS idx_specsym_sym ON spec_symbol(symbol_id);

-- v0.22 P1 (OpenSpec interop): scenarios are OpenSpec's atomic, testable unit.
-- Every OpenSpec requirement MUST carry >=1 `#### Scenario:` (WHEN/THEN) block;
-- `validate_openspec` enforces that invariant. We model them as first-class
-- rows (they were previously flattened into spec.description) so the round-trip
-- export re-emits them structurally and coverage can reason per scenario.
-- `body` is the raw markdown under the heading (the WHEN/THEN bullet list).
CREATE TABLE IF NOT EXISTS spec_scenario (
    id INTEGER PRIMARY KEY,
    spec_id INTEGER NOT NULL REFERENCES spec(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,   -- source order within the requirement
    UNIQUE(spec_id, name)
);

CREATE INDEX IF NOT EXISTS idx_spec_scenario_spec ON spec_scenario(spec_id);

-- v0.22 P3 (OpenSpec interop): scenario-level traceability. OpenSpec reasons
-- about behaviour per scenario, so this links code symbols to an individual
-- `#### Scenario:` (not just the whole requirement) — enabling per-scenario
-- "which code/test verifies this WHEN/THEN?" queries. Same shape/relations as
-- spec_symbol; cascades on scenario or symbol delete.
CREATE TABLE IF NOT EXISTS scenario_symbol (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES spec_scenario(id) ON DELETE CASCADE,
    symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'implements',  -- implements | tests | references
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'manual',        -- manual | annotation | embedding | llm
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scenario_id, symbol_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_scenario_symbol_scenario ON scenario_symbol(scenario_id);
CREATE INDEX IF NOT EXISTS idx_scenario_symbol_symbol ON scenario_symbol(symbol_id);

-- v0.5 P2: Spec dependency graph. parent depends on child.
--   requires:  parent needs child to be implemented first
--   extends:   parent specializes / refines child
--   conflicts: parent and child cannot both be active
-- Self-edges are forbidden via CHECK; cycles are prevented at insert time
-- (the link_spec_dependency tool runs a BFS to reject would-be cycles).
CREATE TABLE IF NOT EXISTS spec_dependency (
    id INTEGER PRIMARY KEY,
    parent_spec_id INTEGER NOT NULL REFERENCES spec(id) ON DELETE CASCADE,
    child_spec_id  INTEGER NOT NULL REFERENCES spec(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'requires',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(parent_spec_id, child_spec_id, kind),
    CHECK (parent_spec_id != child_spec_id)
);

CREATE INDEX IF NOT EXISTS idx_specdep_parent ON spec_dependency(parent_spec_id);
CREATE INDEX IF NOT EXISTS idx_specdep_child ON spec_dependency(child_spec_id);

-- v0.22 P2 (OpenSpec interop): change proposals. OpenSpec's `openspec/changes/
-- <name>/` package is a self-contained change request: proposal.md (why),
-- design.md (how), tasks.md (checklist) plus delta spec files under specs/ that
-- ADD/MODIFY/REMOVE/RENAME requirements. We model the package as one
-- `spec_change` row (the three prose docs inline) plus N `spec_change_delta`
-- rows (one per requirement touched). `apply_spec_change` folds the deltas into
-- the canonical `spec` set; `archive_spec_change` marks it done.
CREATE TABLE IF NOT EXISTS spec_change (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name TEXT NOT NULL,                       -- change folder name, e.g. add-dark-mode
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | applied | archived
    proposal TEXT,                            -- proposal.md content
    design TEXT,                              -- design.md content
    tasks TEXT,                               -- tasks.md content
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_spec_change_project ON spec_change(project_id, status);

CREATE TABLE IF NOT EXISTS spec_change_delta (
    id INTEGER PRIMARY KEY,
    change_id INTEGER NOT NULL REFERENCES spec_change(id) ON DELETE CASCADE,
    operation TEXT NOT NULL,        -- added | modified | removed | renamed
    capability TEXT,                -- capability (== spec module) the requirement lives in
    spec_id TEXT NOT NULL,          -- derived slug id of the target requirement
    title TEXT NOT NULL,
    description TEXT,
    rename_from TEXT,               -- for operation='renamed': old requirement's slug id
    ordinal INTEGER NOT NULL DEFAULT 0,
    UNIQUE(change_id, spec_id, operation)
);

CREATE INDEX IF NOT EXISTS idx_change_delta_change ON spec_change_delta(change_id);

-- v0.16: Spec test-coverage trend. One row per spec per audit_coverage run
-- plus a '__rollup__' row. Created here so a fresh DB matches a migrated one
-- (migration v9 created it under the legacy rf_ name, v11 renamed it).
CREATE TABLE IF NOT EXISTS spec_coverage_snapshot (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    spec_id TEXT NOT NULL,
    ratio REAL,
    verified_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_spec_cov_snap
    ON spec_coverage_snapshot(project_id, spec_id, ts);

-- v0.18: per-project agent scratch notes keyed by symbol qname (migration v10).
CREATE TABLE IF NOT EXISTS agent_scratch (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    qname TEXT NOT NULL,
    note TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, qname)
);

CREATE INDEX IF NOT EXISTS idx_agent_scratch_project ON agent_scratch(project_id);

-- ===== Docs =====
CREATE TABLE IF NOT EXISTS doc (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,      -- symbol | module | spec
    target_key TEXT NOT NULL,       -- qualified_name | module name | spec_id
    content TEXT NOT NULL,
    body_hash_at_write TEXT,        -- snapshot of symbol body_hash when generated
    signature_hash_at_write TEXT,   -- snapshot of symbol signature_hash when generated
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, target_type, target_key)
);

CREATE INDEX IF NOT EXISTS idx_doc_target ON doc(project_id, target_type, target_key);

-- ===== RAG chunks =====
CREATE TABLE IF NOT EXISTS chunk (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,         -- symbol | spec | doc | file
    source_id INTEGER,                 -- symbol.id or spec.id (nullable for doc)
    text_kind TEXT NOT NULL,           -- code | text
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunk_project ON chunk(project_id);
CREATE INDEX IF NOT EXISTS idx_chunk_source ON chunk(project_id, source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_chunk_hash ON chunk(content_hash);

-- FTS5 mirror over chunk.text. Always available (sqlite ships with FTS5).
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text, content='chunk', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunk_ai AFTER INSERT ON chunk BEGIN
    INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunk_ad AFTER DELETE ON chunk BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
-- Only rewrite FTS when the text actually changed (not on metadata-only updates).
CREATE TRIGGER IF NOT EXISTS chunk_au AFTER UPDATE ON chunk
WHEN old.text IS NOT new.text BEGIN
    INSERT INTO chunk_fts(chunk_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ===== Index control =====
CREATE TABLE IF NOT EXISTS index_run (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    files_total INTEGER DEFAULT 0,
    files_changed INTEGER DEFAULT 0,
    symbols_total INTEGER DEFAULT 0,
    edges_total INTEGER DEFAULT 0
);
