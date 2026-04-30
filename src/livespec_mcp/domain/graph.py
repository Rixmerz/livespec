"""NetworkX graph loader and impact/topology helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

import networkx as nx


@dataclass
class GraphView:
    g: nx.DiGraph
    sym_meta: dict[int, dict]  # symbol_id -> {name, qname, kind, file_path, lines}


def load_graph(conn: sqlite3.Connection, project_id: int) -> GraphView:
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
    return GraphView(g=g, sym_meta=sym_meta)


def descendants_within(g: nx.DiGraph, source: int, max_depth: int) -> set[int]:
    """BFS up to max_depth, collect descendants (forward slicing)."""
    seen: set[int] = set()
    frontier: list[tuple[int, int]] = [(source, 0)]
    while frontier:
        node, d = frontier.pop()
        if d >= max_depth:
            continue
        for succ in g.successors(node):
            if succ not in seen and succ != source:
                seen.add(succ)
                frontier.append((succ, d + 1))
    return seen


def ancestors_within(g: nx.DiGraph, source: int, max_depth: int) -> set[int]:
    return descendants_within(g.reverse(copy=False), source, max_depth)


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
