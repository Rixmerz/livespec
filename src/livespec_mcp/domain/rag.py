"""AST-aware chunking + FTS5 keyword search over symbols and Specs.

Chunks preserve symbol/Spec boundaries. ``search`` (via ``keyword_search`` /
``hybrid_search`` alias) is FTS5-only — dense vectors / sqlite-vec / fastembed
were removed.
"""
from __future__ import annotations

import re
import sqlite3
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

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
                    # `line` currently points at the START of the overflow piece
                    # (the +2 for the "\n\n" separator is added at loop end); the
                    # buffered piece's last real line is two before it.
                    end_line=max(chunk_start, line - 2),
                )
            )
            buf = [piece]
            buf_tokens = t
            chunk_start = line
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


def chunk_spec(spec_row: sqlite3.Row) -> list[Chunk]:
    desc = spec_row["description"] or ""
    text = f"# {spec_row['spec_id']}: {spec_row['title']}\n\n{desc}".strip()
    if _approx_tokens(text) <= TEXT_CHUNK_MAX_TOKENS:
        return [
            Chunk(
                source_type="spec",
                source_id=int(spec_row["id"]),
                text_kind="text",
                text=text,
            )
        ]
    # Naive split for very long specs
    parts = text.split("\n\n")
    out: list[Chunk] = []
    buf: list[str] = []
    buf_tokens = 0
    for p in parts:
        t = _approx_tokens(p)
        if buf_tokens + t > TEXT_CHUNK_MAX_TOKENS and buf:
            out.append(
                Chunk(
                    source_type="spec",
                    source_id=int(spec_row["id"]),
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
                source_type="spec",
                source_id=int(spec_row["id"]),
                text_kind="text",
                text="\n\n".join(buf),
            )
        )
    return out


# ---------- Persistence ----------

def upsert_chunks(conn: sqlite3.Connection, project_id: int, chunks: Iterable[Chunk]) -> list[int]:
    """Replace a source's chunks only when its full chunk set changed.

    Group by (source_type, source_id) FIRST. The old per-chunk loop deleted
    "prior chunks for this source" on every non-matching chunk — so for a
    symbol that splits into N chunks, chunk 2's delete wiped chunk 1 that the
    same loop had just inserted, leaving only the LAST chunk (and aliased
    rowids in the returned list). Comparing the whole hash sequence per source
    also lets an unchanged symbol keep its existing rows instead of re-inserting.
    """
    grouped: OrderedDict[tuple[str, int | None], list[Chunk]] = OrderedDict()
    for ch in chunks:
        grouped.setdefault((ch.source_type, ch.source_id), []).append(ch)

    ids: list[int] = []
    for (source_type, source_id), group in grouped.items():
        existing = conn.execute(
            """SELECT id, content_hash FROM chunk
               WHERE project_id=? AND source_type=? AND source_id IS ?
               ORDER BY id""",
            (project_id, source_type, source_id),
        ).fetchall()
        incoming_hashes = [c.content_hash for c in group]
        if [r["content_hash"] for r in existing] == incoming_hashes:
            # Identical chunk set in the same order — reuse rows (no FTS churn).
            ids.extend(int(r["id"]) for r in existing)
            continue
        # Changed set: replace all of this source's chunks in one shot.
        conn.execute(
            "DELETE FROM chunk WHERE project_id=? AND source_type=? AND source_id IS ?",
            (project_id, source_type, source_id),
        )
        for ch in group:
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
                    ch.content_hash,
                ),
            )
            ids.append(int(cur.lastrowid))
    return ids



