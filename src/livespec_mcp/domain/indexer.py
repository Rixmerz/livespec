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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xxhash
from pathspec import GitIgnoreSpec

from livespec_mcp.config import RepoConfig, Settings, load_repo_config
from livespec_mcp.domain.extractors import (
    RECEIVER_MODULE,
    RECEIVER_OBJECT,
    RECEIVER_SELF,
    RECEIVER_SUPER,
    RECEIVER_TYPE,
    REF_INHERIT,
    REF_TYPE,
    ExtractResult,
    extract,
    normalize_route_path,
)
from livespec_mcp.domain.hierarchy import Hierarchy
from livespec_mcp.domain.languages import (
    EXTRACTOR_SUPPORTED,
    SCHEMA_ONLY_LANGUAGES,
    detect_language,
    reset_grammar_failures,
)
from livespec_mcp.domain.py_modules import PyModules
from livespec_mcp.domain.ts_modules import TsModules
from livespec_mcp.storage.db import (
    clear_reextract_flag,
    get_or_create_project,
    peek_reextract_flag,
    transaction,
)

DEFAULT_IGNORES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "target",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    ".mcp-docs",
}

# Languages a file is read for: full extractors, plus `.sql`, which yields only
# schema tables and needs no tree-sitter grammar.
_INDEXED_LANGUAGES = EXTRACTOR_SUPPORTED | SCHEMA_ONLY_LANGUAGES


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
    # language -> file count that could not be indexed because the tree-sitter
    # GRAMMAR would not load (language-pack 1.x downloads grammars on first
    # use). Distinct from `languages_unsupported`, which is a language livespec
    # deliberately has no extractor for. This one is recoverable: run
    # `livespec grammars` once with network and re-index.
    languages_failed: dict[str, int] = None  # type: ignore
    # language -> the parser-load error (first one seen this run), so the
    # payload can say WHY, not just how many files were skipped.
    parser_errors: dict[str, str] = None  # type: ignore
    repo_config: dict[str, Any] | None = None  # echo of .livespec.toml, if present

    def __post_init__(self) -> None:
        if self.languages is None:
            self.languages = {}
        if self.languages_unsupported is None:
            self.languages_unsupported = {}
        if self.languages_failed is None:
            self.languages_failed = {}
        if self.parser_errors is None:
            self.parser_errors = {}


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
            if lang not in _INDEXED_LANGUAGES:
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
    """@spec:indexing-indexing-workspace-walk"""
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

    run_id = conn.execute("INSERT INTO index_run(project_id) VALUES(?)", (project_id,)).lastrowid

    stats = IndexStats()
    # A grammar that failed in an earlier run (or before `livespec grammars`
    # ran in another terminal) gets one fresh attempt per run; within the run
    # the failure is memoised so 500 files cost one network attempt.
    reset_grammar_failures()
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
            if result.grammar_missing:
                # The grammar never loaded, so this file was not read at all.
                # Persisting it would store zero symbols AND advance the
                # content hash, and the next run would skip it as unchanged —
                # one offline index turning a polyglot repo permanently
                # Python-only. Leave the row (and any existing symbols) alone
                # so a later run with the grammar present re-extracts it.
                stats.files_changed -= 1  # undo the increment above
                stats.files_skipped += 1
                stats.languages_failed[result.grammar_missing] = (
                    stats.languages_failed.get(result.grammar_missing, 0) + 1
                )
                stats.parser_errors.setdefault(
                    result.grammar_missing, result.grammar_error or "unknown error"
                )
                continue
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
                not force and not files_deleted and bool(changed_file_ids) and int(prior_runs) > 0
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

            stats.spec_links_created = scan_annotations(conn, project_id=project_id).created
            # `@schema: <entity>` bindings, then the links between every pair of
            # symbols bound to one entity. DB-wide like _resolve_routes, so a
            # group DB links the schemas of every repo in the group.
            from livespec_mcp.domain.matcher import scan_schema_annotations

            scan_schema_annotations(conn, project_id=project_id)
            _resolve_schema_links(conn)
            # Data-access map: ORM accesses name a model; the model's table
            # comes from declarations that may sit in another file, so every
            # ORM row is re-resolved on each run that changed anything.
            from livespec_mcp.domain.data_access_store import resolve_data_access

            resolve_data_access(conn)

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
            (
                stats.files_total,
                stats.files_changed,
                stats.symbols_total,
                stats.edges_total,
                run_id,
            ),
        )
        if needs_reextract:
            clear_reextract_flag(conn)
    return stats


