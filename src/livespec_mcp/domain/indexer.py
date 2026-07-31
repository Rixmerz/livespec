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
from typing import Any

import xxhash
from pathspec import GitIgnoreSpec

from livespec_mcp.config import RepoConfig, Settings, load_repo_config
from livespec_mcp.domain.extractors import (
    ExtractResult,
    extract,
    normalize_route_path,
)
from livespec_mcp.domain.languages import EXTRACTOR_SUPPORTED, detect_language
from livespec_mcp.storage.db import (
    clear_reextract_flag,
    get_or_create_project,
    peek_reextract_flag,
    transaction,
)

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
    files_deleted: bool = False
    symbols_total: int = 0
    edges_total: int = 0
    spec_links_created: int = 0
    manual_links_restored: int = 0
    languages: dict[str, int] = None  # type: ignore
    languages_unsupported: dict[str, int] = None  # type: ignore  # mapped ext, no extractor
    repo_config: dict[str, Any] | None = None  # echo of .livespec.toml, if present

    def __post_init__(self) -> None:
        if self.languages is None:
            self.languages = {}
        if self.languages_unsupported is None:
            self.languages_unsupported = {}


def _hash_bytes(b: bytes) -> str:
    return xxhash.xxh3_128_hexdigest(b)


def _load_gitignore(d: Path) -> GitIgnoreSpec | None:
    gi = d / ".gitignore"
    try:
        if not gi.is_file():
            return None
        lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    spec = GitIgnoreSpec.from_lines(lines)
    return spec if spec.patterns else None


def _gitignored(path: Path, specs: list[tuple[Path, GitIgnoreSpec]], *, is_dir: bool) -> bool:
    # Deepest .gitignore with an opinion wins, matching git's precedence
    # (a nested `!pattern` can re-include what a parent ignored). Dirs
    # pruned here never get walked, so re-includes *inside* an ignored
    # directory don't apply — the same limitation git documents.
    for base, spec in reversed(specs):
        rel = path.relative_to(base).as_posix()
        if is_dir:
            rel += "/"
        result = spec.check_file(rel)
        if result.include is not None:
            return result.include
    return False


