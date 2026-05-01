-- livespec-mcp schema v2
-- Four blocks: project, code (file/symbol), graph (edges), RFs+docs.
-- v2 changes: dropped commit_snapshot (unused), file.size_bytes, rf.source,
-- index_run.error (write-only / never written).

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
    decorators TEXT,               -- JSON array of decorator names (Python today; extensible)
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

-- ===== Requirements =====
CREATE TABLE IF NOT EXISTS module (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS rf (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    rf_id TEXT NOT NULL,             -- e.g. RF-042
    title TEXT NOT NULL,
    description TEXT,
    module_id INTEGER REFERENCES module(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | active | deprecated
    priority TEXT NOT NULL DEFAULT 'medium',-- low | medium | high | critical
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, rf_id)
);

CREATE INDEX IF NOT EXISTS idx_rf_status ON rf(project_id, status);
CREATE INDEX IF NOT EXISTS idx_rf_module ON rf(module_id);

CREATE TABLE IF NOT EXISTS rf_symbol (
    id INTEGER PRIMARY KEY,
    rf_id INTEGER NOT NULL REFERENCES rf(id) ON DELETE CASCADE,
    symbol_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'implements',  -- implements | tests | references
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'manual',        -- manual | annotation | embedding | llm
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(rf_id, symbol_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_rfsym_rf ON rf_symbol(rf_id);
CREATE INDEX IF NOT EXISTS idx_rfsym_sym ON rf_symbol(symbol_id);

-- ===== Docs =====
CREATE TABLE IF NOT EXISTS doc (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,      -- symbol | module | requirement
    target_key TEXT NOT NULL,       -- qualified_name | module name | rf_id
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
    source_type TEXT NOT NULL,         -- symbol | requirement | doc | file
    source_id INTEGER,                 -- symbol.id or rf.id (nullable for doc)
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
