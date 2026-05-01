"""Project indexer: walks workspace, extracts symbols+refs, persists to SQLite,
and resolves call edges by name matching.

Design note: refs are kept in a per-run in-memory dict keyed by `src_symbol_id`,
so resolution happens once after every changed file has been re-extracted.
There is no on-disk `unresolved_ref` table — that was a v1 leak of an
implementation detail into the schema and the source of two regression bugs."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import xxhash

from livespec_mcp.config import Settings
from livespec_mcp.domain.extractors import ExtractResult, extract
from livespec_mcp.domain.languages import detect_language
from livespec_mcp.storage.db import get_or_create_project, transaction

DEFAULT_IGNORES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", "target", ".next", ".nuxt", ".turbo", ".cache",
    ".mcp-docs",
}


@dataclass
class IndexStats:
    files_total: int = 0
    files_changed: int = 0
    files_skipped: int = 0
    symbols_total: int = 0
    edges_total: int = 0
    languages: dict[str, int] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.languages is None:
            self.languages = {}


def _hash_bytes(b: bytes) -> str:
    return xxhash.xxh3_128_hexdigest(b)


def _iter_files(root: Path, ignores: set[str]) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored dirs in-place
        dirnames[:] = [d for d in dirnames if d not in ignores and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            if detect_language(p) is None:
                continue
            try:
                if p.stat().st_size > 2_000_000:  # skip >2MB
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def index_project(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    project_name: str | None = None,
    force: bool = False,
) -> IndexStats:
    settings.ensure_dirs()
    name = project_name or settings.workspace.name
    project_id = get_or_create_project(conn, name=name, root=str(settings.workspace))

    run_id = conn.execute(
        "INSERT INTO index_run(project_id) VALUES(?)", (project_id,)
    ).lastrowid

    stats = IndexStats()
    files = _iter_files(settings.workspace, DEFAULT_IGNORES)

    # Build a snapshot of existing files for delta detection
    existing = {
        row["path"]: dict(row)
        for row in conn.execute(
            "SELECT id, path, content_hash, mtime FROM file WHERE project_id = ?",
            (project_id,),
        )
    }
    seen: set[str] = set()

    # In-memory ref pool collected during this run. Maps src_symbol_id -> list of
    # (target_name, ref_type). Resolved into edges after all files are processed.
    pending_refs: list[tuple[int, str, str]] = []

    with transaction(conn):
        for p in files:
            stats.files_total += 1
            rel = str(p.relative_to(settings.workspace))
            seen.add(rel)
            try:
                raw = p.read_bytes()
            except OSError:
                stats.files_skipped += 1
                continue
            content_hash = _hash_bytes(raw)
            mtime = p.stat().st_mtime
            prev = existing.get(rel)
            if not force and prev and prev["content_hash"] == content_hash:
                continue  # unchanged
            stats.files_changed += 1
            language = detect_language(p) or "unknown"
            stats.languages[language] = stats.languages.get(language, 0) + 1
            try:
                source = raw.decode("utf-8", errors="replace")
            except Exception:
                stats.files_skipped += 1
                continue
            _, result = extract(p, source, settings.workspace)
            line_count = source.count("\n") + 1
            file_id = _upsert_file(
                conn,
                project_id=project_id,
                path=rel,
                language=language,
                content_hash=content_hash,
                line_count=line_count,
                mtime=mtime,
            )
            new_refs = _replace_symbols(conn, file_id=file_id, result=result)
            pending_refs.extend(new_refs)

        # Remove deleted files
        for rel, row in existing.items():
            if rel not in seen:
                conn.execute("DELETE FROM file WHERE id = ?", (row["id"],))

    # Resolve only the refs we just collected; edges from untouched files survive
    # because we never touched their src symbols (no cascade).
    if pending_refs:
        _resolve_refs(conn, project_id=project_id, pending=pending_refs)

    stats.edges_total = int(
        conn.execute(
            """SELECT COUNT(*) c FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (project_id,),
        ).fetchone()["c"]
    )
    sym_total = conn.execute(
        "SELECT COUNT(*) c FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?",
        (project_id,),
    ).fetchone()["c"]
    stats.symbols_total = int(sym_total)

    conn.execute(
        """UPDATE index_run
           SET finished_at = datetime('now'),
               files_total = ?, files_changed = ?, symbols_total = ?, edges_total = ?
           WHERE id = ?""",
        (stats.files_total, stats.files_changed, stats.symbols_total, stats.edges_total, run_id),
    )
    return stats