def _file_has_symbols(conn: sqlite3.Connection, file_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM symbol WHERE file_id=? LIMIT 1", (file_id,)).fetchone()
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


def pick_by_span(rows: list[tuple[int, int, int]] | None, line: int) -> int | None:
    """The symbol (id, start, end) of one qualified name that a reference at
    *line* belongs to: the narrowest span containing it, else the nearest
    definition above it, else the first. With a single row this is that row."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0][0]
    inside = [r for r in rows if r[1] <= line <= r[2]]
    if inside:
        return min(inside, key=lambda r: (r[2] - r[1], r[1]))[0]
    below = [r for r in rows if r[1] > line]
    if below:
        return min(below, key=lambda r: r[1])[0]
    return max(rows, key=lambda r: r[1])[0]


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

    seen_keys: set[tuple[str, int]] = set()
    rows: list[tuple[Any, ...]] = []
    for s in result.symbols:
        key = (s.qualified_name, s.start_line)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        body_hash = xxhash.xxh3_128_hexdigest(s.body_hash_seed.encode("utf-8", errors="replace"))
        sig_hash = (
            xxhash.xxh3_128_hexdigest(s.signature.encode("utf-8", errors="replace"))
            if s.signature
            else None
        )
        decorators_json = _json.dumps(s.decorators) if s.decorators else None
        rows.append(
            (
                file_id,
                None,
                s.name,
                s.qualified_name,
                s.kind,
                s.signature,
                sig_hash,
                s.docstring,
                body_hash,
                decorators_json,
                s.visibility,
                s.start_line,
                s.end_line,
            )
        )
    # One statement for the file's symbols, then their ids read back in
    # insertion order: the same rows and rowids as one INSERT per symbol, and
    # the same qname -> id map (a qname inserted twice maps to its last row).
    conn.executemany(
        """INSERT INTO symbol(file_id, parent_symbol_id, name, qualified_name, kind,
            signature, signature_hash, docstring, body_hash, decorators,
            visibility, start_line, end_line)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    qname_to_id: dict[str, int] = {}
    # Every row a qualified name owns, with its span. Overloads, anonymous
    # classes, enum-constant bodies, same-named local classes and a function
    # defined in both branches of an `if` share ONE qualified name; a ref
    # carries its line, so it binds to the symbol whose span contains it.
    rows_by_qname: dict[str, list[tuple[int, int, int]]] = {}
    for r in conn.execute(
        "SELECT qualified_name, id, start_line, end_line FROM symbol WHERE file_id=? ORDER BY id",
        (file_id,),
    ):
        qname_to_id[r[0]] = int(r[1])
        rows_by_qname.setdefault(r[0], []).append((int(r[1]), int(r[2]), int(r[3])))

    def owner(qname: str, line: int) -> int | None:
        return pick_by_span(rows_by_qname.get(qname), line)

    conn.executemany(
        "UPDATE symbol SET parent_symbol_id=? WHERE id=?",
        [
            (
                owner(s.parent_qname, s.start_line),
                owner(s.qualified_name, s.start_line),
            )
            for s in result.symbols
            if s.parent_qname and s.parent_qname in qname_to_id
        ],
    )
    conn.executemany(
        """INSERT INTO symbol_ref(src_symbol_id, target_name, ref_type, line, scope_module,
                                 receiver, receiver_class)
           VALUES(?,?,?,?,?,?,?)""",
        [
            (
                owner(r.src_qname, r.line),
                r.target_name,
                r.ref_type,
                r.line,
                r.scope_module,
                r.receiver,
                r.receiver_class,
            )
            for r in result.refs
            if r.src_qname in qname_to_id
        ],
    )
    # v0.21 P2: persist route sites (client fetch/axios/requests + server
    # handlers). Cascade on symbol delete keeps these consistent on re-extract,
    # exactly like symbol_ref.
    conn.executemany(
        """INSERT INTO route_ref(symbol_id, role, method, path, norm_path, line)
           VALUES(?,?,?,?,?,?)""",
        [
            (
                owner(rt.src_qname, rt.line),
                rt.role,
                rt.method,
                rt.path,
                normalize_route_path(rt.path),
                rt.line,
            )
            for rt in result.routes
            if rt.src_qname in qname_to_id
        ],
    )
    _insert_schemas(conn, result=result, qname_to_id=qname_to_id)
    from livespec_mcp.domain.data_access_store import persist_data_access

    persist_data_access(conn, file_id=file_id, result=result, qname_to_id=qname_to_id)


def _insert_schemas(
    conn: sqlite3.Connection, *, result: ExtractResult, qname_to_id: dict[str, int]
) -> None:
    """@spec:schemas-schema-field-extraction

    Persist the file's schema declarations and their fields in source order.
    Rows hang off the symbol, so the cascade on re-extract replaces them along
    with the symbols, exactly like symbol_ref and route_ref.
    """
    for sc in result.schemas:
        symbol_id = qname_to_id.get(sc.src_qname)
        if symbol_id is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO schema_decl(symbol_id, flavor) VALUES(?,?)",
            (symbol_id, sc.flavor),
        )
        conn.executemany(
            """INSERT OR IGNORE INTO schema_field(symbol_id, ordinal, name, field_key, type,
                   item_type, ref_target, raw_type, optional, nullable, line)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    symbol_id,
                    ordinal,
                    f.name,
                    f.field_key,
                    f.type,
                    f.item_type,
                    f.ref_target,
                    f.raw_type,
                    int(f.optional),
                    int(f.nullable),
                    f.line,
                )
                for ordinal, f in enumerate(sc.fields)
            ],
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
    for r in conn.execute("SELECT symbol_id, method, norm_path FROM route_ref WHERE role='server'"):
        if r["norm_path"]:
            servers.setdefault(r["norm_path"], []).append((int(r["symbol_id"]), r["method"]))
    if not servers:
        return 0
    edge_count = 0
    for c in conn.execute("SELECT symbol_id, method, norm_path FROM route_ref WHERE role='client'"):
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
                   DO UPDATE SET weight = MAX(symbol_edge.weight, excluded.weight),
                                 origin = 'livespec'""",
                (src_id, server_id, weight),
            )
            edge_count += 1
    return edge_count