def _iter_files(
    root: Path,
    ignores: set[str],
    cfg: RepoConfig | None = None,
    *,
    unsupported: dict[str, int] | None = None,
) -> list[Path]:
    cfg = cfg or RepoConfig()
    # .livespec.toml [index].ignore outranks every .gitignore: when the
    # config has an opinion (ignore or !re-include), that decision is final.
    cfg_spec = GitIgnoreSpec.from_lines(cfg.ignore) if cfg.ignore else None

    def _decide(path: Path, *, is_dir: bool, specs: list[tuple[Path, GitIgnoreSpec]]) -> bool:
        if cfg_spec is not None:
            rel = path.relative_to(root).as_posix()
            verdict = cfg_spec.check_file(rel + "/" if is_dir else rel).include
            if verdict is not None:
                return verdict
        return bool(specs) and _gitignored(path, specs, is_dir=is_dir)

    out: list[Path] = []
    # Each walked dir inherits the .gitignore specs of its ancestors;
    # os.walk is top-down so parents are always seen before children.
    inherited: dict[str, list[tuple[Path, GitIgnoreSpec]]] = {str(root): []}
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        specs = inherited.pop(dirpath, [])
        own = _load_gitignore(dp)
        if own is not None:
            specs = [*specs, (dp, own)]
        # Prune ignored dirs in-place
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ignores
            and not d.startswith(".")
            and not _decide(dp / d, is_dir=True, specs=specs)
        ]
        for d in dirnames:
            inherited[os.path.join(dirpath, d)] = specs
        for fn in filenames:
            if fn.startswith("."):
                continue
            p = dp / fn
            lang = detect_language(p)
            if lang is None:
                continue
            if cfg.languages is not None and lang not in cfg.languages:
                continue
            if _decide(p, is_dir=False, specs=specs):
                continue
            if lang not in EXTRACTOR_SUPPORTED:
                if unsupported is not None:
                    unsupported[lang] = unsupported.get(lang, 0) + 1
                continue
            try:
                if p.stat().st_size > cfg.max_file_bytes:
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
    # Peek (don't clear) so a crashed/rolled-back run leaves the flag set — it
    # is cleared inside the commit transaction only after the run succeeds.
    needs_reextract = peek_reextract_flag(conn)
    if needs_reextract:
        force = True

    run_id = conn.execute(
        "INSERT INTO index_run(project_id) VALUES(?)", (project_id,)
    ).lastrowid

    stats = IndexStats()
    repo_cfg = load_repo_config(settings.workspace)
    if (settings.workspace / ".livespec.toml").is_file():
        stats.repo_config = repo_cfg.as_payload()
    files = _iter_files(
        settings.workspace, DEFAULT_IGNORES, repo_cfg, unsupported=stats.languages_unsupported
    )

    # Build a snapshot of existing files for delta detection
    existing = {
        row["path"]: dict(row)
        for row in conn.execute(
            "SELECT id, path, content_hash, mtime FROM file WHERE project_id = ?",
            (project_id,),
        )
    }
    seen: set[str] = set()
    changed_file_ids: list[int] = []
    files_deleted = False

    # Snapshot manual / non-annotation spec_symbol links before any cascade
    # delete fires. Re-extracting a file wipes its symbols (and via FK
    # cascade, every spec_symbol row pointing at them), which silently
    # destroyed mappings created by `bulk_link_spec_symbols` /
    # `link_spec_symbol`. We re-resolve by symbol qname after the new
    # symbols are inserted and INSERT OR IGNORE the manual links back.
    # `source = 'annotation'` is intentionally NOT snapshotted: those
    # are re-derived by `scan_annotations` from the fresh docstrings,
    # so trying to preserve them would just shadow legitimate edits to
    # `@spec:` tags in source.
    manual_links_snapshot: list[tuple[str, str, str, float, str]] = [
        (
            r["spec_id"],
            r["qname"],
            r["relation"],
            float(r["confidence"]),
            r["source"],
        )
        for r in conn.execute(
            """SELECT sp.spec_id AS spec_id, s.qualified_name AS qname,
                      ss.relation AS relation, ss.confidence AS confidence,
                      ss.source AS source
               FROM spec_symbol ss
               JOIN spec sp ON sp.id = ss.spec_id
               JOIN symbol s ON s.id = ss.symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?
                 AND ss.source != 'annotation'""",
            (project_id,),
        )
    ]

    with transaction(conn):
        for p in files:
            stats.files_total += 1
            rel = str(p.relative_to(settings.workspace))
            seen.add(rel)
            try:
                raw = p.read_bytes()
                mtime = p.stat().st_mtime
            except OSError:
                # File vanished between the walk and this read (e.g. a git
                # checkout mid-index). Skip it — must not abort the whole run.
                stats.files_skipped += 1
                continue
            content_hash = _hash_bytes(raw)
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
            # C4: a transient parse failure (file saved mid-edit) must NOT wipe
            # the file's existing symbols — the cascade would take their
            # spec_symbol links with them and the restore can't re-resolve a
            # symbol that momentarily doesn't exist, permanently destroying
            # manual traceability. Leave the old symbols in place and DON'T
            # advance content_hash, so the next index retries once it parses.
            if (
                result.parse_error
                and not result.symbols
                and prev is not None
                and _file_has_symbols(conn, int(prev["id"]))
            ):
                stats.files_changed -= 1  # undo the increment above; nothing changed
                stats.files_skipped += 1
                continue
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
            changed_file_ids.append(file_id)

        # Remove deleted files
        for rel, row in existing.items():
            if rel not in seen:
                conn.execute("DELETE FROM file WHERE id = ?", (row["id"],))
                files_deleted = True
        stats.files_deleted = files_deleted

        # Re-resolve refs. v0.9: when partial changes are detected (no force,
        # no deletions, prior index_run exists), walk only the affected ref
        # subset — refs whose src is in a changed file OR whose target_name
        # matches a name re-inserted in a changed file. Falls back to the
        # full walk on `force=True`, file deletions (their target names need
        # global cleanup), or the very first index run on this project. The
        # whole resolve/annotation/restore phase now runs inside the SAME
        # transaction as the symbol writes so the run is atomic — a crash
        # mid-resolve rolls back cleanly instead of committing new content
        # hashes while leaving edges unresolved (which the next incremental
        # run would then skip forever).
        if stats.files_changed > 0 or force or files_deleted:
            prior_runs = conn.execute(
                "SELECT COUNT(*) c FROM index_run WHERE project_id=? AND finished_at IS NOT NULL",
                (project_id,),
            ).fetchone()["c"]
            use_targeted = (
                not force
                and not files_deleted
                and bool(changed_file_ids)
                and int(prior_runs) > 0
            )
            _resolve_refs(
                conn,
                project_id=project_id,
                changed_file_ids=changed_file_ids if use_targeted else None,
            )
            # v0.21 P2: join frontend call sites to backend handlers by route.
            # DB-wide (not project-scoped): a shared group DB makes this
            # cross-repo; a per-repo DB links a monorepo's own front+back.
            _resolve_routes(conn)
            # P0.1: also re-link Spec annotations from docstrings. Cheap, idempotent
            # (INSERT OR IGNORE), and prevents traceability from going silently
            # stale when an edited symbol's old spec_symbol row is cascaded away.
            from livespec_mcp.domain.matcher import scan_annotations
            stats.spec_links_created = scan_annotations(conn, project_id=project_id)

            # Restore manual spec_symbol links wiped by the symbol cascade. We
            # re-resolve symbol qname → new symbol_id and INSERT OR IGNORE,
            # so links whose target symbol now lives at a new id come back,
            # and links whose symbol qname disappeared from the codebase
            # silently drop (the symbol no longer exists — nothing to link).
            if manual_links_snapshot:
                # Batch (v0.14): one set-based INSERT..SELECT instead of two
                # queries per snapshot row. MIN(s.id) keeps the old LIMIT 1
                # semantics when a qname exists in more than one file.
                conn.execute(
                    """CREATE TEMP TABLE IF NOT EXISTS _manual_links(
                           spec_id_str TEXT, qname TEXT, relation TEXT,
                           confidence REAL, source TEXT)"""
                )
                conn.execute("DELETE FROM _manual_links")
                conn.executemany(
                    "INSERT INTO _manual_links VALUES(?,?,?,?,?)", manual_links_snapshot
                )
                cur = conn.execute(
                    """INSERT OR IGNORE INTO spec_symbol(spec_id, symbol_id, relation, confidence, source)
                       SELECT sp.id, MIN(s.id), m.relation, m.confidence, m.source
                       FROM _manual_links m
                       JOIN spec sp ON sp.spec_id = m.spec_id_str AND sp.project_id = ?
                       JOIN symbol s ON s.qualified_name = m.qname
                       JOIN file f ON f.id = s.file_id AND f.project_id = ?
                       GROUP BY sp.id, m.qname, m.relation, m.confidence, m.source""",
                    (project_id, project_id),
                )
                stats.manual_links_restored = max(cur.rowcount, 0)
                conn.execute("DELETE FROM _manual_links")

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

        # Finish the run + clear the re-extract flag inside the transaction so
        # finished_at (which the graph cache and recovery logic key on) and the
        # flag clear commit atomically with the data they describe.
        conn.execute(
            """UPDATE index_run
               SET finished_at = datetime('now'),
                   files_total = ?, files_changed = ?, symbols_total = ?, edges_total = ?
               WHERE id = ?""",
            (stats.files_total, stats.files_changed, stats.symbols_total, stats.edges_total, run_id),
        )
        if needs_reextract:
            clear_reextract_flag(conn)
    return stats


