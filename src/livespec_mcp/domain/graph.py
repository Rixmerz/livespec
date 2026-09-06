"""NetworkX graph loader and impact/topology helpers."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

import networkx as nx


@dataclass
class GraphView:
    g: nx.DiGraph
    sym_meta: dict[int, dict]  # symbol_id -> {name, qname, kind, file_path, lines}
    # Lazily-computed, cached unpersonalized PageRank. PageRank is a pure
    # function of the graph (measured 5.4s on Django's 465K-edge graph) and
    # was recomputed on every quick_orient / get_project_overview /
    # propose_specs call. Cached here so it is computed at most once per
    # GraphView (which is itself cached per finished index run).
    _pagerank: dict[int, float] | None = None


def graph_pagerank(view: GraphView) -> dict[int, float]:
    """@spec:graph-call-graph-pagerank

    Cached unpersonalized PageRank for a GraphView.
    """
    if view._pagerank is None:
        view._pagerank = page_rank(view.g)
    return view._pagerank


# v0.6 P3: graph cache. Building the NetworkX object from SQL costs ~4s on a
# 40K-symbol repo and is repeated on every analysis tool call. Cache by
# (db_path, project_id, last_index_run_id) — invalidated automatically when
# a new index run completes (since the latest id changes). DB path is part
# of the key so isolated test workspaces with the same project_id don't
# collide. Module-level so a single MCP server instance shares the cache
# across workspaces.
_GRAPH_CACHE: dict[tuple[str, int, int], GraphView] = {}
_GRAPH_CACHE_LOCK = threading.Lock()
_GRAPH_CACHE_MAX = 8  # one per active workspace; LRU-ish via insertion order


def _latest_run_id(conn: sqlite3.Connection, project_id: int) -> int:
    # Cache generation = the latest FINISHED run that actually changed files.
    # Two reasons for both filters:
    #  - finished_at IS NOT NULL: the indexer inserts the run row at the START
    #    (before symbol writes), so keying on the raw max id would let a load
    #    during an in-flight index cache a half-built graph under the key that
    #    stays current after the run finishes.
    #  - files_changed > 0: a no-op reindex (every watcher tick on a quiet repo
    #    inserts a finished run with files_changed=0) must not invalidate the
    #    ~4s / 183MB graph when the symbol/edge tables did not move.
    row = conn.execute(
        """SELECT id FROM index_run
           WHERE project_id=? AND finished_at IS NOT NULL AND files_changed > 0
           ORDER BY id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    return int(row["id"]) if row else 0


def _db_path(conn: sqlite3.Connection) -> str:
    """Best-effort identifier for the SQLite DB this conn points to."""
    try:
        for r in conn.execute("PRAGMA database_list"):
            if r[1] == "main":
                return r[2] or f"conn:{id(conn)}"
    except sqlite3.Error:
        pass
    return f"conn:{id(conn)}"


