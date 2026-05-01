"""Project indexer: walks workspace, extracts symbols+refs, persists to SQLite,
and resolves call edges by name matching.

Design (post-P1.3 v2): refs are persisted in `symbol_ref` because partial
re-index needs to re-resolve refs from UNCHANGED files when their target is
in a file that did change — without persistence, those edges would vanish
permanently. Cascade on symbol delete keeps the ref table consistent.

Resolve is INSERT OR IGNORE only (never DELETE) so existing edges from
unchanged files are always preserved."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import xxhash

from livespec_mcp.config import Settings
from livespec_mcp.domain.extractors import ExtractResult, extract
from livespec_mcp.domain.languages import detect_language
from livespec_mcp.storage.db import consume_reextract_flag, get_or_create_project, transaction

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
    rf_links_created: int = 0
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

    # P0.2: a recent migration may have flagged that this DB needs a one-time
    # full re-extract (e.g. upgrading from v0.2 where symbol_ref didn't exist).
    if consume_reextract_flag(conn):
        force = True

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
            _replace_symbols(conn, file_id=file_id, result=result)

        # Remove deleted files
        for rel, row in existing.items():
            if rel not in seen:
                conn.execute("DELETE FROM file WHERE id = ?", (row["id"],))

    # Re-resolve all refs in the project. Cheap: indexed by target_name, and
    # we only INSERT OR IGNORE so existing edges survive unchanged. Refs whose
    # src symbol was deleted in a re-extract were cascaded out automatically.
    if stats.files_changed > 0 or force:
        _resolve_refs(conn, project_id=project_id)
        # P0.1: also re-link RF annotations from docstrings. Cheap, idempotent
        # (INSERT OR IGNORE), and prevents traceability from going silently
        # stale when an edited symbol's old rf_symbol row is cascaded away.
        from livespec_mcp.domain.matcher import scan_annotations
        stats.rf_links_created = scan_annotations(conn, project_id=project_id)

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


def _replace_symbols(conn: sqlite3.Connection, *, file_id: int, result: ExtractResult) -> None:
    """Insert symbols for a file and persist their refs to symbol_ref.

    Deduplicates extractor output by (qualified_name, start_line) before
    insert. Real-world Python code can produce duplicates: a function
    redefined under `if/else` or `try/except` (e.g. Django's compatibility
    shims `def cached_property(...)` defined twice in the same module under
    a Python-version guard). Both ASTNodes have identical qname and
    start_line, so the v0.6 schema's UNIQUE(file_id, qname, start_line)
    constraint would fire. We keep the first occurrence — that's the
    branch-active definition in source order.
    """
    import json as _json
    qname_to_id: dict[str, int] = {}
    seen_keys: set[tuple[str, int]] = set()
    for s in result.symbols:
        key = (s.qualified_name, s.start_line)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        body_hash = xxhash.xxh3_128_hexdigest(s.body_hash_seed.encode("utf-8", errors="replace"))
        sig_hash = (
            xxhash.xxh3_128_hexdigest(s.signature.encode("utf-8", errors="replace"))
            if s.signature else None
        )
        decorators_json = _json.dumps(s.decorators) if s.decorators else None
        cur = conn.execute(
            """INSERT INTO symbol(file_id, parent_symbol_id, name, qualified_name, kind,
                signature, signature_hash, docstring, body_hash, decorators,
                visibility, start_line, end_line)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id, None, s.name, s.qualified_name, s.kind,
                s.signature, sig_hash, s.docstring, body_hash, decorators_json,
                s.visibility, s.start_line, s.end_line,
            ),
        )
        qname_to_id[s.qualified_name] = int(cur.lastrowid)
    for s in result.symbols:
        if s.parent_qname and s.parent_qname in qname_to_id:
            conn.execute(
                "UPDATE symbol SET parent_symbol_id=? WHERE id=?",
                (qname_to_id[s.parent_qname], qname_to_id[s.qualified_name]),
            )
    for r in result.refs:
        src_id = qname_to_id.get(r.src_qname)
        if src_id is None:
            continue
        conn.execute(
            """INSERT INTO symbol_ref(src_symbol_id, target_name, ref_type, line, scope_module)
               VALUES(?,?,?,?,?)""",
            (src_id, r.target_name, r.ref_type, r.line, r.scope_module),
        )


def _resolve_refs(conn: sqlite3.Connection, *, project_id: int) -> int:
    """Resolve every symbol_ref in the project into symbol_edge rows.

    INSERT OR IGNORE only — never DELETE. The unique constraint on
    (src, dst, edge_type) makes this idempotent. Refs whose src symbol was
    deleted in a re-extract were cascaded out automatically; refs from
    unchanged files survive and re-resolve against the new symbol IDs of
    re-extracted files.
    """
    rows = conn.execute(
        """SELECT u.src_symbol_id, u.target_name, u.scope_module FROM symbol_ref u
           JOIN symbol s ON s.id = u.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?""",
        (project_id,),
    ).fetchall()

    # name_index: short name -> [(symbol_id, qualified_name)]
    name_index: dict[str, list[tuple[int, str]]] = {}
    for r in conn.execute(
        """SELECT s.id, s.name, s.qualified_name FROM symbol s
           JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (project_id,),
    ):
        name_index.setdefault(r["name"], []).append((int(r["id"]), r["qualified_name"]))

    edge_count = 0
    seen_pairs: set[tuple[int, int]] = set()
    for u in rows:
        candidates = name_index.get(u["target_name"], [])
        if not candidates:
            continue

        # P0.4: if the ref carries a scope_module (Python imports), prefer
        # candidates whose qualified_name lives under that module. If at least
        # one matches, drop the rest — that's a confident, scoped resolution.
        scope = u["scope_module"]
        scoped: list[tuple[int, str]] = []
        if scope:
            for sid, qname in candidates:
                # Match either the exact module prefix or its tail (because the
                # extractor may emit module names without the package prefix that
                # the indexer assigns to qualified_name).
                if scope and (f".{scope}." in f".{qname}" or qname.startswith(f"{scope}.")):
                    scoped.append((sid, qname))
            if scoped:
                candidates = scoped

        weight = 1.0 if len(candidates) == 1 else (0.9 if scoped else 0.5)
        for tid, _qname in candidates:
            src_id = int(u["src_symbol_id"])
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