def _upsert_file(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    path: str,
    language: str,
    content_hash: str,
    line_count: int,
    mtime: float,
) -> int:
    row = conn.execute(
        "SELECT id FROM file WHERE project_id=? AND path=?", (project_id, path)
    ).fetchone()
    if row:
        file_id = int(row["id"])
        conn.execute(
            """UPDATE file SET language=?, content_hash=?, line_count=?, mtime=?,
               indexed_at=datetime('now') WHERE id=?""",
            (language, content_hash, line_count, mtime, file_id),
        )
        # Wipe old symbols (cascade also wipes edges with src OR dst in those symbols)
        conn.execute("DELETE FROM symbol WHERE file_id=?", (file_id,))
        return file_id
    cur = conn.execute(
        """INSERT INTO file(project_id, path, language, content_hash, line_count, mtime)
           VALUES(?,?,?,?,?,?)""",
        (project_id, path, language, content_hash, line_count, mtime),
    )
    return int(cur.lastrowid)


def _replace_symbols(
    conn: sqlite3.Connection, *, file_id: int, result: ExtractResult
) -> list[tuple[int, str, str]]:
    """Insert symbols for a file and return the refs collected, with src ids
    resolved to symbol.id values (so the caller can append them to the in-memory
    pending_refs pool without another DB round-trip)."""
    qname_to_id: dict[str, int] = {}
    for s in result.symbols:
        body_hash = xxhash.xxh3_128_hexdigest(s.body_hash_seed.encode("utf-8", errors="replace"))
        sig_hash = (
            xxhash.xxh3_128_hexdigest(s.signature.encode("utf-8", errors="replace"))
            if s.signature else None
        )
        cur = conn.execute(
            """INSERT INTO symbol(file_id, parent_symbol_id, name, qualified_name, kind,
                signature, signature_hash, docstring, body_hash, start_line, end_line)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id, None, s.name, s.qualified_name, s.kind,
                s.signature, sig_hash, s.docstring, body_hash, s.start_line, s.end_line,
            ),
        )
        qname_to_id[s.qualified_name] = int(cur.lastrowid)
    for s in result.symbols:
        if s.parent_qname and s.parent_qname in qname_to_id:
            conn.execute(
                "UPDATE symbol SET parent_symbol_id=? WHERE id=?",
                (qname_to_id[s.parent_qname], qname_to_id[s.qualified_name]),
            )
    refs: list[tuple[int, str, str]] = []
    for r in result.refs:
        src_id = qname_to_id.get(r.src_qname)
        if src_id is None:
            continue
        refs.append((src_id, r.target_name, r.ref_type))
    return refs


def _resolve_refs(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    pending: list[tuple[int, str, str]],
) -> int:
    """Resolve in-memory refs by short-name matching against all symbols in the
    project. One match -> high-confidence edge (weight 1.0). Multiple matches ->
    connect to all (weight 0.5). Zero matches -> drop.

    The src symbols are necessarily in files that were just re-extracted, so
    their existing edges were already cascaded by the symbol DELETE in
    `_upsert_file`. We only INSERT here — no DELETE on the project-wide edge
    table — which preserves edges from unchanged files.
    """
    if not pending:
        return 0

    name_index: dict[str, list[int]] = {}
    for r in conn.execute(
        """SELECT s.id, s.name FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (project_id,),
    ):
        name_index.setdefault(r["name"], []).append(int(r["id"]))

    edge_count = 0
    seen_pairs: set[tuple[int, int]] = set()
    for src_id, target_name, _ref_type in pending:
        targets = name_index.get(target_name, [])
        if not targets:
            continue
        weight = 1.0 if len(targets) == 1 else 0.5
        for tid in targets:
            if tid == src_id:
                continue
            key = (src_id, tid)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            conn.execute(
                """INSERT OR IGNORE INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight)
                   VALUES(?,?,?,?)""",
                (src_id, tid, "calls", weight),
            )
            edge_count += 1
    return edge_count