def load_graph(conn: sqlite3.Connection, project_id: int) -> GraphView:
    """Load (or fetch from cache) the call graph for a project.

    Cache key: (db_path, project_id, latest_index_run_id). A new index run
    bumps the id and invalidates automatically. Misses fall through to the
    SQL rebuild path."""
    run_id = _latest_run_id(conn, project_id)
    key = (_db_path(conn), project_id, run_id)
    with _GRAPH_CACHE_LOCK:
        cached = _GRAPH_CACHE.get(key)
        if cached is not None:
            return cached

    g = nx.DiGraph()
    sym_meta: dict[int, dict] = {}
    for r in conn.execute(
        """SELECT s.id, s.name, s.qualified_name, s.kind, s.start_line, s.end_line, f.path
           FROM symbol s JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?""",
        (project_id,),
    ):
        sid = int(r["id"])
        sym_meta[sid] = {
            "id": sid,
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "file_path": r["path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
        }
        g.add_node(sid)
    for r in conn.execute(
        """SELECT e.src_symbol_id, e.dst_symbol_id, e.edge_type, e.weight, e.origin
           FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?""",
        (project_id,),
    ):
        # `origin` (v0.33) rides along so a caller can tell whose claim an edge
        # is. Carried as an attribute rather than filtered here: an ingested
        # edge is an ordinary edge for traversal, and the tools that report it
        # are the ones that should say where it came from.
        g.add_edge(
            int(r["src_symbol_id"]),
            int(r["dst_symbol_id"]),
            edge_type=r["edge_type"],
            weight=float(r["weight"]),
            origin=r["origin"] or "livespec",
        )

    view = GraphView(g=g, sym_meta=sym_meta)
    with _GRAPH_CACHE_LOCK:
        # Drop stale entries for THIS (db, project) at older run_ids; apply
        # a coarse size cap.
        for k in list(_GRAPH_CACHE.keys()):
            if k[0] == key[0] and k[1] == project_id and k != key:
                _GRAPH_CACHE.pop(k, None)
        if len(_GRAPH_CACHE) >= _GRAPH_CACHE_MAX:
            _GRAPH_CACHE.pop(next(iter(_GRAPH_CACHE)), None)
        _GRAPH_CACHE[key] = view
    return view


def invalidate_graph_cache(project_id: int | None = None) -> int:
    """Drop cached graphs for one project (or all). Returns dropped count.

    project_id None drops the entire cache (across all workspaces). A
    specific id drops every entry matching that id regardless of db_path —
    use this when you know the project changed; tests that need full
    isolation should pass None.
    """
    with _GRAPH_CACHE_LOCK:
        if project_id is None:
            n = len(_GRAPH_CACHE)
            _GRAPH_CACHE.clear()
            return n
        keys = [k for k in _GRAPH_CACHE if k[1] == project_id]
        for k in keys:
            _GRAPH_CACHE.pop(k, None)
        return len(keys)


#: Edge types that mean "this symbol invokes that one" — the claim `who_calls`
#: and `who_does_this_call` actually make. livespec's own extraction only ever
#: writes these two (`_resolve_refs` -> `calls`, route joining ->
#: `invokes_route`), so filtering a traversal by them is a no-op on any index
#: nobody has ingested into, and filters exactly the rows that came from
#: somewhere else.
#:
#: This exists because ingestion (v0.33) put `references` and `inherits` rows
#: into the same table. Those are real dependencies and belong in
#: `analyze_impact` — a type annotation DOES break when you change the type —
#: but calling them "callers" is false. Measured on this repo: `who_calls`
#: for one class went from 2 to 40 after an ingest, and 38 of the 40 were
#: methods taking it as a type-position parameter. An agent reading "40
#: callers" has no way to tell which two actually call it.
INVOCATION_EDGE_TYPES: frozenset[str] = frozenset({"calls", "invokes_route"})


def descendants_within(
    g: nx.DiGraph,
    source: int,
    max_depth: int,
    min_weight: float = 0.0,
    edge_types: frozenset[str] | set[str] | None = None,
) -> set[int]:
    """BFS up to max_depth, collect descendants (forward slicing).

    v0.9 P3: ``min_weight`` skips edges below the threshold. Resolver
    fan-out (multiple short-name candidates that the static analyzer
    can't disambiguate) lands at weight 0.5; pass ``min_weight=0.6`` to
    drop that noise from the traversal. Default 0.0 keeps the legacy
    behavior (every edge counted).

    Unreleased: ``edge_types`` restricts the walk to edges of those types.
    ``None`` (the default) walks every edge, so every existing caller of this
    function is unaffected. Filtering happens during the walk, not after it: a
    node reachable only *through* an excluded edge is not a caller either, and
    counting it would reintroduce the same lie one hop further out.
    """
    # True BFS (FIFO) so every node is first reached by its SHORTEST path.
    # A LIFO frontier (DFS) with a global `seen` set marked at enqueue time
    # would record a node at whatever depth discovered it first — if that was
    # a longer path at/near max_depth, its within-budget descendants reached
    # via a shorter path were never expanded, silently under-reporting the
    # blast radius of who_calls / analyze_impact / coverage.
    from collections import deque

    seen: set[int] = set()
    frontier: deque[tuple[int, int]] = deque([(source, 0)])
    while frontier:
        node, d = frontier.popleft()
        if d >= max_depth:
            continue
        for succ in g.successors(node):
            if succ in seen or succ == source:
                continue
            if min_weight > 0.0 or edge_types is not None:
                ed = g.get_edge_data(node, succ) or {}
                if min_weight > 0.0 and float(ed.get("weight", 1.0)) < min_weight:
                    continue
                if edge_types is not None and ed.get("edge_type") not in edge_types:
                    continue
            seen.add(succ)
            frontier.append((succ, d + 1))
    return seen


def ancestors_within(
    g: nx.DiGraph,
    source: int,
    max_depth: int,
    min_weight: float = 0.0,
    edge_types: frozenset[str] | set[str] | None = None,
) -> set[int]:
    return descendants_within(g.reverse(copy=False), source, max_depth, min_weight, edge_types)


def page_rank(g: nx.DiGraph, personalization: dict[int, float] | None = None) -> dict[int, float]:
    if g.number_of_nodes() == 0:
        return {}
    try:
        return nx.pagerank(g, alpha=0.85, personalization=personalization)
    except (ImportError, ModuleNotFoundError):
        # scipy missing — fall back to a pure-Python power iteration
        return _pagerank_pure(g, alpha=0.85, personalization=personalization)


def _pagerank_pure(
    g: nx.DiGraph,
    alpha: float = 0.85,
    personalization: dict[int, float] | None = None,
    max_iter: int = 50,
    tol: float = 1e-6,
) -> dict[int, float]:
    nodes = list(g.nodes())
    n = len(nodes)
    if n == 0:
        return {}
    if personalization:
        s = sum(personalization.values()) or 1.0
        p = {k: personalization.get(k, 0.0) / s for k in nodes}
    else:
        p = {k: 1.0 / n for k in nodes}
    rank = dict(p)
    for _ in range(max_iter):
        new = {k: (1 - alpha) * p[k] for k in nodes}
        leaked = 0.0
        for u in nodes:
            out_deg = g.out_degree(u)
            if out_deg == 0:
                leaked += rank[u]
                continue
            share = alpha * rank[u] / out_deg
            for v in g.successors(u):
                new[v] += share
        # Distribute leaked rank
        for k in nodes:
            new[k] += alpha * leaked * p[k]
        diff = sum(abs(new[k] - rank[k]) for k in nodes)
        rank = new
        if diff < tol:
            break
    return rank


def _has_any_external_edge(conn: sqlite3.Connection) -> bool:
    """Cheap existence probe: does this DB hold a non-livespec edge at all?

    Two range scans on `idx_edge_origin`, each O(log n), instead of the
    three-table join below. `origin <> 'livespec'` cannot use that index — an
    inequality on a single value is not a range — so it degrades to a full scan
    of `symbol_edge`, which on Django (465K edges) is real time paid by every
    read tool on every call, on an index nobody has ever ingested into.

    Splitting it into the two ranges around the constant makes it a b-tree
    seek. Unscoped by project on purpose: the summary below is what needs to be
    exact per project, and this only decides whether asking is worth it.
    """
    for op in ("<", ">"):
        row = conn.execute(
            f"SELECT 1 FROM symbol_edge WHERE origin {op} 'livespec' LIMIT 1"
        ).fetchone()
        if row is not None:
            return True
    return False


def external_edge_summary(conn: sqlite3.Connection, project_id: int) -> dict[str, int] | None:
    """Ingested-edge counts by origin for a project, or None when there are none.

    Read tools call this to say, in their own payload, that part of the answer
    came from a second extractor. An agent that cannot tell an ingested edge
    from an extracted one cannot calibrate what it is reading.

    Costs an index seek on every index nobody has ingested into — which is
    every default install — and only pays for the exact per-project count on
    one that has, where the caller opted into it.
    """
    if not _has_any_external_edge(conn):
        return None
    rows = conn.execute(
        """SELECT e.origin, COUNT(*) AS c
           FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ? AND e.origin <> 'livespec'
           GROUP BY e.origin""",
        (project_id,),
    ).fetchall()
    if not rows:
        return None
    return {r["origin"]: int(r["c"]) for r in rows}
