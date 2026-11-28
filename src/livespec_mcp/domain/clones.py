"""Repo-wide clone clusters — read one copy instead of N.

`duplication.py` answers "does THIS snippet already exist?" in front of a
write. This module answers the comprehension question a legacy repo raises on
first contact: the same function is pasted N times, so which one do I read,
and what is different about the others?

It reuses the fingerprints `duplication.load_symbol_corpus` caches by
`body_hash`; nothing is re-tokenised when the cache is warm.

**How clusters are formed** (deterministic, no pairwise scan of the corpus):

1. *Level 0* — bucket by `structural_hash`. Every bucket with two or more
   symbols is already a clone group. O(n).
2. *Level 1* — one fingerprint per distinct shape, then an exact set-similarity
   join with a **prefix filter** (Bayardo et al., "Scaling up all pairs
   similarity search"): each shape's winnowed hashes are ordered rarest-first
   and only the first ``|x| - ceil(t*|x|) + 1`` are put in an inverted index.
   Two sets with Jaccard >= t are guaranteed to share a hash inside those
   prefixes, so the join misses nothing, and the common hashes — the ones that
   would make an inverted index quadratic — are never indexed at all.
   Candidates are then verified with the real Jaccard overlap.
   Cost: O(sum of prefix lengths x posting-list length of *rare* hashes) to
   generate candidates, plus O(set size) per candidate to verify. It degrades
   to quadratic only when the repo really is thousands of mutually similar
   but not identical bodies.
3. Verified pairs and level-0 buckets are merged with union-find (single
   linkage). A member can therefore sit below the threshold against the
   representative when it got in through a chain; its own `similarity` and
   diff say so rather than hiding it.

A cluster whose members all sit *inside* the members of another cluster (the
nested helper of a pasted function) is dropped: it is the same duplicated text
counted twice.

No MCP coupling: what counts as a test file or a generated path is injected by
the caller, and every failure is data.
"""

from __future__ import annotations

import difflib
import io
import math
import re
import sqlite3
import tokenize
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from livespec_mcp.domain.duplication import (
    MIN_TOKENS_FOR_LEVEL1,
    NEAR_DUPLICATE_THRESHOLD,
    CorpusSymbol,
    load_symbol_corpus,
)
from livespec_mcp.domain.graph import _db_path, _latest_run_id

# Payload bounds. The point of the tool is to cost less than reading the
# copies, so a cluster must never grow with the size of what it summarises.
MAX_OTHERS_LISTED = 8
MAX_DIFF_LINES_PER_MEMBER = 8
MAX_DIFF_LINES_PER_CLUSTER = 24
MAX_DIFF_LINE_WIDTH = 120
MAX_SUBSTITUTIONS = 8
MAX_SUBSTITUTION_WIDTH = 40

_SQL_IN_CHUNK = 900


# ---------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Smaller root wins: the result does not depend on the order the
            # pairs were discovered in.
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _prefix_len(n: int, threshold: float) -> int:
    return max(1, min(n, n - math.ceil(threshold * n - 1e-9) + 1))


def similar_shape_pairs(
    shapes: list[tuple[str, frozenset[int]]],
    threshold: float,
    token_counts: list[int] | None = None,
) -> tuple[list[tuple[int, int]], int]:
    """Index pairs of *shapes* whose winnowed sets overlap >= threshold.

    `shapes` is ``(structural_hash, hashes)``; returns ``(pairs, candidates)``
    where `candidates` is how many pairs had to be verified — the number that
    tells you whether the prefix filter is earning its keep.

    `token_counts`, when given, adds a length check: winnowed hashes are a
    *set*, so a 70-line run of `parser.add_argument(...)` and a 25-line one
    can overlap almost completely. A body is not a near-copy of one several
    times its size, whatever the overlap says.
    """
    order = sorted(range(len(shapes)), key=lambda i: (len(shapes[i][1]), shapes[i][0]))
    df: Counter[int] = Counter()
    for _, hashes in shapes:
        df.update(hashes)

    index: dict[int, list[int]] = {}
    pairs: list[tuple[int, int]] = []
    candidates = 0
    for i in order:
        hashes = shapes[i][1]
        n = len(hashes)
        if not n:
            continue
        prefix = sorted(hashes, key=lambda h: (df[h], h))[: _prefix_len(n, threshold)]
        seen: set[int] = set()
        for h in prefix:
            for j in index.get(h, ()):
                seen.add(j)
        floor = threshold * n - 1e-9
        for j in sorted(seen):
            other = shapes[j][1]
            # Everything indexed so far is no larger than this set; below
            # t*|x| it cannot reach the threshold whatever it shares.
            if len(other) < floor:
                continue
            if token_counts is not None:
                ta, tb = token_counts[i], token_counts[j]
                if min(ta, tb) < threshold * max(ta, tb):
                    continue
            candidates += 1
            inter = len(hashes & other)
            if inter and inter / (n + len(other) - inter) >= threshold - 1e-9:
                pairs.append((j, i))
        for h in prefix:
            index.setdefault(h, []).append(i)
    return pairs, candidates