def _file_has_symbols(conn: sqlite3.Connection, file_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM symbol WHERE file_id=? LIMIT 1", (file_id,)
        ).fetchone()
        is not None
    )


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
    # v0.21 P2: persist route sites (client fetch/axios/requests + server
    # handlers). Cascade on symbol delete keeps these consistent on re-extract,
    # exactly like symbol_ref.
    for rt in result.routes:
        src_id = qname_to_id.get(rt.src_qname)
        if src_id is None:
            continue
        conn.execute(
            """INSERT INTO route_ref(symbol_id, role, method, path, norm_path, line)
               VALUES(?,?,?,?,?,?)""",
            (src_id, rt.role, rt.method, rt.path, normalize_route_path(rt.path), rt.line),
        )


def _route_edge_weight(client_method: str | None, server_method: str | None) -> float | None:
    """Confidence for a client↔server route match, or None if incompatible.

    Method mismatch (both known and different) → no edge. Both known & equal →
    0.9. One side unknown (fetch without a method, or a `@app.route` without an
    explicit verb) → 0.8 — still a strong path match, just method-agnostic.
    Server verbs like ROUTE / WEBSOCKET / ON count as unknown for matching."""
    c = (client_method or "").upper() or None
    s = (server_method or "").upper() or None
    if s in {"ROUTE", "WEBSOCKET", "ON", "ALL"}:
        s = None
    if c and s:
        return 0.9 if c == s else None
    return 0.8


