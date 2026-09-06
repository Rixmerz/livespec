"""Ingest an external extractor's edges into livespec's own call graph.

`external_graph.py` reads a Graphify `graph.json` as *corroborating evidence*:
it can only remove candidates from `find_dead_code` / `find_orphan_tests`, and
it touches no table. That was deliberately the weakest possible form of
consumption, and it left the useful half on the floor — the questions an agent
actually asks (`who_calls`, `analyze_impact`, "what breaks if I change this?")
never saw a single external edge, because those read `symbol_edge` and
corroboration never writes there.

This module closes that gap under three boundaries, in descending order of how
much they matter:

1. **The symbol table stays ours, absolutely.** An edge is ingested only when
   *both* endpoints already resolve to symbols livespec extracted. An external
   node with no livespec counterpart is counted and dropped, never created.
   Symbols are what every tool enumerates; the moment a foreign file can add
   one, every count in the product becomes partly someone else's.
2. **Every ingested row is labelled and reversible.** Rows carry
   `symbol_edge.origin = 'external:graphify'` (migration 22). Re-ingesting
   deletes this origin's own rows first, so the state after an ingest depends
   only on the current graph and the current index — never on ingest history.
3. **Ingested edges are ordinary edges afterwards.** They are in the graph, so
   `who_calls` and `find_dead_code` improve without a flag. That is the point;
   a labelled edge nobody reads is worth nothing. The tools say so in their
   payloads instead of pretending the answer is purely ours.

Why not simply ingest everything the corroboration path accepts. Corroboration
answers "does *anything* refer to this?", so `imports` is legitimate evidence
there — and empirically its biggest single source (68 of the 133 drops in the
v0.32 sweep). But `who_calls` claims callers, and an import is not a call.
Ingesting import wiring by default would put "imported by" rows in every
backward cone in the product to buy a dead-code improvement that
`corroborate_with` already delivers without touching the database. So the
default set is the relations that genuinely mean *this symbol depends on that
one*, and `relations=` opens the rest for a caller who wants it and has been
told what it does.

That split is also why both features stay: ingestion makes the call graph
better, corroboration answers a broader question more cheaply. Neither
subsumes the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from livespec_mcp.domain.external_graph import ExternalGraph

#: Value written to `symbol_edge.origin` for every row this module inserts.
#: One fixed tag per external extractor, so removal is a single predicate a
#: user can run by hand and recognise in a payload.
EXTERNAL_ORIGIN = "external:graphify"

#: External relation -> livespec `edge_type`. The target vocabulary is the one
#: already documented in `schema.sql` (`calls | imports | inherits |
#: references`); nothing new is invented, so existing edge_type consumers keep
#: working.
#:
#: Kept explicit (not derived from `EVIDENCE_RELATIONS`) because ingestion
#: WRITES a row: an unrecognised relation must land in
#: `skipped.unknown_relation` and be reported, never be guessed onto an
#: edge_type. Corroboration can afford a broader, more forgiving set — it only
#: removes candidates.
RELATION_EDGE_TYPE: dict[str, str] = {
    # invocation
    "calls": "calls",
    "indirect_call": "calls",
    # constructing a type runs its constructor; livespec models `Foo()` as a
    # call, so this is the same claim in another extractor's spelling.
    "instantiates": "calls",
    # inheritance / type hierarchy — one class depending on another's shape.
    # The last four are per-language spellings Graphify 0.9.55 emits for Java
    # and C# interfaces, Go struct embedding and CommonLisp specialisation.
    "inherits": "inherits",
    "mixes_in": "inherits",
    "implements": "inherits",
    "extends": "inherits",
    "specializes": "inherits",
    "embeds": "inherits",
    # usage, including type position — the blind spot livespec does not model
    # at all and the single biggest reason to read a second extractor.
    "uses": "references",
    "references": "references",
    "accesses": "references",
    "reads_from": "references",
    "requires": "references",
    "depends_on": "references",
    "uses_static_prop": "references",
    "uses_component": "references",
    "references_constant": "references",
    "binds_method": "references",
    "bound_to": "references",
    # module wiring — off by default, see DEFAULT_RELATIONS
    "imports": "imports",
    "imports_from": "imports",
    "re_exports": "imports",
    "includes": "imports",
}

#: The module-wiring relations. Excluded from the default ingest set: they
#: describe which file pulls in which, they mostly land on Graphify's per-file
#: nodes (which never map to a livespec symbol anyway), and treating "imported
#: by" as "called by" would make `who_calls` lie in order to improve a
#: different tool.
IMPORT_RELATIONS: frozenset[str] = frozenset(
    {"imports", "imports_from", "re_exports", "includes"}
)

#: Ingested unless the caller asks otherwise. These are the relations that mean
#: "this symbol depends on that one" — exactly the claim a backward cone makes.
DEFAULT_RELATIONS: frozenset[str] = frozenset(RELATION_EDGE_TYPE) - IMPORT_RELATIONS

#: An external claim about our symbols never earns 1.0. That value means "our
#: resolver saw this call and disambiguated it", and a second extractor working
#: from its own parse has not earned the same standing. Floor is livespec's
#: ambiguous-fan-out weight for the same reason in reverse: an INFERRED edge
#: should be filtered by the same `min_weight` that filters our guesses.
_WEIGHT_MAX = 0.9
_WEIGHT_MIN = 0.5
_CONFIDENCE_WEIGHT = {"EXTRACTED": 0.9, "INFERRED": 0.6, "AMBIGUOUS": _WEIGHT_MIN}

#: Confidence labels that cap the weight no matter what `confidence_score`
#: says. `AMBIGUOUS` (Graphify 0.9.55) is the other tool stating outright that
#: it could not disambiguate the edge, which is exactly what livespec's own
#: 0.5 means — and 0.5 is the value `min_weight=0.6` filters. Letting a high
#: numeric score lift it above that would smuggle a guess past the filter
#: built to catch guesses.
_CONFIDENCE_CEILING = {"AMBIGUOUS": _WEIGHT_MIN}


@dataclass(frozen=True)
class PlannedEdge:
    src_symbol_id: int
    dst_symbol_id: int
    edge_type: str
    weight: float
    #: The external relation this came from, kept for reporting only — several
    #: relations collapse onto one edge_type.
    relation: str


@dataclass
class IngestPlan:
    """What ingesting this graph into this index would do. Computed, not applied."""

    origin: str = EXTERNAL_ORIGIN
    #: Edges livespec does not have, ready to insert.
    new_edges: list[PlannedEdge] = field(default_factory=list)
    #: External edges livespec already has. The cross-validation number — a low
    #: agreement rate means the two tools are describing different trees, not
    #: that one of them found more.
    agreed: int = 0
    by_relation: dict[str, int] = field(default_factory=dict)
    agreed_by_relation: dict[str, int] = field(default_factory=dict)
    #: Why external edges were not ingested. Counts only; the payload stays
    #: bounded on a monorepo.
    skipped: dict[str, int] = field(default_factory=dict)
    #: External nodes that matched a livespec symbol.
    mapped_nodes: int = 0
    #: External nodes two or more livespec symbols both claimed. Dropped rather
    #: than guessed — a wrong mapping writes a wrong edge into the call graph,
    #: which is worse than a missing one.
    ambiguous_nodes: int = 0

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    @property
    def edge_count(self) -> int:
        return len(self.new_edges)


def _weight_for(relation: str, confidence: object, score: object) -> float:
    """Map an external edge's confidence onto livespec's weight ladder.

    The numeric `confidence_score` refines, the `confidence` label bounds: a
    label saying the other extractor could not disambiguate wins over any score
    that disagrees with it (see `_CONFIDENCE_CEILING`).
    """
    label = confidence.upper() if isinstance(confidence, str) else ""
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        w = float(score)
    elif label:
        w = _CONFIDENCE_WEIGHT.get(label, _WEIGHT_MIN)
    else:
        w = _WEIGHT_MIN
    w = max(_WEIGHT_MIN, min(_WEIGHT_MAX, w))
    ceiling = _CONFIDENCE_CEILING.get(label)
    return min(w, ceiling) if ceiling is not None else w


def map_nodes_to_symbols(
    graph: ExternalGraph, symbols: list[dict]
) -> tuple[dict[str, int], int]:
    """External node id -> livespec symbol id, plus the ambiguous-node count.

    Direction matters: we walk *livespec's* symbols and ask the external graph
    to identify each one, rather than the reverse. livespec's symbol table is
    the thing being extended, so anything it does not contain is irrelevant by
    construction, and the lookup keeps `external_graph.ExternalGraph.lookup`'s
    guards (file nodes and prose nodes can never satisfy a symbol).

    `symbols` rows need `id`, `file_path`, `start_line` and `name`.
    """
    claimed: dict[str, list[int]] = {}
    for row in symbols:
        node = graph.lookup(
            row["file_path"], int(row["start_line"] or 0), row["name"] or ""
        )
        if node is None:
            continue
        claimed.setdefault(node.node_id, []).append(int(row["id"]))

    mapping: dict[str, int] = {}
    ambiguous = 0
    for node_id, symbol_ids in claimed.items():
        if len(symbol_ids) == 1:
            mapping[node_id] = symbol_ids[0]
        else:
            ambiguous += 1
    return mapping, ambiguous


def plan_ingest(
    graph: ExternalGraph,
    symbols: list[dict],
    existing_edges: set[tuple[int, int, str]],
    *,
    relations: frozenset[str] | set[str] | None = None,
) -> IngestPlan:
    """Decide which external edges to write, without writing anything.

    `existing_edges` is the set of `(src_symbol_id, dst_symbol_id, edge_type)`
    already in `symbol_edge` for this project — including rows a previous
    ingest wrote, which the caller is expected to delete first so that a
    re-ingest is a pure function of the current graph and index.
    """
    wanted = frozenset(relations) if relations is not None else DEFAULT_RELATIONS
    plan = IngestPlan()
    mapping, plan.ambiguous_nodes = map_nodes_to_symbols(graph, symbols)
    plan.mapped_nodes = len(mapping)

    seen: set[tuple[int, int, str]] = set()
    for src_node, links in graph.outbound.items():
        src_id = mapping.get(src_node)
        for relation, dst_node in links:
            if relation not in RELATION_EDGE_TYPE:
                plan._skip("unknown_relation")
                continue
            if relation not in wanted:
                plan._skip("relation_not_requested")
                continue
            dst_id = mapping.get(dst_node)
            if src_id is None or dst_id is None:
                # Overwhelmingly a Graphify file node or a prose node on one
                # end, or a symbol in a file livespec does not index.
                plan._skip("endpoint_not_indexed")
                continue
            if src_id == dst_id:
                plan._skip("self_loop")
                continue
            edge_type = RELATION_EDGE_TYPE[relation]
            key = (src_id, dst_id, edge_type)
            if key in existing_edges:
                plan.agreed += 1
                plan.agreed_by_relation[relation] = (
                    plan.agreed_by_relation.get(relation, 0) + 1
                )
                continue
            if key in seen:
                # Two external relations collapsing onto the same edge_type
                # (`calls` + `indirect_call`). One row, first one wins.
                plan._skip("duplicate_of_planned_edge")
                continue
            seen.add(key)
            meta = graph.link_meta.get((src_node, dst_node, relation), {})
            plan.new_edges.append(
                PlannedEdge(
                    src_symbol_id=src_id,
                    dst_symbol_id=dst_id,
                    edge_type=edge_type,
                    weight=_weight_for(
                        relation, meta.get("confidence"), meta.get("confidence_score")
                    ),
                    relation=relation,
                )
            )
            plan.by_relation[relation] = plan.by_relation.get(relation, 0) + 1
    return plan


def sample_edges(
    plan: IngestPlan, sym_meta: dict[int, dict], limit: int = 20
) -> list[dict]:
    """A readable slice of what would be added, for the dry-run payload.

    An ingest that reports only counts is an ingest nobody can sanity-check
    before letting it into their call graph.
    """
    out: list[dict] = []
    for edge in plan.new_edges[:limit]:
        src = sym_meta.get(edge.src_symbol_id, {})
        dst = sym_meta.get(edge.dst_symbol_id, {})
        out.append(
            {
                "from": src.get("qualified_name", str(edge.src_symbol_id)),
                "to": dst.get("qualified_name", str(edge.dst_symbol_id)),
                "relation": edge.relation,
                "edge_type": edge.edge_type,
                "weight": round(edge.weight, 3),
            }
        )
    return out
