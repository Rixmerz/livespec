"""RAG layer: AST-aware chunking, optional dual-model embeddings, hybrid search.

Embeddings are optional. When `fastembed` and `sqlite-vec` are present we add a
vector lane to the search; otherwise we fall back to FTS5 + BM25 keyword search,
which still benefits from AST chunking (chunks preserve symbol boundaries).

Models when enabled (via `pip install -e .[embeddings]`):
  - jinaai/jina-embeddings-v2-base-code   (768d, code)
  - intfloat/multilingual-e5-base         (768d, EN+ES text)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import xxhash

CODE_CHUNK_MAX_TOKENS = 1500
CODE_CHUNK_MIN_TOKENS = 60
TEXT_CHUNK_MAX_TOKENS = 800


@dataclass
class Chunk:
    source_type: str
    source_id: int | None
    text_kind: str
    text: str
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    @property
    def content_hash(self) -> str:
        return xxhash.xxh3_128_hexdigest(self.text.encode("utf-8", errors="replace"))


# ---------- Chunking ----------


def _approx_tokens(s: str) -> int:
    # 1 token ~= 4 chars heuristic
    return max(1, len(s) // 4)


def chunk_symbol(symbol_row: sqlite3.Row, source_text: str | None) -> list[Chunk]:
    """Build a code chunk for a symbol, prefixed with file/module context.

    cAST principle: keep symbol boundaries intact. Functions/methods become a
    single chunk if under the budget; otherwise we split on blank-line groups.
    """
    qname = symbol_row["qualified_name"]
    sig = symbol_row["signature"] or ""
    doc = symbol_row["docstring"] or ""
    body = source_text or ""
    header = f"# {qname}\n# Kind: {symbol_row['kind']}\n# File: {symbol_row['file_path']}\n"
    if sig:
        header += f"# Signature: {sig}\n"
    if doc:
        header += f"# Doc:\n# {doc.replace(chr(10), chr(10) + '# ')}\n"

    full = header + "\n" + body
    if _approx_tokens(full) <= CODE_CHUNK_MAX_TOKENS:
        return [
            Chunk(
                source_type="symbol",
                source_id=int(symbol_row["id"]),
                text_kind="code",
                text=full,
                file_path=symbol_row["file_path"],
                start_line=symbol_row["start_line"],
                end_line=symbol_row["end_line"],
            )
        ]

    # Split body on double newlines, repack while preserving header on each chunk
    pieces = body.split("\n\n")
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    line = symbol_row["start_line"]
    chunk_start = line
    for piece in pieces:
        t = _approx_tokens(piece)
        if buf_tokens + t > CODE_CHUNK_MAX_TOKENS and buf:
            text = header + "\n" + "\n\n".join(buf)
            chunks.append(
                Chunk(
                    source_type="symbol",
                    source_id=int(symbol_row["id"]),
                    text_kind="code",
                    text=text,
                    file_path=symbol_row["file_path"],
                    start_line=chunk_start,
                    end_line=line,
                )
            )
            buf = [piece]
            buf_tokens = t
            chunk_start = line + 1
        else:
            buf.append(piece)
            buf_tokens += t
        line += piece.count("\n") + 2
    if buf:
        text = header + "\n" + "\n\n".join(buf)
        chunks.append(
            Chunk(
                source_type="symbol",
                source_id=int(symbol_row["id"]),
                text_kind="code",
                text=text,
                file_path=symbol_row["file_path"],
                start_line=chunk_start,
                end_line=symbol_row["end_line"],
            )
        )
    return chunks


def chunk_requirement(rf_row: sqlite3.Row) -> list[Chunk]:
    desc = rf_row["description"] or ""
    text = f"# {rf_row['rf_id']}: {rf_row['title']}\n\n{desc}".strip()
    if _approx_tokens(text) <= TEXT_CHUNK_MAX_TOKENS:
        return [
            Chunk(
                source_type="requirement",
                source_id=int(rf_row["id"]),
                text_kind="text",
                text=text,
            )
        ]
    # Naive split for very long RFs
    parts = text.split("\n\n")
    out: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    for p in parts:
        t = _approx_tokens(p)
        if buf_tokens + t > TEXT_CHUNK_MAX_TOKENS and buf:
            out.append(
                Chunk(
                    source_type="requirement",
                    source_id=int(rf_row["id"]),
                    text_kind="text",
                    text="\n\n".join(buf),
                )
            )
            buf = [p]
            buf_tokens = t
        else:
            buf.append(p)
            buf_tokens += t
    if buf:
        out.append(
            Chunk(
                source_type="requirement",
                source_id=int(rf_row["id"]),
                text_kind="text",
                text="\n\n".join(buf),
            )
        )
    return out


# ---------- Embeddings (optional) ----------


_CODE_EMBEDDER = None
_TEXT_EMBEDDER = None


def have_embeddings() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _code_embedder():
    global _CODE_EMBEDDER
    if _CODE_EMBEDDER is None:
        from fastembed import TextEmbedding

        _CODE_EMBEDDER = TextEmbedding(model_name="jinaai/jina-embeddings-v2-base-code")
    return _CODE_EMBEDDER


def _text_embedder():
    global _TEXT_EMBEDDER
    if _TEXT_EMBEDDER is None:
        from fastembed import TextEmbedding

        _TEXT_EMBEDDER = TextEmbedding(model_name="intfloat/multilingual-e5-base")
    return _TEXT_EMBEDDER


def embed_texts(texts: list[str], kind: str) -> list[list[float]]:
    if not have_embeddings():
        return [[] for _ in texts]
    embedder = _code_embedder() if kind == "code" else _text_embedder()
    return [list(v) for v in embedder.embed(texts)]


# ---------- Vector store (sqlite-vec) ----------


def have_sqlite_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def ensure_vec_tables(conn: sqlite3.Connection) -> None:
    """Create vec0 virtual tables if sqlite-vec is loaded."""
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec_code USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[768]
        )"""
    )
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec_text USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[768]
        )"""
    )


# ---------- Persistence ----------


def upsert_chunks(conn: sqlite3.Connection, project_id: int, chunks: Iterable[Chunk]) -> list[int]:
    ids: list[int] = []
    for ch in chunks:
        h = ch.content_hash
        existing = conn.execute(
            """SELECT id FROM chunk
               WHERE project_id=? AND source_type=? AND source_id IS ? AND content_hash=?""",
            (project_id, ch.source_type, ch.source_id, h),
        ).fetchone()
        if existing:
            ids.append(int(existing["id"]))
            continue
        # Replace any prior chunks for this source
        conn.execute(
            "DELETE FROM chunk WHERE project_id=? AND source_type=? AND source_id IS ?",
            (project_id, ch.source_type, ch.source_id),
        )
        cur = conn.execute(
            """INSERT INTO chunk(project_id, source_type, source_id, text_kind, file_path,
                start_line, end_line, text, content_hash)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                project_id,
                ch.source_type,
                ch.source_id,
                ch.text_kind,
                ch.file_path,
                ch.start_line,
                ch.end_line,
                ch.text,
                h,
            ),
        )
        ids.append(int(cur.lastrowid))
    return ids