def _resolve_routes(conn: sqlite3.Connection) -> int:
    """Join client route sites to server handlers by normalized path, DB-wide.

    Writes ``invokes_route`` edges (client symbol → server symbol) into
    symbol_edge. DB-wide by design: one DB may hold several projects only via a
    shared ``[workspace] group_db`` (state.py routes each workspace to its own
    DB otherwise), so matching across every project in the connection is
    exactly "match within the group" — cross-repo when grouped, intra-repo
    (monorepo front+back) otherwise.

    INSERT OR IGNORE / weight-MAX only — never DELETE, same invariant as
    ``_resolve_refs``. A route site whose symbol was wiped in a re-extract was
    cascaded out of route_ref already; surviving sites re-resolve against the
    new symbol ids.
    """
    servers: dict[str, list[tuple[int, str | None]]] = {}
    for r in conn.execute(
        "SELECT symbol_id, method, norm_path FROM route_ref WHERE role='server'"
    ):
        if r["norm_path"]:
            servers.setdefault(r["norm_path"], []).append(
                (int(r["symbol_id"]), r["method"])
            )
    if not servers:
        return 0
    edge_count = 0
    for c in conn.execute(
        "SELECT symbol_id, method, norm_path FROM route_ref WHERE role='client'"
    ):
        for server_id, server_method in servers.get(c["norm_path"], []):
            src_id = int(c["symbol_id"])
            if server_id == src_id:
                continue
            weight = _route_edge_weight(c["method"], server_method)
            if weight is None:
                continue
            conn.execute(
                """INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight)
                   VALUES(?,?, 'invokes_route', ?)
                   ON CONFLICT(src_symbol_id, dst_symbol_id, edge_type)
                   DO UPDATE SET weight = MAX(symbol_edge.weight, excluded.weight)""",
                (src_id, server_id, weight),
            )
            edge_count += 1
    return edge_count