def _resolve_schema_links(conn: sqlite3.Connection) -> int:
    """@spec:schemas-cross-repo-schema-linking

    Link every pair of distinct symbols bound to the same entity, DB-wide.

    Same scope argument as ``_resolve_routes``: one DB holds several projects
    only through a shared ``[workspace] group_db``, so matching across the
    whole connection is cross-repo when grouped and intra-repo otherwise.
    INSERT OR IGNORE only, never DELETE: a link whose symbol was re-extracted
    without its binding was cascaded away with the symbol, and survivors pair
    up again with the new ids. Pairs are stored once, lower id first.
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO schema_link(entity_key, a_symbol_id, b_symbol_id)
           SELECT a.entity_key, a.symbol_id, b.symbol_id
           FROM schema_binding a
           JOIN schema_binding b
             ON b.entity_key = a.entity_key AND a.symbol_id < b.symbol_id"""
    )
    return max(cur.rowcount, 0)


def _is_caller_parameter(signature: str | None, target_name: str) -> bool:
    """True when *target_name* is a parameter of the calling symbol.

    Parsed off the stored signature rather than re-reading source: the
    signature is already on the row being walked, and this runs once per ref.
    Handles the annotated form (`text: Callable`), the default form (`text=...`)
    and the bare form, plus `*args` / `**kw` / `/` / `*` separators. Splits on
    TOP-LEVEL commas only, so `x: dict[str, int]` stays one parameter.
    """
    if not signature or "(" not in signature:
        return False
    inner = signature[signature.index("(") + 1 : signature.rfind(")")]
    if not inner.strip():
        return False

    params: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            params.append("".join(current))
            current = []
        else:
            current.append(ch)
    params.append("".join(current))

    for raw in params:
        name = raw.strip().lstrip("*").split(":")[0].split("=")[0].strip()
        if name and name == target_name:
            return True
    return False


# Languages whose symbols call each other: one family per runtime.
_LANGUAGE_FAMILIES: dict[str, str] = {
    "javascript": "js",
    "typescript": "js",
    "tsx": "js",
}


def _language_family(language: str | None) -> str:
    return _LANGUAGE_FAMILIES.get(language or "", language or "")


def _source_reader(
    conn: sqlite3.Connection, project_id: int, paths: dict[int, str]
) -> Callable[[int], str | None]:
    """Reads a Python file of the project from disk, for the re-export lookup
    (the indexed tables do not keep a module's import bindings)."""
    row = conn.execute("SELECT root FROM project WHERE id=?", (project_id,)).fetchone()
    root = Path(row["root"]) if row else None

    def read(file_id: int) -> str | None:
        if root is None or file_id not in paths:
            return None
        try:
            return (root / paths[file_id]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    return read


def _under_scope(candidates: list[tuple[int, str, int]], scope: str) -> list[tuple[int, str, int]]:
    # Match either the exact module prefix or its tail (because the extractor
    # may emit module names without the package prefix that the indexer
    # assigns to qualified_name).
    return [c for c in candidates if f".{scope}." in f".{c[1]}" or c[1].startswith(f"{scope}.")]


def _declares_base(signature: str | None) -> bool:
    """True unless a Python class signature (`class A(B, C)`) lists no base
    other than ``object`` (a metaclass keyword is no base either)."""
    if not signature or "(" not in signature:
        return False
    inner = signature[signature.index("(") + 1 : signature.rfind(")")]
    bases = [b.strip() for b in inner.split(",") if b.strip()]
    return any(b != "object" and "=" not in b for b in bases)


def _enclosing_class(src_qname: str, class_bases: dict[str, bool]) -> str | None:
    """The innermost Python class that encloses the caller, if any."""
    parts = src_qname.split(".")
    for depth in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:depth])
        if prefix in class_bases:
            return prefix
    return None


#: `symbol_ref.ref_type` -> `symbol_edge.edge_type`. Every other ref type
#: (`call`, `callback_arg`, `jsx`) is an invocation and resolves to `calls`.
_REF_EDGE_TYPE: dict[str, str] = {REF_INHERIT: "inherits", REF_TYPE: "references"}

#: Symbol kinds a base class or a type-position name can resolve to. A function
#: that happens to share the name is never the target of an annotation.
_TYPE_LIKE_KINDS: frozenset[str] = frozenset(
    {"class", "interface", "type_alias", "enum", "struct", "trait"}
)


def _java_scope_matches(scope: str, name: str, qname: str) -> bool:
    """Does the Java import path *scope* (``a.Outer.Inner``, or ``a.Outer``
    when the ref names ``Inner`` through it) denote the symbol *qname*?

    A Java class's qualified name here is ``<package dirs>.<File>.<classes...>``
    with the file's own top class repeated (``a.Outer.Outer.Inner``), which is
    why neither the plain import path nor a prefix of it matches a NESTED type."""
    q = qname.split(".")
    for want in (scope.split("."), [*scope.split("."), name]):
        for k in range(len(q) - 1):
            if q[k] == q[k + 1] and [*q[: k + 1], *q[k + 2 :]] == want:
                return True
    return False


