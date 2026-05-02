"""NetworkX graph loader and impact/topology helpers."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable

import networkx as nx


@dataclass
class GraphView:
    g: nx.DiGraph
    sym_meta: dict[int, dict]  # symbol_id -> {name, qname, kind, file_path, lines}


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
    row = conn.execute(
        "SELECT id FROM index_run WHERE project_id=? ORDER BY id DESC LIMIT 1",
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
        """SELECT e.src_symbol_id, e.dst_symbol_id, e.edge_type, e.weight
           FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?""",
        (project_id,),
    ):
        g.add_edge(int(r["src_symbol_id"]), int(r["dst_symbol_id"]),
                   edge_type=r["edge_type"], weight=float(r["weight"]))

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


def descendants_within(
    g: nx.DiGraph,
    source: int,
    max_depth: int,
    min_weight: float = 0.0,
) -> set[int]:
    """BFS up to max_depth, collect descendants (forward slicing).

    v0.9 P3: ``min_weight`` skips edges below the threshold. Resolver
    fan-out (multiple short-name candidates that the static analyzer
    can't disambiguate) lands at weight 0.5; pass ``min_weight=0.6`` to
    drop that noise from the traversal. Default 0.0 keeps the legacy
    behavior (every edge counted).
    """
    seen: set[int] = set()
    frontier: list[tuple[int, int]] = [(source, 0)]
    while frontier:
        node, d = frontier.pop()
        if d >= max_depth:
            continue
        for succ in g.successors(node):
            if succ in seen or succ == source:
                continue
            if min_weight > 0.0:
                ed = g.get_edge_data(node, succ) or {}
                if float(ed.get("weight", 1.0)) < min_weight:
                    continue
            seen.add(succ)
            frontier.append((succ, d + 1))
    return seen


def ancestors_within(
    g: nx.DiGraph,
    source: int,
    max_depth: int,
    min_weight: float = 0.0,
) -> set[int]:
    return descendants_within(g.reverse(copy=False), source, max_depth, min_weight)


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


def subgraph_edges(view: GraphView, nodes: Iterable[int]) -> list[dict]:
    nset = set(nodes)
    out = []
    for u, v, data in view.g.edges(data=True):
        if u in nset and v in nset:
            out.append({
                "src": view.sym_meta[u]["qualified_name"],
                "dst": view.sym_meta[v]["qualified_name"],
                "edge_type": data.get("edge_type", "calls"),
                "weight": data.get("weight", 1.0),
            })
    return out