def _resolve_refs(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    changed_file_ids: list[int] | None = None,
) -> int:
    """Resolve every symbol_ref in the project into symbol_edge rows.

    INSERT OR IGNORE only — never DELETE. The unique constraint on
    (src, dst, edge_type) makes this idempotent. Refs whose src symbol was
    deleted in a re-extract were cascaded out automatically; refs from
    unchanged files survive and re-resolve against the new symbol IDs of
    re-extracted files.

    Targeted walk (v0.9): when ``changed_file_ids`` is provided, only refs
    that need re-resolution are walked:
      - refs whose src is in a changed file (their old edges died via
        cascade when the file's symbols were wiped + re-inserted), OR
      - refs whose target_name matches a name defined in a changed file
        (edges to those names died via dst-cascade when the changed
        file's symbols were re-inserted with new IDs).
    Refs from unchanged files to unchanged files keep their existing
    edges untouched (INSERT OR IGNORE on the same (src, dst) is a no-op).
    Pass ``changed_file_ids=None`` for a full re-walk (force re-index).

    Disambiguation precedence when target_name has multiple candidates:
    1. scope_module match (Python imports captured by extractor) → weight 0.9.
    2. same source file as the call site → weight 0.7. Closes the v0.8 P2
       session-01 bug where short names like ``list_tools`` (defined in
       3 different modules) created edges to all 3 from a single in-module
       call site.
    3. otherwise: keep all candidates at weight 0.5 (legacy behavior). True
       cross-file ambiguous call where the extractor missed the import.
    Single-candidate matches are always weight 1.0.
    """
    if changed_file_ids:
        placeholders = ",".join("?" * len(changed_file_ids))
        names_in_changed = {
            r["name"]
            for r in conn.execute(
                f"SELECT DISTINCT name FROM symbol WHERE file_id IN ({placeholders})",
                changed_file_ids,
            )
        }
        params: list[Any] = [project_id, *changed_file_ids]
        sql = (
            f"SELECT u.src_symbol_id, u.target_name, u.scope_module, s.file_id AS src_file_id "
            f"FROM symbol_ref u "
            f"JOIN symbol s ON s.id = u.src_symbol_id "
            f"JOIN file f ON f.id = s.file_id "
            f"WHERE f.project_id = ? AND ("
            f"  s.file_id IN ({placeholders})"
        )
        if names_in_changed:
            name_placeholders = ",".join("?" * len(names_in_changed))
            sql += f" OR u.target_name IN ({name_placeholders})"
            params.extend(names_in_changed)
        sql += ")"
        rows = conn.execute(sql, params).fetchall()
    else:
        rows = conn.execute(
            """SELECT u.src_symbol_id, u.target_name, u.scope_module, s.file_id AS src_file_id
               FROM symbol_ref u
               JOIN symbol s ON s.id = u.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (project_id,),
        ).fetchall()

    # name_index: short name -> [(symbol_id, qualified_name, file_id)]
    name_index: dict[str, list[tuple[int, str, int]]] = {}
    for r in conn.execute(
        """SELECT s.id, s.name, s.qualified_name, s.file_id FROM symbol s
           JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (project_id,),
    ):
        name_index.setdefault(r["name"], []).append(
            (int(r["id"]), r["qualified_name"], int(r["file_id"]))
        )

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
        scoped: list[tuple[int, str, int]] = []
        if scope:
            for sid, qname, fid in candidates:
                # Match either the exact module prefix or its tail (because the
                # extractor may emit module names without the package prefix that
                # the indexer assigns to qualified_name).
                if f".{scope}." in f".{qname}" or qname.startswith(f"{scope}."):
                    scoped.append((sid, qname, fid))
            if scoped:
                candidates = scoped

        # v0.8 P2 fix: when scope didn't disambiguate AND there are still
        # multiple candidates, prefer same-file candidates. An in-module
        # call to a short name almost always resolves locally; without this
        # the resolver fans out to every same-named symbol across the repo
        # (battle-test session 01: list_tools x3, _cosine x2).
        same_file: list[tuple[int, str, int]] = []
        if not scoped and len(candidates) > 1:
            src_file_id = int(u["src_file_id"])
            same_file = [c for c in candidates if c[2] == src_file_id]
            if same_file:
                candidates = same_file

        if len(candidates) == 1:
            weight = 1.0
        elif scoped:
            weight = 0.9
        elif same_file:
            weight = 0.7
        else:
            weight = 0.5
        for tid, _qname, _fid in candidates:
            src_id = int(u["src_symbol_id"])
            if tid == src_id:
                continue
            key = (src_id, tid)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            # Never DELETE (edges from unchanged files must survive); but DO
            # refresh the weight monotonically so a previously-ambiguous edge
            # (0.5 fan-out) upgrades once disambiguation makes it unambiguous.
            # MAX avoids a stray targeted re-resolve DOWNGRADING a confirmed
            # 1.0 edge back to 0.5.
            conn.execute(
                """INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight)
                   VALUES(?,?,?,?)
                   ON CONFLICT(src_symbol_id, dst_symbol_id, edge_type)
                   DO UPDATE SET weight = MAX(symbol_edge.weight, excluded.weight)""",
                (src_id, tid, "calls", weight),
            )
            edge_count += 1
    return edge_count