def _collapse_same_qname(
    targets: list[tuple[int, str, int]],
    line: int,
    span_of: dict[int, tuple[int, int, str]],
    src_id: int = -1,
) -> list[tuple[int, str, int]]:
    """One target per (file, qualified name).

    Symbols that share a qualified name are not different things a reference
    could mean: overloads of one method, a class or function defined in both
    branches of an `if`, same-named local or anonymous classes. A reference
    gets ONE of them.

    - overloads and other duplicate methods: the FIRST in the file, whichever
      overload the call site really picks (the index cannot tell by argument
      types) — consistent, so a re-index never flips the edge. A call from one
      overload to its own name skips itself: `f(a)` delegating to `f(a, b)`
      reaches the first OTHER overload, not nothing;
    - a class / function defined more than once: the definition in scope at
      the reference, i.e. the nearest one above its line (the first one when
      the reference precedes them all).
    Distinct qualified names are untouched."""
    if len(targets) < 2:
        return targets
    groups: dict[tuple[int, str], list[tuple[int, str, int]]] = {}
    for t in targets:
        groups.setdefault((t[2], t[1]), []).append(t)
    if all(len(g) == 1 for g in groups.values()):
        return targets
    keep: set[int] = set()
    for g in groups.values():
        if len(g) == 1:
            keep.add(g[0][0])
            continue
        ordered = sorted(g, key=lambda t: span_of.get(t[0], (0, 0, ""))[0])
        if span_of.get(ordered[0][0], (0, 0, ""))[2] == "method":
            others = [t for t in ordered if t[0] != src_id]
            keep.add((others or ordered)[0][0])
            continue
        above = [t for t in ordered if span_of.get(t[0], (0, 0, ""))[0] <= line]
        keep.add((above[-1] if above else ordered[0])[0])
    return [t for t in targets if t[0] in keep]


