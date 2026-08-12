"""Read an external code graph (Graphify `graph.json`) as corroborating evidence.

livespec's own extractors are the source of truth. This module never adds
symbols, never writes edges, and never feeds the call graph. It answers exactly
one question, on demand: *does another deterministic extractor believe this
symbol is reachable?*

Why bother. `find_dead_code` is livespec's least trustworthy output, and the
untrustworthiness is not uniform — it tracks what the extractor cannot see.
Measured on a real TypeScript composer (2026-08-11): of 46 dead candidates, 21
had inbound edges in a Graphify graph of the same tree. Three were checked by
hand and all three were genuine livespec misses:

- a cross-file call the resolver lost (`assertMaxContentLength`, imported *and*
  called one file over)
- a base class kept alive only by `class X extends Base` — livespec has no
  inheritance edge, so a base with no direct callers reads as dead
- an interface used solely as a type annotation — livespec does not track
  type-position usage

Those are livespec's blind spots, not Graphify's cleverness, which is the point:
a second extractor with different blind spots is worth more as a *filter* than
as a source. Graphify's code pass is tree-sitter with no LLM (`_origin: "ast"`),
so consuming it costs nothing in determinism.

Format. Graphify writes NetworkX node-link JSON:

    {"directed": true, "multigraph": false, "graph": {},
     "nodes": [{"id", "label", "source_file", "source_location": "L53",
                "community", "_callable", "_origin", ...}],
     "links": [{"source", "target", "relation": "calls", "confidence":
                "EXTRACTED"|"INFERRED", "confidence_score", "_origin", ...}]}

The reader is deliberately tolerant: unknown keys are ignored, missing optional
keys are tolerated, and anything unparseable is skipped rather than raised. An
external file must never be able to break an index.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Relations that describe graph structure or prose attachment rather than one
# symbol depending on another. An inbound `contains` edge only says "this lives
# in that file", which every symbol has and which proves nothing about use.
#
# `method` belongs here for the same reason, and it took a 14-repo sweep to
# notice: it is emitted as `Class -> .method()`, always within one file, from
# the declaring class. Every method has one. Counting it as evidence quietly
# made every method of every class un-killable and was responsible for 98 of
# 223 dead-code rescues across that sweep — nearly half the measured effect was
# an artifact of this line.
STRUCTURAL_RELATIONS = frozenset({"contains", "rationale_for", "method"})

# Relations accepted as evidence that a symbol is reachable. `imports` and
# `imports_from` are weaker than `calls` (importing a name is not calling it)
# but still contradict "nothing in this repo refers to it", which is the claim
# `find_dead_code` actually makes.
EVIDENCE_RELATIONS = frozenset(
    {
        "calls",
        "indirect_call",
        "inherits",
        "mixes_in",
        "uses",
        "references",
        "imports",
        "imports_from",
        "re_exports",
    }
)


@dataclass(frozen=True)
class ExternalNode:
    node_id: str
    label: str
    source_file: str | None
    line: int | None
    community: int | None
    origin: str | None


@dataclass
class ExternalGraph:
    """Parsed external graph, indexed for lookup by position and by name."""

    path: str
    node_count: int = 0
    edge_count: int = 0
    directed: bool = False
    #: Every node id that carries a usable (file, line) position.
    by_position: dict[tuple[str, int], ExternalNode] = field(default_factory=dict)
    #: file -> lowercased bare label -> nodes (fallback when lines drift).
    by_file_name: dict[tuple[str, str], list[ExternalNode]] = field(
        default_factory=dict
    )
    #: node id -> inbound relations, structural ones already dropped.
    inbound: dict[str, list[str]] = field(default_factory=dict)
    #: node id -> (relation, target node id), structural ones already dropped.
    #: Needed for the opposite question from dead code: not "does anything
    #: refer to this?" but "does this reach anything?" — which is what an
    #: orphan test is really about.
    outbound: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: node id -> node, for resolving outbound targets.
    by_id: dict[str, ExternalNode] = field(default_factory=dict)
    #: Relation histogram over all links, for reporting.
    relation_counts: dict[str, int] = field(default_factory=dict)
    #: True when any link carries an origin other than deterministic AST.
    has_non_ast_origin: bool = False
    #: Share of this graph's files that the consuming index also has. Set by
    #: the caller's sanity gate; 0.0 until then.
    file_overlap: float = 0.0

    def lookup(self, file_path: str, line: int, name: str) -> ExternalNode | None:
        """Position first, then bare name within the same file.

        Position is exact and unambiguous when it matches. The name fallback
        exists because a decorated symbol can be recorded at the decorator line
        by one extractor and the `def` line by the other; it stays scoped to a
        single file so unrelated same-named symbols cannot collide.
        """
        hit = self.by_position.get((file_path, line))
        if hit is not None:
            return hit
        candidates = self.by_file_name.get((file_path, _bare(name)), [])
        return candidates[0] if len(candidates) == 1 else None

    def evidence_for(self, node: ExternalNode) -> list[str]:
        """Relations pointing *at* this node that suggest it is reachable."""
        return [r for r in self.inbound.get(node.node_id, []) if r in EVIDENCE_RELATIONS]

    def reaches(
        self, node: ExternalNode, predicate: Callable[[ExternalNode], bool]
    ) -> list[tuple[str, ExternalNode]]:
        """Outbound edges from this node landing on a node matching `predicate`.

        Depth 1 on purpose. A test that reaches production through five hops of
        test helpers is still a test that reaches production, but the deeper the
        walk the more a single bad external edge can vouch for anything. One hop
        keeps each rescue individually checkable.
        """
        out: list[tuple[str, ExternalNode]] = []
        for relation, target_id in self.outbound.get(node.node_id, []):
            if relation not in EVIDENCE_RELATIONS:
                continue
            target = self.by_id.get(target_id)
            if target is not None and predicate(target):
                out.append((relation, target))
        return out


def _bare(name: str) -> str:
    """`foo()` and `Foo` normalise to the same key as a livespec symbol name."""
    return name.rstrip("()").rsplit(".", 1)[-1].strip().lower()


def _parse_line(location: Any) -> int | None:
    """`"L53"` / `"L53-L60"` -> 53. Anything else -> None."""
    if not isinstance(location, str) or not location.startswith("L"):
        return None
    head = location[1:].split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def load_external_graph(path: str | Path) -> ExternalGraph:
    """Parse a Graphify-style node-link graph.

    Raises ``FileNotFoundError`` if the path does not exist and ``ValueError``
    if the file is not JSON or carries no recognisable nodes — callers turn both
    into a shaped ``mcp_error``. Individual malformed nodes/links are skipped
    silently: one bad row must not cost the caller the whole file.
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a JSON object at the top level")

    nodes = data.get("nodes")
    links = data.get("links") or data.get("edges") or []
    if not isinstance(nodes, list) or not nodes:
        raise ValueError(
            f"{p}: no 'nodes' array — is this a Graphify graph.json? "
            "(expected NetworkX node-link JSON)"
        )

    graph = ExternalGraph(path=str(p), directed=bool(data.get("directed")))
    ids: dict[str, ExternalNode] = {}

    for entry in nodes:
        if not isinstance(entry, dict):
            continue
        node_id = entry.get("id")
        if not isinstance(node_id, str):
            continue
        label = entry.get("label")
        label = label if isinstance(label, str) else ""
        source_file = entry.get("source_file")
        source_file = source_file if isinstance(source_file, str) else None
        community = entry.get("community")
        community = community if isinstance(community, int) else None
        node = ExternalNode(
            node_id=node_id,
            label=label,
            source_file=source_file,
            line=_parse_line(entry.get("source_location")),
            community=community,
            origin=entry.get("_origin") if isinstance(entry.get("_origin"), str) else None,
        )
        ids[node_id] = node
        graph.by_id[node_id] = node
        graph.node_count += 1
        if source_file and node.line is not None:
            graph.by_position.setdefault((source_file, node.line), node)
        if source_file and label:
            graph.by_file_name.setdefault((source_file, _bare(label)), []).append(node)

    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        target = link.get("target")
        source = link.get("source")
        relation = link.get("relation") or link.get("type") or link.get("rel")
        if not isinstance(target, str) or not isinstance(relation, str):
            continue
        graph.edge_count += 1
        graph.relation_counts[relation] = graph.relation_counts.get(relation, 0) + 1
        origin = link.get("_origin")
        if isinstance(origin, str) and origin != "ast":
            graph.has_non_ast_origin = True
        if relation in STRUCTURAL_RELATIONS:
            continue
        graph.inbound.setdefault(target, []).append(relation)
        if isinstance(source, str):
            graph.outbound.setdefault(source, []).append((relation, target))

    return graph


def overlap_ratio(graph: ExternalGraph, indexed_files: set[str]) -> float:
    """Share of the external graph's files that livespec also indexed.

    A near-zero overlap means the graph describes a different tree (or uses
    absolute paths against a relative index), and corroborating against it would
    silently vouch for nothing. Callers refuse rather than report a clean sweep.
    """
    external_files = {
        node.source_file for node in graph.by_position.values() if node.source_file
    }
    if not external_files:
        return 0.0
    return len(external_files & indexed_files) / len(external_files)