def rebuild_chunks(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """Re-chunk every symbol and RF for the project. Idempotent."""
    # Wipe existing chunks
    conn.execute("DELETE FROM chunk WHERE project_id=?", (project_id,))

    workspace = Path(
        conn.execute("SELECT root FROM project WHERE id=?", (project_id,)).fetchone()["root"]
    )

    sym_count = 0
    rf_count = 0

    # Symbols
    rows = conn.execute(
        """SELECT s.id, s.qualified_name, s.kind, s.signature, s.docstring,
                  s.start_line, s.end_line, f.path AS file_path
           FROM symbol s JOIN file f ON f.id=s.file_id
           WHERE f.project_id=?""",
        (project_id,),
    ).fetchall()
    for r in rows:
        path = workspace / r["file_path"]
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            body = "\n".join(lines[max(r["start_line"] - 1, 0) : min(r["end_line"], len(lines))])
        except OSError:
            body = ""
        chunks = chunk_symbol(r, body)
        upsert_chunks(conn, project_id, chunks)
        sym_count += len(chunks)

    # Requirements
    rfs = conn.execute(
        "SELECT id, rf_id, title, description FROM rf WHERE project_id=?", (project_id,)
    ).fetchall()
    for r in rfs:
        chunks = chunk_requirement(r)
        upsert_chunks(conn, project_id, chunks)
        rf_count += len(chunks)

    return {"symbol_chunks": sym_count, "requirement_chunks": rf_count}


def embed_pending(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """Embed any chunk that has no embedding yet. Requires fastembed + sqlite-vec.

    Returns counts. No-op if embeddings stack missing.
    """
    if not have_embeddings():
        return {"skipped": 1, "reason": "fastembed not installed"}
    if not have_sqlite_vec(conn):
        return {"skipped": 1, "reason": "sqlite-vec not installed"}
    ensure_vec_tables(conn)
    code_rows = conn.execute(
        """SELECT id, text FROM chunk
           WHERE project_id=? AND text_kind='code' AND embedded_at IS NULL""",
        (project_id,),
    ).fetchall()
    text_rows = conn.execute(
        """SELECT id, text FROM chunk
           WHERE project_id=? AND text_kind='text' AND embedded_at IS NULL""",
        (project_id,),
    ).fetchall()

    def _embed(rows, kind, table):
        if not rows:
            return 0
        ids = [int(r["id"]) for r in rows]
        texts = [r["text"] for r in rows]
        vecs = embed_texts(texts, kind)
        for cid, vec in zip(ids, vecs):
            conn.execute(
                f"INSERT OR REPLACE INTO {table}(chunk_id, embedding) VALUES(?, ?)",
                (cid, _floats_blob(vec)),
            )
            conn.execute(
                "UPDATE chunk SET embedded_at = datetime('now') WHERE id=?", (cid,)
            )
        return len(ids)

    code_n = _embed(code_rows, "code", "chunk_vec_code")
    text_n = _embed(text_rows, "text", "chunk_vec_text")
    return {"code_embedded": code_n, "text_embedded": text_n}


def _floats_blob(vec: list[float]) -> bytes:
    import struct

    return struct.pack(f"{len(vec)}f", *vec)


# ---------- Hybrid search ----------


def fts_search(
    conn: sqlite3.Connection, project_id: int, query: str, limit: int, scope: str
) -> list[tuple[int, float, dict]]:
    """FTS5 over chunks. Returns (chunk_id, bm25_score, payload).

    Lower bm25 = better; we invert to score = 1 / (1 + bm25) so larger is better.
    """
    if not query.strip():
        return []
    # FTS5 defaults to AND between bare tokens. Use explicit OR so natural-language
    # queries return useful results when not every term matches every chunk.
    tokens = [t.replace('"', "").replace("*", "").strip() for t in query.split()]
    tokens = [t for t in tokens if t and t.isalnum()]
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)
    sql = [
        """SELECT c.id, c.source_type, c.source_id, c.file_path, c.start_line, c.end_line,
                  c.text_kind, substr(c.text, 1, 240) AS snippet, bm25(chunk_fts) AS bm
           FROM chunk_fts JOIN chunk c ON c.id = chunk_fts.rowid
           WHERE chunk_fts MATCH ? AND c.project_id = ?"""
    ]
    args: list = [fts_query, project_id]
    if scope == "code":
        sql.append("AND c.text_kind='code'")
    elif scope == "requirements":
        sql.append("AND c.source_type='requirement'")
    sql.append("ORDER BY bm LIMIT ?")
    args.append(limit * 3)
    out: list[tuple[int, float, dict]] = []
    try:
        rows = conn.execute(" ".join(sql), args).fetchall()
    except sqlite3.OperationalError:
        return []
    for r in rows:
        score = 1.0 / (1.0 + float(r["bm"]))
        out.append((int(r["id"]), score, dict(r)))
    return out


def vec_search(
    conn: sqlite3.Connection, project_id: int, query: str, limit: int, scope: str
) -> list[tuple[int, float, dict]]:
    """Vector search via sqlite-vec. Returns (chunk_id, score, payload)."""
    if not have_embeddings() or not have_sqlite_vec(conn):
        return []
    ensure_vec_tables(conn)
    code_q = list(embed_texts([query], "code")[0]) if scope in ("all", "code") else None
    text_q = list(embed_texts([query], "text")[0]) if scope in ("all", "requirements") else None
    out: list[tuple[int, float, dict]] = []
    if code_q:
        rows = conn.execute(
            """SELECT v.chunk_id, v.distance, c.source_type, c.source_id, c.file_path,
                      c.start_line, c.end_line, c.text_kind,
                      substr(c.text, 1, 240) AS snippet
               FROM chunk_vec_code v JOIN chunk c ON c.id = v.chunk_id
               WHERE v.embedding MATCH ? AND k = ?
                 AND c.project_id = ?""",
            (_floats_blob(code_q), limit * 2, project_id),
        ).fetchall()
        for r in rows:
            score = 1.0 / (1.0 + float(r["distance"]))
            out.append((int(r["chunk_id"]), score, dict(r)))
    if text_q:
        rows = conn.execute(
            """SELECT v.chunk_id, v.distance, c.source_type, c.source_id, c.file_path,
                      c.start_line, c.end_line, c.text_kind,
                      substr(c.text, 1, 240) AS snippet
               FROM chunk_vec_text v JOIN chunk c ON c.id = v.chunk_id
               WHERE v.embedding MATCH ? AND k = ?
                 AND c.project_id = ?""",
            (_floats_blob(text_q), limit * 2, project_id),
        ).fetchall()
        for r in rows:
            score = 1.0 / (1.0 + float(r["distance"]))
            out.append((int(r["chunk_id"]), score, dict(r)))
    return out


def reciprocal_rank_fusion(
    *result_lists: list[tuple[int, float, dict]],
    k: int = 60,
) -> list[tuple[int, float, dict]]:
    """RRF: combine multiple ranked lists. Returns merged list sorted by score."""
    scores: dict[int, float] = {}
    payloads: dict[int, dict] = {}
    for results in result_lists:
        for rank, (cid, _score, payload) in enumerate(results):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in payloads:
                payloads[cid] = payload
    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(cid, score, payloads[cid]) for cid, score in merged]


def hybrid_search(
    conn: sqlite3.Connection, project_id: int, query: str, scope: str, limit: int
) -> list[dict]:
    fts = fts_search(conn, project_id, query, limit, scope)
    vec = vec_search(conn, project_id, query, limit, scope)
    if vec:
        merged = reciprocal_rank_fusion(fts, vec)
    else:
        merged = fts
    out = []
    for cid, score, payload in merged[:limit]:
        out.append({
            "chunk_id": cid,
            "score": round(float(score), 6),
            "source_type": payload.get("source_type"),
            "source_id": payload.get("source_id"),
            "text_kind": payload.get("text_kind"),
            "file_path": payload.get("file_path"),
            "start_line": payload.get("start_line"),
            "end_line": payload.get("end_line"),
            "snippet": payload.get("snippet"),
        })
    return out
