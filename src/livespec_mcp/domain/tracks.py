"""Tracks: what a git range changed, symbol by symbol, and which Specs it touched.

A *track* is a macro-commit: a named ``base..head`` range (a branch, a PR, a
run of commits) plus the reason it exists. This module computes the draft of
one — it reads git, never the working tree and never the index's view of the
base side:

* ``diff_symbols`` — per symbol, ``added`` (in head, not base), ``modified``
  (``body_hash`` differs, so reformat-only edits do not count) and ``removed``
  (in base, not head). Both sides come from ``git cat-file`` and the regular
  extractors run on the text. Renamed files pair their symbols by the name
  local to the module, so a move is not a remove plus an add.
* ``openspec_deltas`` — requirement-level ``ADDED/MODIFIED/REMOVED/RENAMED``
  deltas from OpenSpec change files the range adds or edits.
* ``draft_track`` — both of the above folded onto Spec ids: annotations and the
  link table at head for what exists at head, base-side annotations for what
  was removed. Each Spec is ``changed`` or only ``touched`` (``SpecTouch.tier``).

Writing the draft into ``CHANGESPEC.md`` lives in ``domain/changespec.py``.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import xxhash

from livespec_mcp.domain.extractors import ExtractedSymbol, extract
from livespec_mcp.domain.git_range import (
    DiffEntry,
    GitProblem,
    commit_date,
    diff_name_status,
    read_blobs,
    resolve_commit,
)
from livespec_mcp.domain.languages import detect_language
from livespec_mcp.domain.matcher import AnnotationHit, derive_spec_prefixes, parse_annotations
from livespec_mcp.domain.md_specs import parse_openspec_markdown

CHANGES = ("added", "modified", "removed")
_SCOPE_SEPS = (".", "::", "#")


def module_of(path: str) -> str:
    """The module prefix the extractors give every symbol of ``path``."""
    return ".".join(PurePosixPath(path).with_suffix("").parts)


def local_name(qname: str, path: str) -> str:
    """``qname`` without its module prefix (``Class.method``, ``fn``)."""
    mod = module_of(path)
    if mod and qname.startswith(mod + "."):
        return qname[len(mod) + 1 :]
    return qname


def qualify(local: str, path: str) -> str:
    """Inverse of ``local_name``: rebuild the qualified name from the file."""
    mod = module_of(path)
    if not mod or local.startswith(mod + "."):
        return local
    return f"{mod}.{local}"


@dataclass
class SymbolChange:
    change: str  # added | modified | removed
    qualified_name: str
    name: str  # local to the module: what CHANGESPEC.md shows
    kind: str | None
    path: str  # head-side path; base-side for a removed symbol
    old_path: str | None = None  # base-side path when the file moved
    docstring: str | None = None
    specs: list[str] = field(default_factory=list)
    # The Spec this symbol's own `@spec:` names first (see ``primary_spec``).
    primary: str | None = None

    def as_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "qualified_name": self.qualified_name,
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
        }
        if self.old_path:
            out["old_path"] = self.old_path
        return out


@dataclass
class SymbolDiff:
    changes: list[SymbolChange] = field(default_factory=list)
    # path -> why it could not be compared symbol by symbol (syntax error,
    # grammar missing, blob unreadable). Callers that need a safe answer
    # (git_diff_impact) treat every symbol of such a file as changed.
    unparsed: dict[str, str] = field(default_factory=dict)


def _symbol_table(
    path: str, source: str | None, root: Path
) -> tuple[dict[str, tuple[str, ExtractedSymbol]] | None, str | None]:
    """``{local name: (body hash, symbol)}`` for one side of a file."""
    if source is None:
        return None, "blob unreadable"
    _, result = extract(Path(path), source, root)
    if result.grammar_missing:
        return None, f"grammar unavailable: {result.grammar_missing}"
    if result.parse_error:
        return None, "parse error"
    table: dict[str, tuple[str, ExtractedSymbol]] = {}
    for s in result.symbols:
        local = local_name(s.qualified_name, path)
        h = xxhash.xxh3_128_hexdigest(s.body_hash_seed.encode("utf-8", errors="replace"))
        if local in table:
            # Same name twice in one file (an if/else shim): the pair is one
            # identity, and either body changing changes it.
            prev_h, first = table[local]
            h = xxhash.xxh3_128_hexdigest(f"{prev_h}|{h}".encode())
            table[local] = (h, first)
        else:
            table[local] = (h, s)
    return table, None


def _is_inside(child: str, parent: str) -> bool:
    return any(child.startswith(parent + sep) for sep in _SCOPE_SEPS)


def diff_symbols(
    ws_root: str, base_sha: str, head_sha: str, entries: list[DiffEntry]
) -> SymbolDiff:
    """@spec:tracks-symbol-level-range-diff

    Added / modified / removed symbols between two commits.

    ``modified`` means the normalised ``body_hash`` differs — the same seed the
    indexer stores — so whitespace and comment-only edits never count. A
    container (class, impl) whose only change is inside a member that is itself
    reported is not reported again: the member is the change."""
    root = Path(ws_root)
    out = SymbolDiff()
    code = [e for e in entries if any(detect_language(Path(p)) is not None for p in e.paths)]
    want: list[tuple[str, str]] = []
    for e in code:
        if e.old_path:
            want.append((base_sha, e.old_path))
        if e.new_path:
            want.append((head_sha, e.new_path))
    blobs = read_blobs(ws_root, want)
    for e in code:
        base_tab: dict[str, tuple[str, ExtractedSymbol]] = {}
        head_tab: dict[str, tuple[str, ExtractedSymbol]] = {}
        if e.old_path:
            tab, why = _symbol_table(e.old_path, blobs.get((base_sha, e.old_path)), root)
            if tab is None:
                out.unparsed[e.old_path] = why or "unreadable"
                if e.new_path:
                    out.unparsed[e.new_path] = why or "unreadable"
                continue
            base_tab = tab
        if e.new_path:
            tab, why = _symbol_table(e.new_path, blobs.get((head_sha, e.new_path)), root)
            if tab is None:
                out.unparsed[e.new_path] = why or "unreadable"
                if e.old_path:
                    out.unparsed[e.old_path] = why or "unreadable"
                continue
            head_tab = tab
        moved = e.old_path if (e.old_path and e.new_path and e.old_path != e.new_path) else None
        file_changes: list[SymbolChange] = []
        for local, (h, sym) in head_tab.items():
            prev = base_tab.get(local)
            if prev is None:
                change = "added"
            elif prev[0] != h:
                change = "modified"
            else:
                continue
            file_changes.append(
                SymbolChange(
                    change=change,
                    qualified_name=sym.qualified_name,
                    name=local,
                    kind=sym.kind,
                    path=e.new_path or "",
                    old_path=moved if change == "modified" else None,
                    docstring=sym.docstring,
                )
            )
        for local, (_h, sym) in base_tab.items():
            if local in head_tab:
                continue
            file_changes.append(
                SymbolChange(
                    change="removed",
                    qualified_name=sym.qualified_name,
                    name=local,
                    kind=sym.kind,
                    path=e.old_path or "",
                    docstring=sym.docstring,
                )
            )
        changed_locals = [c.name for c in file_changes]
        for c in file_changes:
            if c.change == "modified" and any(
                _is_inside(other, c.name) for other in changed_locals if other != c.name
            ):
                continue
            out.changes.append(c)
    return out


def changed_qnames_by_path(
    ws_root: str, base_ref: str, head_ref: str, entries: list[DiffEntry]
) -> dict[str, set[str] | None]:
    """@spec:analysis-impact-analysis

    For ``git_diff_impact``: per changed path, the qualified names of the
    symbols the range really changed there. ``None`` for a path that could not
    be compared (unparseable, unresolvable refs) — the caller then keeps every
    indexed symbol of that file, the old file-level answer."""
    base_sha, p1 = resolve_commit(ws_root, base_ref)
    head_sha, p2 = resolve_commit(ws_root, head_ref)
    paths = {p for e in entries for p in e.paths}
    if p1 is not None or p2 is not None or not base_sha or not head_sha:
        return dict.fromkeys(paths)
    diff = diff_symbols(ws_root, base_sha, head_sha, entries)
    out: dict[str, set[str] | None] = {p: set() for p in paths}
    for p in diff.unparsed:
        out[p] = None
    for c in diff.changes:
        bucket = out.get(c.path)
        if bucket is not None:
            bucket.add(c.qualified_name)
    return out


# ---------------------------------------------------------------- OpenSpec


@dataclass
class SpecDelta:
    operation: str  # ADDED | MODIFIED | REMOVED | RENAMED
    requirement: str
    spec_id: str
    change: str
    path: str
    rename_from: str | None = None

    def as_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "operation": self.operation,
            "requirement": self.requirement,
            "change": self.change,
            "path": self.path,
        }
        if self.rename_from:
            out["rename_from"] = self.rename_from
        return out


def _delta_file(path: str) -> tuple[str, str] | None:
    """``(change name, capability)`` when ``path`` is an OpenSpec change's
    delta spec (``openspec/changes/[archive/]<name>/specs/**/spec.md``)."""
    parts = PurePosixPath(path).parts
    if len(parts) < 5 or parts[-1] != "spec.md":
        return None
    for i in range(len(parts) - 1):
        if parts[i] == "openspec" and parts[i + 1] == "changes":
            rest = parts[i + 2 :]
            if rest and rest[0] == "archive":
                rest = rest[1:]
            if len(rest) >= 4 and rest[1] == "specs":
                return rest[0], parts[-2]
    return None


def _parsed_deltas(text: str | None, capability: str) -> dict[tuple[str, str], Any]:
    if not text:
        return {}
    try:
        parsed = parse_openspec_markdown(text, capability=capability)
    except ValueError:
        return {}
    return {(p.operation or "added", p.spec_id): p for p in parsed}


def openspec_deltas(
    ws_root: str, base_sha: str, head_sha: str, entries: list[DiffEntry]
) -> list[SpecDelta]:
    """@spec:tracks-openspec-deltas-in-a-track

    Requirement-level deltas from the OpenSpec change files a range adds,
    moves (archiving is how a change lands) or edits. An edited file yields
    only the requirements whose text changed."""
    picked: list[tuple[DiffEntry, str, str]] = []
    for e in entries:
        if not e.new_path or e.status == "D":
            continue
        hit = _delta_file(e.new_path)
        if hit:
            picked.append((e, hit[0], hit[1]))
    if not picked:
        return []
    want: list[tuple[str, str]] = []
    for e, _c, _cap in picked:
        want.append((head_sha, e.new_path or ""))
        if e.status == "M" and e.old_path:
            want.append((base_sha, e.old_path))
    blobs = read_blobs(ws_root, want)
    out: list[SpecDelta] = []
    for e, change, cap in picked:
        head = _parsed_deltas(blobs.get((head_sha, e.new_path or "")), cap)
        base = (
            _parsed_deltas(blobs.get((base_sha, e.old_path)), cap)
            if e.status == "M" and e.old_path
            else {}
        )
        for key, p in head.items():
            prev = base.get(key)
            if prev is not None and (prev.title, prev.description) == (p.title, p.description):
                continue
            out.append(
                SpecDelta(
                    operation=(p.operation or "added").upper(),
                    requirement=p.title,
                    spec_id=p.spec_id,
                    change=change,
                    path=e.new_path or "",
                    rename_from=p.rename_from,
                )
            )
    return out


def spec_ids_in_tree(ws_root: str, sha: str) -> set[str] | None:
    """Spec ids the OpenSpec tree (``openspec/specs/**/spec.md``) holds at
    ``sha``, or None when that commit has no OpenSpec specs.

    The link table describes the code as indexed now; a range from the past
    must not credit a Spec that did not exist yet at its head."""
    try:
        proc = subprocess.run(
            ["git", "-C", ws_root, "ls-tree", "-r", "--name-only", "--end-of-options", sha],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    files = []
    for path in proc.stdout.splitlines():
        parts = PurePosixPath(path).parts
        if parts[-1:] != ("spec.md",) or len(parts) < 4:
            continue
        for i in range(len(parts) - 2):
            if parts[i] == "openspec" and parts[i + 1] == "specs":
                files.append(path)
                break
    if not files:
        return None
    blobs = read_blobs(ws_root, [(sha, f) for f in files])
    ids: set[str] = set()
    for f in files:
        text = blobs.get((sha, f))
        if not text:
            continue
        try:
            ids |= {
                p.spec_id
                for p in parse_openspec_markdown(text, capability=PurePosixPath(f).parent.name)
            }
        except ValueError:
            continue
    return ids


# ---------------------------------------------------------------- draft


@dataclass
class SpecTouch:
    spec_id: str
    title: str | None
    known: bool
    deltas: list[SpecDelta] = field(default_factory=list)
    symbols: list[SymbolChange] = field(default_factory=list)

    @property
    def tier(self) -> str:
        """@spec:tracks-changed-and-touched-specs

        ``changed`` when the range carries a requirement delta for this Spec or
        it is the primary explicit annotation of a symbol the range changed;
        ``touched`` when it is reached only through shared code (a secondary
        id, a verb-anchored mention, a link-table row)."""
        if self.deltas or any(c.primary == self.spec_id for c in self.symbols):
            return "changed"
        return "touched"


@dataclass
class TrackDraft:
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    date: str
    files: list[DiffEntry]
    specs: dict[str, SpecTouch]
    unlinked: list[SymbolChange]
    unparsed: dict[str, str]

    @property
    def track_id(self) -> str:
        return track_id_for(self.base_sha, self.head_sha)

    @property
    def changed_specs(self) -> list[SpecTouch]:
        return [t for t in self.specs.values() if t.tier == "changed"]

    @property
    def touched_specs(self) -> list[SpecTouch]:
        return [t for t in self.specs.values() if t.tier == "touched"]

    @property
    def changes(self) -> list[str]:
        """OpenSpec change folders the range carries, in order."""
        seen: list[str] = []
        for t in self.specs.values():
            for d in t.deltas:
                if d.change not in seen:
                    seen.append(d.change)
        return seen


def track_id_for(base_sha: str, head_sha: str) -> str:
    return f"{base_sha[:7]}-{head_sha[:7]}"


def primary_spec(hits: list[AnnotationHit]) -> str | None:
    """@spec:tracks-changed-and-touched-specs

    The Spec a symbol names as its own: the first id of its first level-1
    ``@spec:`` / ``@implements:`` line (``parse_annotations`` keeps source
    order and has already applied ``@not_spec``). ``@tests:`` / ``@see:`` and
    verb-anchored mentions say the code relates to a Spec, not that it is it."""
    return next((h.spec_id for h in hits if h.explicit and h.relation == "implements"), None)


def _index_links(
    conn: sqlite3.Connection, project_id: int, paths: set[str]
) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    if not paths:
        return out
    plist = sorted(paths)
    for i in range(0, len(plist), 500):
        chunk = plist[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"""SELECT f.path, s.qualified_name, sp.spec_id
                FROM spec_symbol ss
                JOIN spec sp ON sp.id = ss.spec_id
                JOIN symbol s ON s.id = ss.symbol_id
                JOIN file f ON f.id = s.file_id
                WHERE f.project_id = ? AND f.path IN ({marks})""",
            (project_id, *chunk),
        ):
            out.setdefault((r["path"], r["qualified_name"]), set()).add(r["spec_id"])
    return out


def _spec_universe(
    conn: sqlite3.Connection, project_id: int
) -> tuple[dict[str, str], dict[str, str]]:
    """``({spec_id: title}, {alias: current spec_id})``."""
    from livespec_mcp.domain.spec_alias import alias_map

    titles = {
        r["spec_id"]: r["title"]
        for r in conn.execute("SELECT spec_id, title FROM spec WHERE project_id=?", (project_id,))
    }
    aliases = {a: sid for a, (_pk, sid) in alias_map(conn, project_id).items()}
    return titles, aliases


def draft_track(
    conn: sqlite3.Connection, project_id: int, ws_root: str, base_ref: str, head_ref: str
) -> tuple[TrackDraft | None, GitProblem | None]:
    """@spec:tracks-propose-a-track

    Everything a track would record for ``base_ref..head_ref``; writes nothing."""
    base_sha, problem = resolve_commit(ws_root, base_ref)
    if problem is not None:
        return None, problem
    head_sha, problem = resolve_commit(ws_root, head_ref)
    if problem is not None:
        return None, problem
    assert base_sha and head_sha
    entries, problem = diff_name_status(ws_root, base_sha, head_sha)
    if problem is not None:
        return None, problem
    entries = entries or []
    diff = diff_symbols(ws_root, base_sha, head_sha, entries)
    deltas = openspec_deltas(ws_root, base_sha, head_sha, entries)

    titles, aliases = _spec_universe(conn, project_id)

    def canon(sid: str) -> str:
        return aliases.get(sid, sid)

    known_ids = set(titles) | set(aliases) | {d.spec_id for d in deltas}
    prefixes = derive_spec_prefixes(titles.keys())
    links = _index_links(conn, project_id, {c.path for c in diff.changes})

    specs: dict[str, SpecTouch] = {}

    def touch(sid: str) -> SpecTouch:
        sid = canon(sid)
        if sid not in specs:
            delta_title = next((d.requirement for d in deltas if d.spec_id == sid), None)
            specs[sid] = SpecTouch(
                spec_id=sid, title=titles.get(sid) or delta_title, known=sid in titles
            )
        return specs[sid]

    for d in deltas:
        touch(d.spec_id).deltas.append(d)

    # OpenSpec repos: only Specs that existed at the side the symbol lives on
    # (head for added/modified, base for removed), or that the range adds.
    delta_ids = {canon(d.spec_id) for d in deltas}
    at_head = spec_ids_in_tree(ws_root, head_sha)
    at_base = spec_ids_in_tree(ws_root, base_sha)
    unlinked: list[SymbolChange] = []
    for c in diff.changes:
        hits = parse_annotations(c.docstring or "", prefixes, known_ids=known_ids)
        found = {canon(hit.spec_id) for hit in hits}
        found |= {canon(s) for s in links.get((c.path, c.qualified_name), set())}
        existing = at_base if c.change == "removed" else at_head
        if existing is not None:
            allowed = {canon(s) for s in existing} | delta_ids
            found &= allowed
        c.specs = sorted(found)
        primary = primary_spec(hits)
        # A primary the commit did not hold credits nothing; the next id in
        # the docstring is not promoted in its place.
        c.primary = canon(primary) if primary is not None and canon(primary) in found else None
        if not found:
            unlinked.append(c)
        for sid in c.specs:
            touch(sid).symbols.append(c)

    date = commit_date(ws_root, head_sha) or ""
    ordered = dict(sorted(specs.items()))
    return (
        TrackDraft(
            base_ref=base_ref,
            head_ref=head_ref,
            base_sha=base_sha,
            head_sha=head_sha,
            date=date,
            files=entries,
            specs=ordered,
            unlinked=unlinked,
            unparsed=diff.unparsed,
        ),
        None,
    )


def symbols_by_change(items: list[SymbolChange]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in CHANGES}
    for c in items:
        out[c.change].append(c.as_payload())
    return out


_ALIAS_SPLIT_RE = re.compile(r"[:#]+")


def normalize_symbol_query(symbol: str) -> str:
    """``a::b`` / ``a#b`` / ``a.b`` all name the same symbol."""
    return _ALIAS_SPLIT_RE.sub(".", symbol.strip())