def rebuild_chunks(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """Re-chunk every symbol and Spec for the project. Idempotent.

    Idempotent upsert: ``upsert_chunks`` reuses a source's rows when the
    chunk set is unchanged; then deletes chunks whose source no longer exists.
    Reads each file ONCE (all its symbols share the read).
    """
    workspace = Path(
        conn.execute("SELECT root FROM project WHERE id=?", (project_id,)).fetchone()["root"]
    )

    sym_count = 0
    spec_count = 0
    kept_ids: list[int] = []

    rows = conn.execute(
        """SELECT s.id, s.qualified_name, s.kind, s.signature, s.docstring,
                  s.start_line, s.end_line, f.path AS file_path
           FROM symbol s JOIN file f ON f.id=s.file_id
           WHERE f.project_id=?
           ORDER BY f.path""",
        (project_id,),
    ).fetchall()
    by_file: OrderedDict[str, list] = OrderedDict()
    for r in rows:
        by_file.setdefault(r["file_path"], []).append(r)
    for file_path, file_rows in by_file.items():
        try:
            lines = (workspace / file_path).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            lines = []
        for r in file_rows:
            body = "\n".join(
                lines[max(r["start_line"] - 1, 0) : min(r["end_line"], len(lines))]
            )
            chunks = chunk_symbol(r, body)
            kept_ids.extend(upsert_chunks(conn, project_id, chunks))
            sym_count += len(chunks)

    specs = conn.execute(
        "SELECT id, spec_id, title, description FROM spec WHERE project_id=?", (project_id,)
    ).fetchall()
    for r in specs:
        chunks = chunk_spec(r)
        kept_ids.extend(upsert_chunks(conn, project_id, chunks))
        spec_count += len(chunks)

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _kept_chunks(id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _kept_chunks")
    conn.executemany("INSERT OR IGNORE INTO _kept_chunks(id) VALUES(?)", [(i,) for i in kept_ids])
    conn.execute(
        "DELETE FROM chunk WHERE project_id=? AND id NOT IN (SELECT id FROM _kept_chunks)",
        (project_id,),
    )
    conn.execute("DELETE FROM _kept_chunks")

    return {"symbol_chunks": sym_count, "spec_chunks": spec_count}


# ---------- FTS5 search ----------

_FTS_TOKEN_SPLIT = re.compile(r"[_\-.]+")
_PHRASE_RE = re.compile(r'"([^"]+)"')


def _fts_query_tokens(query: str) -> list[str]:
    """Turn a user query into FTS5 OR tokens (snake_case → separate terms)."""
    out: list[str] = []
    for raw in query.split():
        t = raw.replace('"', "").replace("*", "").strip()
        if not t:
            continue
        parts = _FTS_TOKEN_SPLIT.split(t) if _FTS_TOKEN_SPLIT.search(t) else [t]
        for p in parts:
            if p and p.isalnum():
                out.append(p)
    return out


def _fts_match_expr(query: str) -> tuple[str, str]:
    """Build an FTS5 MATCH expression.

    Quoted phrases become phrase queries; remaining tokens are OR'd.
    Returns ``(expr, mode)`` where mode is ``phrase``, ``tokens``, or ``mixed``.
    """
    phrases = [m.group(1).strip() for m in _PHRASE_RE.finditer(query) if m.group(1).strip()]
    remainder = _PHRASE_RE.sub(" ", query)
    tokens = _fts_query_tokens(remainder)
    parts: list[str] = []
    for ph in phrases:
        clean = " ".join(
            _fts_query_tokens(ph)
            or [t for t in ph.replace('"', "").split() if t.isalnum()]
        )
        if clean:
            parts.append(f'"{clean}"')
    if tokens:
        parts.append(" OR ".join(tokens))
    if not parts:
        return "", "tokens"
    if phrases and tokens:
        mode = "mixed"
        expr = " AND ".join(parts) if len(parts) > 1 else parts[0]
    elif phrases:
        mode = "phrase"
        expr = " AND ".join(parts)
    else:
        mode = "tokens"
        expr = parts[0]
    return expr, mode


def fts_search(
    conn: sqlite3.Connection, project_id: int, query: str, limit: int, scope: str
) -> list[tuple[int, float, dict]]:
    """FTS5 over chunks. Returns (chunk_id, bm25_score, payload)."""
    if not query.strip():
        return []
    fts_query, _mode = _fts_match_expr(query)
    if not fts_query:
        return []
    sql = [
        """SELECT c.id, c.source_type, c.source_id, c.file_path, c.start_line, c.end_line,
                  c.text_kind, substr(c.text, 1, 240) AS snippet, bm25(chunk_fts) AS bm
           FROM chunk_fts JOIN chunk c ON c.id = chunk_fts.rowid
           WHERE chunk_fts MATCH ? AND c.project_id = ?"""
    ]
    args: list = [fts_query, project_id]
    if scope == "code":
        sql.append("AND c.text_kind='code'")
    elif scope == "specs":
        sql.append("AND c.source_type='spec'")
    sql.append("ORDER BY bm LIMIT ?")
    args.append(limit * 3)
    out: list[tuple[int, float, dict]] = []
    try:
        rows = conn.execute(" ".join(sql), args).fetchall()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        return []
    for r in rows:
        bm = float(r["bm"])
        score = -bm
        out.append((int(r["id"]), score, dict(r)))
    return out


def chunks_index_fresh(
    conn: sqlite3.Connection, project_id: int, workspace: Path
) -> dict:
    """Whether chunk source files still match indexed ``file.content_hash``."""
    from livespec_mcp.domain.indexer import _hash_bytes

    rows = conn.execute(
        """SELECT DISTINCT c.file_path, f.content_hash
           FROM chunk c
           LEFT JOIN file f ON f.project_id=c.project_id AND f.path=c.file_path
           WHERE c.project_id=? AND c.file_path IS NOT NULL AND c.file_path != ''""",
        (project_id,),
    ).fetchall()
    stale: list[str] = []
    for r in rows:
        path = r["file_path"]
        expected = r["content_hash"]
        if not expected:
            continue
        fp = workspace / path
        if not fp.is_file():
            stale.append(path)
            continue
        try:
            raw = fp.read_bytes()
        except OSError:
            stale.append(path)
            continue
        if _hash_bytes(raw) != expected:
            stale.append(path)
    return {
        "index_fresh": not stale,
        "stale_files_count": len(stale),
        "stale_files": sorted(stale)[:20],
    }


def keyword_search(
    conn: sqlite3.Connection, project_id: int, query: str, scope: str, limit: int
) -> tuple[list[dict], str]:
    """FTS5 keyword search over AST-aware chunks. Returns (results, query_mode)."""
    _, mode = _fts_match_expr(query)
    fts = fts_search(conn, project_id, query, limit, scope)
    out = []
    for cid, score, payload in fts[:limit]:
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
    return out, mode


def hybrid_search(
    conn: sqlite3.Connection, project_id: int, query: str, scope: str, limit: int
) -> list[dict]:
    """Alias for ``keyword_search`` (stable call-site name; vectors removed)."""
    results, _mode = keyword_search(conn, project_id, query, scope, limit)
    return results