def _jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


@dataclass
class Cluster:
    """One clone group. `members[0]` is the representative."""

    members: list[CorpusSymbol]
    callers: dict[int, int]
    tests: frozenset[int]
    rank: int = 0

    @property
    def representative(self) -> CorpusSymbol:
        return self.members[0]

    @property
    def duplicated_lines(self) -> int:
        return sum(_lines(m) for m in self.members)

    @property
    def lines_saved(self) -> int:
        return self.duplicated_lines - _lines(self.representative)


@dataclass
class CloneReport:
    clusters: list[Cluster]
    scanned: int
    filtered_out: dict[str, int]
    candidate_pairs: int
    nested_clusters_dropped: int
    totals: dict[str, int] = field(default_factory=dict)


def _lines(s: CorpusSymbol) -> int:
    return max(s.end_line - s.start_line + 1, 1)


def _file_key(s: CorpusSymbol) -> tuple[str, str]:
    return (s.project_root or "", s.file_path)


def _caller_counts(conn: sqlite3.Connection, symbol_ids: list[int]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for start in range(0, len(symbol_ids), _SQL_IN_CHUNK):
        chunk = symbol_ids[start : start + _SQL_IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT dst_symbol_id, COUNT(DISTINCT src_symbol_id) FROM symbol_edge "
            f"WHERE dst_symbol_id IN ({marks}) AND src_symbol_id != dst_symbol_id "
            f"  AND edge_type IN ('calls', 'invokes_route') "
            f"GROUP BY dst_symbol_id",
            chunk,
        ):
            counts[int(r[0])] = int(r[1])
    return counts


def representative_key(
    s: CorpusSymbol, callers: dict[int, int], tests: frozenset[int]
) -> tuple[Any, ...]:
    """Sort key; the smallest is the copy an agent should read.

    1. production before test — the copy in `tests/` is the one least likely
       to be what the question is about;
    2. most callers — the copy the rest of the code actually depends on, so
       reading it also explains the most call sites;
    3. shallowest, then shortest path — `utils/fmt.py` before
       `apps/billing/legacy/v2/fmt_helpers.py`;
    4. path, qualified name, line — a total order, so the choice is stable.
    """
    return (
        s.symbol_id in tests,
        -callers.get(s.symbol_id, 0),
        s.file_path.count("/"),
        len(s.file_path),
        s.project_root or "",
        s.file_path,
        s.qualified_name,
        s.start_line,
    )


# Paging through clusters must not redo the join for every page. Same
# generation key as the graph cache: a new index run that changed files
# invalidates, nothing else can (clustering reads only the database).
_REPORT_CACHE: dict[tuple[Any, ...], CloneReport] = {}
_REPORT_CACHE_MAX = 8


def find_clusters(
    conn: sqlite3.Connection,
    project_ids: tuple[int, ...],
    root: Path,
    *,
    min_size: int = 2,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
    include_tests: bool = False,
    is_test: Callable[[str], bool] = lambda _p: False,
    is_excluded: Callable[[str], bool] = lambda _p: False,
    path: str | None = None,
) -> CloneReport:
    """Cluster the indexed functions/methods and order them by lines saved."""
    key = (
        _db_path(conn),
        project_ids,
        tuple(_latest_run_id(conn, pid) for pid in project_ids),
        min_size,
        threshold,
        include_tests,
        path,
        is_test,
        is_excluded,
    )
    hit = _REPORT_CACHE.get(key)
    if hit is not None:
        return hit
    report = _find_clusters(
        conn,
        project_ids,
        root,
        min_size=min_size,
        threshold=threshold,
        include_tests=include_tests,
        is_test=is_test,
        is_excluded=is_excluded,
        path=path,
    )
    if len(_REPORT_CACHE) >= _REPORT_CACHE_MAX:
        _REPORT_CACHE.pop(next(iter(_REPORT_CACHE)), None)
    _REPORT_CACHE[key] = report
    return report


def _find_clusters(
    conn: sqlite3.Connection,
    project_ids: tuple[int, ...],
    root: Path,
    *,
    min_size: int = 2,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
    include_tests: bool = False,
    is_test: Callable[[str], bool] = lambda _p: False,
    is_excluded: Callable[[str], bool] = lambda _p: False,
    path: str | None = None,
) -> CloneReport:
    corpus = load_symbol_corpus(conn, project_ids, root)

    filtered = {"generated": 0, "tests": 0, "trivial": 0}
    tests: set[int] = set()
    kept: list[CorpusSymbol] = []
    for s in corpus:
        if is_excluded(s.file_path):
            filtered["generated"] += 1
            continue
        test = is_test(s.file_path)
        if test and not include_tests:
            filtered["tests"] += 1
            continue
        # Same floor `search_similar` uses for level 1, applied to level 0 too:
        # in a whole-repo sweep every getter and every two-line guard is
        # "structurally identical" to dozens of others, and none of them saves
        # a reader anything.
        if not s.fingerprint.eligible_for_level1 or not s.fingerprint.minhashes:
            filtered["trivial"] += 1
            continue
        if test:
            tests.add(s.symbol_id)
        kept.append(s)

    by_shape: dict[str, list[int]] = {}
    for i, s in enumerate(kept):
        by_shape.setdefault(s.fingerprint.structural_hash, []).append(i)

    uf = _UnionFind(len(kept))
    for idxs in by_shape.values():
        for i in idxs[1:]:
            uf.union(idxs[0], i)

    shape_keys = sorted(by_shape)
    shapes = [(k, frozenset(kept[by_shape[k][0]].fingerprint.minhashes)) for k in shape_keys]
    counts = [kept[by_shape[k][0]].fingerprint.token_count for k in shape_keys]
    pairs, candidates = similar_shape_pairs(shapes, threshold, counts)
    for a, b in pairs:
        uf.union(by_shape[shape_keys[a]][0], by_shape[shape_keys[b]][0])

    groups: dict[int, list[CorpusSymbol]] = {}
    for i, s in enumerate(kept):
        groups.setdefault(uf.find(i), []).append(s)
    raw = [g for g in (_drop_self_nested(g) for g in groups.values()) if len(g) >= max(min_size, 2)]

    raw, nested = _drop_nested(raw)

    callers = _caller_counts(conn, sorted(s.symbol_id for g in raw for s in g))
    frozen_tests = frozenset(tests)
    clusters: list[Cluster] = []
    for g in raw:
        g.sort(key=lambda s: representative_key(s, callers, frozen_tests))
        clusters.append(Cluster(members=g, callers=callers, tests=frozen_tests))

    if path:
        clusters = [c for c in clusters if any(_under(m.file_path, path) for m in c.members)]

    clusters.sort(
        key=lambda c: (
            -c.lines_saved,
            -len(c.members),
            c.representative.project_root or "",
            c.representative.file_path,
            c.representative.qualified_name,
            c.representative.start_line,
        )
    )
    for n, c in enumerate(clusters, start=1):
        c.rank = n

    return CloneReport(
        clusters=clusters,
        scanned=len(corpus),
        filtered_out=filtered,
        candidate_pairs=candidates,
        nested_clusters_dropped=nested,
        totals={
            "clusters": len(clusters),
            "members": sum(len(c.members) for c in clusters),
            "duplicated_lines": sum(c.duplicated_lines for c in clusters),
            "lines_saved_if_read_once": sum(c.lines_saved for c in clusters),
        },
    )


def _under(file_path: str, path: str) -> bool:
    p = path.replace("\\", "/").lstrip("./").rstrip("/")
    if not p:
        return True
    return file_path == p or file_path.startswith(p + "/")


def _drop_self_nested(group: list[CorpusSymbol]) -> list[CorpusSymbol]:
    """A wrapper and the closure that is 95% of it are one copy, not two."""
    if len(group) < 2:
        return group
    out = []
    for s in group:
        inside = any(
            o.symbol_id != s.symbol_id
            and _file_key(o) == _file_key(s)
            and o.start_line <= s.start_line
            and o.end_line >= s.end_line
            and (o.end_line - o.start_line) > (s.end_line - s.start_line)
            for o in group
        )
        if not inside:
            out.append(s)
    return out


def _drop_nested(groups: list[list[CorpusSymbol]]) -> tuple[list[list[CorpusSymbol]], int]:
    """Drop groups that are entirely the inside of another group's members."""
    spans: dict[tuple[str, str], list[tuple[int, int, int, int]]] = {}
    for gi, g in enumerate(groups):
        for s in g:
            spans.setdefault(_file_key(s), []).append((s.start_line, s.end_line, s.symbol_id, gi))

    kept: list[list[CorpusSymbol]] = []
    dropped = 0
    for gi, g in enumerate(groups):
        enclosing: set[int] | None = None
        for s in g:
            around = {
                other_gi
                for start, end, sid, other_gi in spans[_file_key(s)]
                if other_gi != gi
                and sid != s.symbol_id
                and start <= s.start_line
                and end >= s.end_line
                and (end - start) > (s.end_line - s.start_line)
            }
            enclosing = around if enclosing is None else enclosing & around
            if not enclosing:
                break
        if enclosing:
            dropped += 1
        else:
            kept.append(g)
    return kept, dropped


# ---------------------------------------------------------------------------
# what differs from the representative
# ---------------------------------------------------------------------------


class _Sources:
    """One read per file for the clusters actually being rendered."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: dict[tuple[str, str], list[str]] = {}

    def body(self, s: CorpusSymbol) -> list[str]:
        key = _file_key(s)
        if key not in self._files:
            base = Path(s.project_root) if s.project_root else self.root
            try:
                self._files[key] = (
                    (base / s.file_path).read_text(encoding="utf-8", errors="replace").splitlines()
                )
            except OSError:
                self._files[key] = []
        lines = self._files[key]
        return lines[max(s.start_line - 1, 0) : min(s.end_line, len(lines))]


def _norm(lines: list[str]) -> list[str]:
    return [ln.strip() for ln in lines]


_WORD = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|[0-9][0-9A-Za-z_.]*|\"[^\"\n]*\"|'[^'\n]*'|[^\sA-Za-z0-9_]"
)
_SKIP_TOKENS = frozenset(
    {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}
)


def _lex(lines: list[str], python: bool) -> list[str]:
    text = "\n".join(lines)
    if python:
        pad = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
        dedented = "\n".join(ln[pad:] for ln in lines) + "\n"
        try:
            return [
                t.string
                for t in tokenize.generate_tokens(io.StringIO(dedented).readline)
                if t.type not in _SKIP_TOKENS and t.string
            ]
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
    return _WORD.findall(text)


def substitutions(
    rep: list[str], member: list[str], python: bool
) -> tuple[list[list[str]], int] | None:
    """What was renamed between two bodies of identical structure.

    Returns ``(pairs, omitted)`` — the distinct ``[representative, member]``
    token pairs that differ, in order of first appearance — or None when the
    two bodies do not line up token for token (a comment or a docstring of a
    different length), in which case the caller falls back to a line diff.
    """
    a, b = _lex(rep, python), _lex(member, python)
    if len(a) != len(b):
        return None
    seen: dict[tuple[str, str], None] = {}
    for x, y in zip(a, b, strict=True):
        if x != y:
            seen.setdefault((x, y), None)
    pairs = [_clip_pair(x, y) for x, y in seen]
    return pairs[:MAX_SUBSTITUTIONS], max(len(pairs) - MAX_SUBSTITUTIONS, 0)


def _clip_pair(x: str, y: str) -> list[str]:
    """Two differing tokens, cut down to the part that differs.

    A 30-line SQL string that changes one word would otherwise clip to the
    same 40 characters on both sides and say nothing.
    """
    width = MAX_SUBSTITUTION_WIDTH
    if len(x) <= width and len(y) <= width:
        return [x, y]
    shortest = min(len(x), len(y))
    pre = 0
    while pre < shortest and x[pre] == y[pre]:
        pre += 1
    suf = 0
    while suf < shortest - pre and x[-1 - suf] == y[-1 - suf]:
        suf += 1
    lead = max(pre - 8, 0)
    tail = max(suf - 8, 0)

    def cut(t: str) -> str:
        core = t[lead : len(t) - tail] if tail else t[lead:]
        core = " ".join(core.split())
        clipped = _clip(core, width)
        closing = "…" if tail and not clipped.endswith("…") else ""
        return ("…" if lead else "") + clipped + closing

    return [cut(x), cut(y)]


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def line_diff(
    rep: CorpusSymbol, rep_lines: list[str], member: CorpusSymbol, member_lines: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Counts plus a compact unified-style excerpt, representative -> member.

    Lines are compared with indentation stripped, so the same body pasted as a
    method instead of a function does not show up as a wall of changes. Hunk
    headers carry absolute file lines on both sides.
    """
    a, b = _norm(rep_lines), _norm(member_lines)
    counts = {"added": 0, "removed": 0, "changed": 0}
    out: list[str] = []
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed, added = i2 - i1, j2 - j1
        changed = min(removed, added)
        counts["changed"] += changed
        counts["removed"] += removed - changed
        counts["added"] += added - changed
        out.append(f"@@ -{rep.start_line + i1},{removed} +{member.start_line + j1},{added} @@")
        out.extend("-" + _clip(ln, MAX_DIFF_LINE_WIDTH) for ln in a[i1:i2])
        out.extend("+" + _clip(ln, MAX_DIFF_LINE_WIDTH) for ln in b[j1:j2])
    return counts, out


def _ref(s: CorpusSymbol) -> dict[str, Any]:
    out: dict[str, Any] = {
        "qualified_name": s.qualified_name,
        "file_path": s.file_path,
        "start_line": s.start_line,
        "end_line": s.end_line,
    }
    if s.project_root:
        out["project_root"] = s.project_root
    return out


def render_cluster(cluster: Cluster, root: Path, sources: _Sources | None = None) -> dict[str, Any]:
    """The payload for one cluster: the representative and what differs.

    Never a member's body. A member is `identical` (same structure; `renamed`
    lists the identifier/literal substitutions when the text is not the same
    byte for byte) or `similar` with change counts and a capped diff excerpt.
    A member with the same shape as one already described points at it with
    `same_as` instead of repeating the diff.
    """
    src = sources or _Sources(root)
    rep = cluster.representative
    rep_lines = src.body(rep)
    rep_set = frozenset(rep.fingerprint.minhashes)
    python = rep.file_path.endswith((".py", ".pyi"))

    others = cluster.members[1:]

    def _sim(m: CorpusSymbol) -> float:
        if m.fingerprint.structural_hash == rep.fingerprint.structural_hash:
            return 1.0
        return _jaccard(rep_set, frozenset(m.fingerprint.minhashes))

    ranked = sorted(
        others,
        key=lambda m: (
            m.fingerprint.structural_hash != rep.fingerprint.structural_hash,
            -round(_sim(m), 6),
            representative_key(m, cluster.callers, cluster.tests),
        ),
    )

    described: dict[str, str] = {}
    budget = MAX_DIFF_LINES_PER_CLUSTER
    listed: list[dict[str, Any]] = []
    for m in ranked[:MAX_OTHERS_LISTED]:
        entry = _ref(m)
        if _file_key(m) == _file_key(rep):
            # Same file as the representative: the line span is the address.
            del entry["file_path"]
            entry.pop("project_root", None)
        shape = m.fingerprint.structural_hash
        level0 = shape == rep.fingerprint.structural_hash
        m_lines = src.body(m)
        if level0:
            entry["relation"] = "identical"
            if _norm(m_lines) != _norm(rep_lines):
                subs = substitutions(rep_lines, m_lines, python)
                if subs is not None and subs[0]:
                    entry["renamed"] = subs[0]
                    if subs[1]:
                        entry["renamed_omitted"] = subs[1]
                elif subs is None:
                    budget = _attach_diff(entry, rep, rep_lines, m, m_lines, budget)
        else:
            entry["relation"] = "similar"
            entry["similarity"] = round(_sim(m), 3)
            if shape in described:
                entry["same_as"] = described[shape]
            else:
                described[shape] = m.qualified_name
                budget = _attach_diff(entry, rep, rep_lines, m, m_lines, budget)
        listed.append(entry)

    out: dict[str, Any] = {
        "cluster": cluster.rank,
        "members": len(cluster.members),
        "duplicated_lines": cluster.duplicated_lines,
        "lines_saved": cluster.lines_saved,
        "representative": {
            **_ref(rep),
            "lines": _lines(rep),
            "callers": cluster.callers.get(rep.symbol_id, 0),
        },
        "others": listed,
    }
    if len(ranked) > len(listed):
        out["others_omitted"] = len(ranked) - len(listed)
    return out


def _attach_diff(
    entry: dict[str, Any],
    rep: CorpusSymbol,
    rep_lines: list[str],
    member: CorpusSymbol,
    member_lines: list[str],
    budget: int,
) -> int:
    counts, diff = line_diff(rep, rep_lines, member, member_lines)
    entry["lines"] = counts
    take = min(len(diff), MAX_DIFF_LINES_PER_MEMBER, max(budget, 0))
    if take:
        entry["diff"] = diff[:take]
    if take < len(diff):
        entry["diff_truncated"] = len(diff) - take
    return budget - take


def render_page(clusters: list[Cluster], root: Path) -> list[dict[str, Any]]:
    sources = _Sources(root)
    return [render_cluster(c, root, sources) for c in clusters]


# ---------------------------------------------------------------------------
# "you are looking at a clone" — for the tools that open one symbol
# ---------------------------------------------------------------------------


def identical_copies(
    conn: sqlite3.Connection,
    project_ids: tuple[int, ...],
    symbol_id: int,
    body_hash: str | None,
    *,
    limit: int = 3,
) -> dict[str, Any] | None:
    """Other symbols with this exact structure, from the fingerprint cache.

    Level 0 only, and only from what is already cached — two indexed lookups,
    no file read, no fingerprinting. Before `find_clone_clusters` or
    `search_similar` has warmed the cache this returns None; it never makes a
    one-symbol read pay for a repo-wide pass.
    """
    if not body_hash or not project_ids:
        return None
    own = conn.execute(
        "SELECT structural_hash, token_count FROM symbol_fingerprint WHERE body_hash = ?",
        (body_hash,),
    ).fetchone()
    if own is None or int(own[1]) < MIN_TOKENS_FOR_LEVEL1:
        return None
    marks = ",".join("?" * len(project_ids))
    rows = conn.execute(
        f"SELECT s.qualified_name, f.path, s.start_line "
        f"FROM symbol s JOIN file f ON f.id = s.file_id "
        f"JOIN symbol_fingerprint fp ON fp.body_hash = s.body_hash "
        f"WHERE fp.structural_hash = ? AND s.id != ? "
        f"  AND s.kind IN ('function', 'method') AND f.project_id IN ({marks}) "
        f"ORDER BY f.path, s.start_line, s.qualified_name",
        (own[0], symbol_id, *project_ids),
    ).fetchall()
    if not rows:
        return None
    return {
        "identical_copies": len(rows),
        "also_at": [f"{r[0]} ({r[1]}:{r[2]})" for r in rows[:limit]],
        "note": (
            "same structure elsewhere (only names/literals differ) — no need to "
            "read the copies; find_clone_clusters shows what differs"
        ),
    }