def _resolve_type_ref(
    *,
    candidates: list[tuple[int, str, int]],
    scope: str | None,
    src_qname: str,
    src_file_id: int,
    family: str,
    project_scopes: set[str],
    file_paths: dict[int, str],
    target_name: str = "",
    py_modules: PyModules | None = None,
    allow_enclosing: bool = False,
    ts_modules: TsModules | None = None,
    ts_candidates: Callable[[str], list[tuple[int, str, int]]] | None = None,
) -> tuple[list[tuple[int, str, int]], float]:
    """Targets and weight for one `inherit` / `type` ref. `candidates` are
    already the same-language, type-like symbols carrying the name.

    Stricter than a call on purpose. A call by a bare name nobody imported
    still fans out to every same-named function (weight 0.5), because a missed
    import is the likelier explanation. A TYPE name the file neither imports
    nor declares is far more often a builtin or a library type (`Path`,
    `Request`, `List`) that merely shares its name with a project class, and
    an edge to that class would keep it alive in `find_dead_code` for no
    reason. So:

    1. an import that names a project module scopes the match (1.0 / 0.9);
       one that names anything else means "not ours" and yields nothing;
    2. else a declaration in the same file, nearest enclosing scope first;
    3. else whatever the language makes visible without an import: nothing in
       Python, ambient `.d.ts` declarations in TypeScript, the same package
       (directory) in Java and, failing that, a name unique in the project
       (a wildcard import), capped at 0.6 like any unconfirmed match.

    The declaration enclosing the ref is never a target: a method returning
    its own class does not make that class used.
    """
    candidates = [
        c
        for c in candidates
        if allow_enclosing
        or not (
            src_qname == c[1]
            or src_qname.startswith(f"{c[1]}.")
            or src_qname.startswith(f"{c[1]}::")
        )
    ]
    if scope is not None and family == "js" and ts_modules is not None and ts_candidates:
        # An imported TS/JS name: the module the import names, then its
        # `export ... from` chain (bounded, cycle-safe) to the ONE file that
        # declares it (`export { A as B }` renames along the way). Whatever
        # this cannot prove falls to the directory-prefix match below.
        ts_hit = ts_modules.lookup(scope, target_name, ts_candidates)
        if ts_hit is not None:
            return [ts_hit[0]], 0.9 if ts_hit[1] else 1.0
    if not candidates:
        return [], 0.0
    if scope and family == "python" and py_modules is not None:
        # An imported Python name: the file's own import says which module it
        # came from, resolved to ONE indexed file (relative dots counted from
        # the importer, whole dotted segments, re-exports followed to the
        # single symbol they bind). An import that names no indexed module
        # (`typing`, a third-party package) or one that does not yield exactly
        # one symbol links nothing: precision over recall. The answer depends
        # only on the files, never on which scopes happen to exist.
        hit = py_modules.lookup_type(scope, target_name, src_file_id, candidates)
        if hit is None:
            return [], 0.0
        return [hit[0]], 0.9 if hit[1] else 1.0
    if scope and family == "java":
        exact = [c for c in candidates if _java_scope_matches(scope, target_name, c[1])]
        if len(exact) == 1:
            return exact, 1.0
    if scope:
        scoped = _under_scope(candidates, scope)
        if scoped:
            return scoped, 1.0 if len(scoped) == 1 else 0.9
        if scope not in project_scopes:
            return [], 0.0
    local = [c for c in candidates if c[2] == src_file_id]
    if local:
        if len(local) == 1:
            return local, 1.0
        by_scope: dict[str, list[tuple[int, str, int]]] = {}
        for cand in local:
            owner = cand[1].rsplit(".", 1)[0] if "." in cand[1] else ""
            by_scope.setdefault(owner, []).append(cand)
        parts = src_qname.split(".")
        for depth in range(len(parts), 0, -1):
            enclosing = ".".join(parts[:depth])
            if enclosing in by_scope:
                return by_scope[enclosing], 0.8
        return local, 0.7
    if scope:
        # The import names a project module and the symbol was not found under
        # it (a re-export): ambiguous, exactly as for a call.
        return candidates, 1.0 if len(candidates) == 1 else 0.5
    if family == "js":
        ambient = [c for c in candidates if file_paths.get(c[2], "").endswith(".d.ts")]
        return ambient, 1.0 if len(ambient) == 1 else 0.5
    if family == "java":
        src_dir = file_paths.get(src_file_id, "").rpartition("/")[0]
        package = [c for c in candidates if file_paths.get(c[2], "").rpartition("/")[0] == src_dir]
        if package:
            return package, 1.0 if len(package) == 1 else 0.7
        if len(candidates) == 1:
            return candidates, 0.6
    return [], 0.0


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
    2. nearest enclosing lexical scope → weight 0.8. A nested helper binds to
       the definition in the closest enclosing scope, the way the language
       resolves it. Handles the case rule 3 cannot: twelve closures named
       ``text`` in ONE file, each inside a different parent.
    3. same source file as the call site → weight 0.7. Closes the v0.8 P2
       session-01 bug where short names like ``list_tools`` (defined in
       3 different modules) created edges to all 3 from a single in-module
       call site.
    4. otherwise: keep all candidates at weight 0.5 (legacy behavior). True
       cross-file ambiguous call where the extractor missed the import.
    Single-candidate matches are weight 1.0, except a method matched on an
    object of unknown type (``x.f()``, receiver ``object``), capped at 0.6.

    Before any of that, candidates are narrowed by the call itself: a call
    never crosses language families, and an attribute call ``X.f()`` reaches
    a module-level function only when ``X`` is an imported name (receiver
    ``module``); ``self.f()`` and a call on any other object reach methods
    only, and ``self.f()`` in a class with no declared base only its own.

    Refs of type ``inherit`` / ``type`` (Python, TypeScript/TSX, Java) take
    their own, stricter path (``_resolve_type_ref``) and are written as
    ``inherits`` / ``references`` edges through the same upsert, so the
    never-DELETE and reclaim-to-``livespec`` rules hold for them unchanged.
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
            f"SELECT u.src_symbol_id, u.target_name, u.scope_module, u.receiver, u.ref_type, "
            f"       u.receiver_class, u.line, "
            f"       s.file_id AS src_file_id, f.language AS src_language, "
            f"       s.qualified_name AS src_qname, s.signature AS src_signature "
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
        # A Python type ref resolves through module structure and re-exports
        # (`pkg/__init__.py` gaining `from .core import Thing`), which a changed
        # file can alter without defining a symbol of that name. They are few,
        # and re-walking them keeps the typed edges a function of the tree.
        # Same for a TS/JS type ref through a barrel (`export * from`).
        sql += (
            " OR (u.ref_type IN ('inherit', 'type') "
            "AND f.language IN ('python','typescript','tsx','javascript')))"
        )
        rows = conn.execute(sql, params).fetchall()
    else:
        rows = conn.execute(
            """SELECT u.src_symbol_id, u.target_name, u.scope_module, u.receiver, u.ref_type,
                      u.receiver_class, u.line,
                      s.file_id AS src_file_id, f.language AS src_language,
                      s.qualified_name AS src_qname, s.signature AS src_signature
               FROM symbol_ref u
               JOIN symbol s ON s.id = u.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (project_id,),
        ).fetchall()

    # name_index: short name -> [(symbol_id, qualified_name, file_id)]
    name_index: dict[str, list[tuple[int, str, int]]] = {}
    # Per symbol id: its language family and whether it is a method. A call
    # never crosses languages (a `.tsx` `parts.join(",")` is not a Python
    # `join`), and a call on an object can only reach a method.
    family_of: dict[int, str] = {}
    methods: set[int] = set()
    # Python class qname -> whether it declares a base (`class A(B)`).
    class_bases: dict[str, bool] = {}
    # What an `inherit` / `type` ref may resolve to, and where each file lives
    # (same-package and ambient-declaration visibility are path questions).
    type_like: set[int] = set()
    file_paths: dict[int, str] = {}
    span_of: dict[int, tuple[int, int, str]] = {}

    def collapse(
        targets: list[tuple[int, str, int]], line: int, src_id: int = -1
    ) -> list[tuple[int, str, int]]:
        return _collapse_same_qname(targets, line, span_of, src_id)

    for r in conn.execute(
        """SELECT s.id, s.name, s.qualified_name, s.file_id, s.kind, s.signature, f.language,
                  f.path, s.start_line, s.end_line
           FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (project_id,),
    ):
        sid = int(r["id"])
        name_index.setdefault(r["name"], []).append((sid, r["qualified_name"], int(r["file_id"])))
        family_of[sid] = _language_family(r["language"])
        file_paths[int(r["file_id"])] = r["path"]
        span_of[sid] = (int(r["start_line"] or 0), int(r["end_line"] or 0), str(r["kind"]))
        if r["kind"] in _TYPE_LIKE_KINDS:
            type_like.add(sid)
        if r["kind"] == "method":
            methods.add(sid)
        elif r["kind"] == "class" and r["language"] == "python":
            class_bases[r["qualified_name"]] = _declares_base(r["signature"])

    # Every module prefix this project defines. Used to tell "the import names
    # a project module and we failed to match the symbol" (ambiguous — keep the
    # old fan-out) apart from "the import names something OUTSIDE the project"
    # (certain — emit nothing).
    project_scopes: set[str] = set()
    for _sid, qname, _fid in (c for cands in name_index.values() for c in cands):
        parts = qname.split(".")
        for i in range(1, len(parts)):
            project_scopes.add(".".join(parts[:i]))
            project_scopes.add(parts[i - 1])

    py_files = {
        int(r["id"]): r["path"]
        for r in conn.execute(
            "SELECT id, path FROM file WHERE project_id=? AND language='python'", (project_id,)
        )
    }
    py_modules = PyModules(py_files, _source_reader(conn, project_id, py_files))
    ts_files = {
        int(r["id"]): r["path"]
        for r in conn.execute(
            "SELECT id, path FROM file WHERE project_id=? "
            "AND language IN ('typescript','tsx','javascript')",
            (project_id,),
        )
    }
    ts_modules = TsModules(ts_files, _source_reader(conn, project_id, ts_files))

    def ts_type_candidates(name: str) -> list[tuple[int, str, int]]:
        return [
            c for c in name_index.get(name, []) if family_of.get(c[0]) == "js" and c[0] in type_like
        ]

    def ts_any_candidates(name: str) -> list[tuple[int, str, int]]:
        return [c for c in name_index.get(name, []) if family_of.get(c[0]) == "js"]

    # Inheritance and type refs first, flushed before any call is resolved: a
    # `super().f()` needs the `inherits` edges of this very walk.
    rows = sorted(rows, key=lambda u: _REF_EDGE_TYPE.get(u["ref_type"]) is None)
    hierarchy: Hierarchy | None = None

    def get_hierarchy() -> Hierarchy:
        nonlocal hierarchy
        if hierarchy is None:
            hierarchy = Hierarchy(conn, [project_id])
        return hierarchy

    def flush_edges() -> None:
        conn.executemany(
            """INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight)
               VALUES(?,?,?,?)
               ON CONFLICT(src_symbol_id, dst_symbol_id, edge_type)
               DO UPDATE SET weight = MAX(symbol_edge.weight, excluded.weight),
                             origin = 'livespec'""",
            pending_edges,
        )
        pending_edges.clear()

    flushed_types = False
    edge_count = 0
    # (src, dst) -> best weight written this walk. Two refs of one caller can
    # reach the same callee with different evidence (`helper()` and
    # `obj.helper()`): the stronger one must win whatever the row order.
    seen_pairs: dict[tuple[int, int, str], float] = {}
    pending_edges: list[tuple[int, int, str, float]] = []

    def emit_call(src_id: int, tid: int, weight: float) -> None:
        nonlocal edge_count
        if tid == src_id:
            return
        key = (src_id, tid, "calls")
        previous = seen_pairs.get(key)
        if previous is not None and previous >= weight:
            return
        seen_pairs[key] = weight
        pending_edges.append((src_id, tid, "calls", weight))
        if previous is None:
            edge_count += 1

    for u in rows:
        if not flushed_types and _REF_EDGE_TYPE.get(u["ref_type"]) is None:
            flush_edges()
            flushed_types = True
        src_family = _language_family(u["src_language"])
        # An imported TS/JS name may be re-exported under ANOTHER name
        # (`export { A as B }`): the name-index lookup is not the last word.
        js_scoped = src_family == "js" and u["scope_module"] is not None
        candidates = name_index.get(u["target_name"], [])
        if not candidates and not js_scoped:
            continue

        # Calls stay inside their language family: a JS/TS call site cannot
        # call a Python function. 2.7k of 29k call edges on a mixed
        # Python + TypeScript repo crossed languages by bare name.
        candidates = [c for c in candidates if family_of.get(c[0]) == src_family]
        if not candidates and not js_scoped:
            continue

        edge_type = _REF_EDGE_TYPE.get(u["ref_type"], "calls")
        if edge_type != "calls":
            src_id = int(u["src_symbol_id"])
            targets, weight = _resolve_type_ref(
                candidates=[c for c in candidates if c[0] in type_like],
                scope=u["scope_module"],
                src_qname=str(u["src_qname"]),
                src_file_id=int(u["src_file_id"]),
                family=src_family,
                project_scopes=project_scopes,
                file_paths=file_paths,
                target_name=str(u["target_name"]),
                py_modules=py_modules,
                ts_modules=ts_modules,
                ts_candidates=ts_type_candidates,
            )
            for tid, _qname, _fid in collapse(targets, int(u["line"] or 0), src_id):
                type_key = (src_id, tid, edge_type)
                previous = seen_pairs.get(type_key)
                if previous is not None and previous >= weight:
                    continue
                seen_pairs[type_key] = weight
                pending_edges.append((src_id, tid, edge_type, weight))
                if previous is None:
                    edge_count += 1
            continue

        # The receiver of an attribute call (`X.f()`). Only an imported
        # receiver is evidence of a module; `self.f()` and a call on any other
        # object (`"".join`, `parts.join`, `self.store.add`) can only reach a
        # METHOD. Linking them to a module-level function of the same name by
        # bare name made every `"\n".join(...)` in a repo a caller of the one
        # project function named `join`. Rows written before the column
        # existed carry NULL and resolve as they always did.
        receiver = u["receiver"]
        src_id_ = int(u["src_symbol_id"])
        if receiver == RECEIVER_SUPER:
            # `super().f()` / `super.f()` / `super(...)`: the method of the
            # nearest indexed base of the enclosing class that defines it, in
            # left-to-right depth-first order. No such base, or an explicit
            # `super(Other, self)` naming another class: no edge.
            hier = get_hierarchy()
            owner_cls = hier.owner_class(src_id_)
            if owner_cls is None:
                continue
            named = u["receiver_class"]
            if named and named != hier.name(owner_cls):
                continue
            found = hier.find_in_bases(owner_cls, str(u["target_name"]))
            if found is not None:
                emit_call(src_id_, found, 1.0)
            continue
        if receiver in (RECEIVER_OBJECT, RECEIVER_TYPE) and u["receiver_class"]:
            # `c = Child(); c.f()`: the receiver is evidently a `Child`, so the
            # call reaches the most-derived definition on Child's own MRO. A
            # class that does not resolve to ONE project class (a factory
            # function, a library type) leaves the call to the name-based path.
            raw = str(u["receiver_class"])
            rscope, _, rname = raw.rpartition("::")
            cls_cands = [
                c
                for c in name_index.get(rname, [])
                if c[0] in type_like and family_of.get(c[0]) == src_family
            ]
            hits, _w = _resolve_type_ref(
                candidates=cls_cands,
                scope=rscope or None,
                src_qname=str(u["src_qname"]),
                src_file_id=int(u["src_file_id"]),
                family=src_family,
                project_scopes=project_scopes,
                file_paths=file_paths,
                target_name=rname,
                py_modules=py_modules,
                allow_enclosing=True,
            )
            if len(hits) == 1:
                hier = get_hierarchy()
                if hier.is_class(hits[0][0]):
                    found = hier.find_in_mro(hits[0][0], str(u["target_name"]))
                    if found is not None:
                        emit_call(src_id_, found, 0.9)
                        continue
                    # Not found: nobody defines it only when the WHOLE ancestry
                    # is indexed; with a library or unresolved base the method
                    # may live there, so the name-based path decides.
                    if receiver == RECEIVER_TYPE or hier.is_complete(hits[0][0]):
                        continue
            if receiver == RECEIVER_TYPE:
                continue  # a class name that is not ONE project class: no edge
        if receiver in (RECEIVER_SELF, RECEIVER_OBJECT):
            candidates = [c for c in candidates if c[0] in methods]
            if receiver == RECEIVER_SELF:
                # `self.f()` in a class that declares no base can only reach its
                # own methods (or ones nested in it): nothing is inherited.
                owner = _enclosing_class(str(u["src_qname"]), class_bases)
                if owner is not None and not class_bases[owner]:
                    candidates = [c for c in candidates if c[1].startswith(f"{owner}.")]
            if not candidates:
                continue

        # A call to one of the caller's OWN PARAMETERS is a call through a
        # value, not to any project symbol — the callee is whatever the caller
        # was handed. `_ts_http_call_uses_param(call_node, param, text)` calling
        # `text(n)` was resolving to all twelve closures named `text` elsewhere
        # in the file, none of which it can possibly reach. Emitting an edge per
        # same-named definition is not an imprecise answer, it is a wrong one.
        if _is_caller_parameter(u["src_signature"], u["target_name"]):
            continue

        # P0.4: if the ref carries a scope_module (Python imports), prefer
        # candidates whose qualified_name lives under that module. If at least
        # one matches, drop the rest — that's a confident, scoped resolution.
        scope = u["scope_module"]
        scoped: list[tuple[int, str, int]] = []
        if scope and src_family == "python":
            # Resolved like a type ref's import: to an indexed module by whole
            # dotted segments, not by the module's name appearing somewhere in
            # a qualified name. `import typing as t; t.cast()` stays outside
            # the project even when a directory is called `typing`.
            found = py_modules.module_scope_prefix(scope, int(u["src_file_id"]))
            if found is None:
                continue
            prefix, past_module, module_file = found
            scoped = [c for c in candidates if c[1].startswith(f"{prefix}.")]
            # A package's own definition beats a same-named method or function
            # of one of its submodules (`from .json import dumps`).
            own = [c for c in scoped if c[2] == module_file]
            if own and not past_module:
                scoped = own
            if scoped:
                candidates = scoped
            elif past_module:
                # `request_ctx.pop()`, `_shells.clear()`: a VALUE the module
                # holds, whose methods live somewhere unknown. Dropped, as a
                # relative import always was; only an absolute one used to
                # fan out to the whole top package. A module that merely
                # failed to match the symbol (a re-export) stays ambiguous.
                continue
        elif (
            scope is not None
            and src_family == "js"
            and (ts_hit := ts_modules.lookup(scope, str(u["target_name"]), ts_any_candidates))
        ):
            scoped = [ts_hit[0]]
            candidates = scoped
        elif scope:
            scoped = _under_scope(candidates, scope)
            if not scoped and receiver == RECEIVER_MODULE and scope not in project_scopes:
                # `from pkg import client; client.get()`: the receiver scope is
                # `pkg.client`, which names no project module when `client` is
                # a VALUE (an instance) rather than a module. Fall back to the
                # nearest enclosing scope that is a project module, the
                # package the name was imported from — what this resolved to
                # before receivers were classified. `os.path` has no such
                # parent and is still dropped below.
                parent = scope
                while "." in parent and not scoped:
                    parent = parent.rsplit(".", 1)[0]
                    if parent in project_scopes:
                        scoped = _under_scope(candidates, parent)
                        break
            if scoped:
                candidates = scoped
            elif scope not in project_scopes:
                # The ref carries an import and that import points OUTSIDE this
                # project — `os.walk`, `pathlib.Path`, `json.loads`. The
                # extractor stores only the call's last name, so `os.walk`
                # arrives as `walk` and used to fan out to every project
                # function named `walk` (six of them here). The scope is the
                # evidence that none of them is the target.
                #
                # Only when the scope matches NO project module at all: an
                # import of a real project module that simply failed to match
                # the symbol stays ambiguous rather than being dropped.
                continue

        # v0.32: lexical scope. The same-file rule below cannot help when the
        # duplicates are all in ONE file, which is the common shape for nested
        # helpers: `extractors.py` defines a closure named `text` inside twelve
        # different parent functions, so a call to `text` from any one of them
        # resolved to all twelve. 20% of this repo's call edges were that.
        #
        # Lexical scoping is what the language itself does: the name binds to
        # the nearest ENCLOSING definition. Walk the caller's scope chain from
        # innermost outward and take the first scope that defines a candidate.
        lexical: list[tuple[int, str, int]] = []
        if not scoped and len(candidates) > 1:
            parts = str(u["src_qname"]).split(".")
            # Innermost first: the caller's own scope, then each parent. A
            # candidate defined *inside* the caller wins over a sibling's.
            by_scope: dict[str, list[tuple[int, str, int]]] = {}
            for cand in candidates:
                owner = cand[1].rsplit(".", 1)[0] if "." in cand[1] else ""
                by_scope.setdefault(owner, []).append(cand)
            for depth in range(len(parts), 0, -1):
                enclosing = ".".join(parts[:depth])
                if enclosing in by_scope:
                    lexical = by_scope[enclosing]
                    break
            if lexical:
                candidates = lexical

        # v0.8 P2 fix: when scope didn't disambiguate AND there are still
        # multiple candidates, prefer same-file candidates. An in-module
        # call to a short name almost always resolves locally; without this
        # the resolver fans out to every same-named symbol across the repo
        # (battle-test session 01: list_tools x3, _cosine x2).
        same_file: list[tuple[int, str, int]] = []
        if not scoped and not lexical and len(candidates) > 1:
            src_file_id = int(u["src_file_id"])
            same_file = [c for c in candidates if c[2] == src_file_id]
            if same_file:
                candidates = same_file

        if len(candidates) == 1:
            weight = 1.0
        elif scoped:
            weight = 0.9
        elif lexical:
            # Below an explicit import (0.9) — that is stated evidence — and
            # above same-file (0.7), which only says "somewhere in this file".
            weight = 0.8
        elif same_file:
            weight = 0.7
        else:
            weight = 0.5
        if receiver == RECEIVER_OBJECT:
            # A method matched by name on an object whose type is not known
            # (`store.add()`, `sep.join()`): plausible, never confirmed. 0.6 is
            # the default traversal threshold, so these stay visible, and
            # `min_weight=0.7` keeps only edges backed by an import, `self`
            # or a bare name in scope.
            weight = min(weight, 0.6)
        for tid, _qname, _fid in collapse(candidates, int(u["line"] or 0), int(u["src_symbol_id"])):
            src_id = int(u["src_symbol_id"])
            if tid == src_id:
                continue
            key = (src_id, tid, "calls")
            previous = seen_pairs.get(key)
            if previous is not None and previous >= weight:
                continue
            seen_pairs[key] = weight
            # Never DELETE (edges from unchanged files must survive); but DO
            # refresh the weight monotonically so a previously-ambiguous edge
            # (0.5 fan-out) upgrades once disambiguation makes it unambiguous.
            # MAX avoids a stray targeted re-resolve DOWNGRADING a confirmed
            # 1.0 edge back to 0.5.
            pending_edges.append((src_id, tid, "calls", weight))
            if previous is None:
                edge_count += 1
    # One statement for every edge, in the order they were decided: an upsert
    # per row, exactly as one execute() each.
    flush_edges()
    return edge_count
