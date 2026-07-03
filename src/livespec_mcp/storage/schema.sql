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
    line INTEGER
);

CREATE INDEX IF NOT EXISTS idx_symref_target ON symbol_ref(target_name);
CREATE INDEX IF NOT EXISTS idx_symref_src ON symbol_ref(src_symbol_id);

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
    content_hash TEXT NOT NULL,
    embedded_at TEXT
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
CREATE TRIGGER IF NOT EXISTS chunk_au AFTER UPDATE ON chunk BEGIN
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
