"""The relation vocabulary livespec reads out of another extractor's graph.

Two ways this goes wrong, and both are silent.

**Too narrow.** Graphify 0.9.55 emits `implements`, `extends`, `specializes`
and `embeds` — per-language spellings of inheritance for Java/C# interfaces,
CommonLisp specialisation and Go struct embedding. livespec knew only
`inherits` and `mixes_in`, so a Java interface implemented ten times over still
read as referenced by nothing. That is the exact blind spot the whole
external-graph feature exists to cover, missed because the v0.32 measurements
ran on Python and TypeScript trees.

**Too wide.** `defines` and `exports` are emitted `file_node -> symbol`, so
every symbol in those languages has one. `evidence_for` looks only at the
relation of an inbound edge, never at whether its source maps to anything, so
one containment relation left off the structural list makes every symbol in
that language un-killable. That is precisely what `method` did before the
14-repo sweep caught it: 98 of 223 rescues were an artifact.

So both lists are asserted here against a stated rule, and a third test pins
the reporting that makes the *next* drift cheap to find.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_graph import (
    EVIDENCE_RELATIONS,
    STRUCTURAL_RELATIONS,
    classify_relation,
    load_external_graph,
)
from livespec_mcp.domain.external_ingest import (
    DEFAULT_RELATIONS,
    IMPORT_RELATIONS,
    RELATION_EDGE_TYPE,
    _weight_for,
)
from livespec_mcp.server import mcp

# Relations observed in Graphify 0.9.55's own extractors (grep of `add_edge`
# and `"relation":` emission sites), classified by what the emitting call
# actually connects. Written down here so the next version bump is a diff
# against evidence rather than a guess.
INHERITANCE_SPELLINGS = ("inherits", "mixes_in", "implements", "extends", "embeds")
CONTAINMENT_SPELLINGS = ("contains", "method", "defines", "exports")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("relation", CONTAINMENT_SPELLINGS)
def test_containment_is_never_evidence(relation: str):
    """`file -> symbol` and `class -> its own method` prove nothing about use.

    Every symbol has one, so counting it rescues everything.
    """
    assert classify_relation(relation) == "structural"
    assert relation not in EVIDENCE_RELATIONS
    assert relation not in RELATION_EDGE_TYPE


@pytest.mark.parametrize("relation", INHERITANCE_SPELLINGS)
def test_every_spelling_of_inheritance_is_understood(relation: str):
    assert classify_relation(relation) == "evidence"
    assert RELATION_EDGE_TYPE[relation] == "inherits"
    assert relation in DEFAULT_RELATIONS


def test_instantiation_is_a_call():
    """livespec models `Foo()` as a call; another extractor spelling the same
    fact `instantiates` must not land on a weaker edge type."""
    assert RELATION_EDGE_TYPE["instantiates"] == "calls"
    assert "instantiates" in DEFAULT_RELATIONS


def test_a_relation_nobody_taught_us_is_unknown_not_evidence():
    """The safe default. An unrecognised relation must never be guessed onto
    an edge_type — ingestion writes a row, and a wrong row survives into
    `analyze_impact`."""
    assert classify_relation("frobnicates") == "unknown"
    assert "frobnicates" not in RELATION_EDGE_TYPE


def test_the_two_lists_cannot_disagree():
    """Every ingestible relation must also count as evidence. If it can create
    an edge but not rescue a dead-code candidate, the same graph makes the two
    tools contradict each other."""
    assert set(RELATION_EDGE_TYPE) <= EVIDENCE_RELATIONS
    assert not (EVIDENCE_RELATIONS & STRUCTURAL_RELATIONS)


def test_imports_stay_out_of_the_default_ingest_set():
    """`who_calls` does not distinguish edge types, so an ingested import row
    reports importers as callers."""
    assert IMPORT_RELATIONS <= set(RELATION_EDGE_TYPE)
    assert not (DEFAULT_RELATIONS & IMPORT_RELATIONS)
    assert DEFAULT_RELATIONS == set(RELATION_EDGE_TYPE) - IMPORT_RELATIONS


# --------------------------------------------------------------------------
# Confidence -> weight
# --------------------------------------------------------------------------


def test_an_external_claim_never_earns_a_resolved_weight():
    """1.0 means livespec's own resolver saw the call and disambiguated it. A
    second extractor working from its own parse has not earned that."""
    assert _weight_for("calls", "EXTRACTED", None) == 0.9
    assert _weight_for("calls", "EXTRACTED", 1.0) <= 0.9


def test_an_inferred_edge_lands_where_min_weight_can_filter_it():
    """0.6 is the default `min_weight`; an INFERRED edge must sit at or below
    the same threshold that mutes livespec's own guesses."""
    assert _weight_for("calls", "INFERRED", None) == 0.6


def test_ambiguous_beats_a_score_that_disagrees_with_it():
    """Graphify 0.9.55's third label. AMBIGUOUS is the other tool stating it
    could not disambiguate — exactly what livespec's 0.5 means, and 0.5 is what
    `min_weight=0.6` filters. A high numeric score must not lift it past the
    filter built to catch guesses."""
    assert _weight_for("calls", "AMBIGUOUS", None) == 0.5
    assert _weight_for("calls", "AMBIGUOUS", 0.99) == 0.5
    assert _weight_for("calls", "ambiguous", 0.95) == 0.5


def test_a_missing_confidence_is_treated_as_the_weakest_claim():
    assert _weight_for("calls", None, None) == 0.5
    assert _weight_for("calls", "SOMETHING_NEW", None) == 0.5


def test_a_score_outside_the_ladder_is_clamped_not_trusted():
    assert _weight_for("calls", "EXTRACTED", 5.0) == 0.9
    assert _weight_for("calls", "EXTRACTED", -1.0) == 0.5
    # `True` is an int in Python; it must not be read as a score of 1.0.
    assert _weight_for("calls", "INFERRED", True) == 0.6


# --------------------------------------------------------------------------
# Drift reporting
# --------------------------------------------------------------------------


def _graph_file(tmp_path: Path, relations: list[str]) -> Path:
    nodes = [
        {
            "id": f"n{i}",
            "label": f"sym{i}",
            "source_file": "a.py",
            "source_location": f"L{i + 1}",
            "_callable": True,
            "_origin": "ast",
        }
        for i in range(len(relations) + 1)
    ]
    links = [
        {"source": f"n{i}", "target": f"n{i + 1}", "relation": rel, "_origin": "ast"}
        for i, rel in enumerate(relations)
    ]
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"directed": True, "nodes": nodes, "links": links}))
    return p


def test_an_unrecognised_relation_is_counted_not_dropped(tmp_path: Path):
    graph = load_external_graph(_graph_file(tmp_path, ["calls", "warps", "warps"]))

    assert graph.unknown_relations == {"warps": 2}


def test_a_graph_we_fully_understand_reports_no_drift(tmp_path: Path):
    graph = load_external_graph(
        _graph_file(tmp_path, ["calls", "implements", "contains"])
    )

    assert graph.unknown_relations == {}


@pytest.mark.asyncio
async def test_the_ingest_payload_names_the_vocabulary_it_did_not_understand(
    workspace: Path,
):
    """The durable half of this fix. The last two additions to this vocabulary
    were found by reading another project's source during an audit; a count in
    the payload is what makes the next one cheap."""
    (workspace / "a.py").write_text("def one():\n    return 1\n\n\ndef two():\n    return one()\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        from livespec_mcp.state import get_state

        st = get_state(str(workspace))
        rows = {
            r["qualified_name"]: dict(r)
            for r in st.conn.execute(
                """SELECT s.qualified_name, s.name, s.start_line, f.path AS file_path
                   FROM symbol s JOIN file f ON f.id = s.file_id
                   WHERE f.project_id = ?""",
                (st.project_id,),
            )
        }
        names = sorted(q for q in rows if q.endswith((".one", ".two")))
        nodes = [
            {
                "id": q,
                "label": rows[q]["name"],
                "source_file": rows[q]["file_path"],
                "source_location": f"L{rows[q]['start_line']}",
                "_callable": True,
                "_origin": "ast",
            }
            for q in names
        ]
        graph = workspace / "g.json"
        graph.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": nodes,
                    "links": [
                        {
                            "source": names[1],
                            "target": names[0],
                            "relation": "teleports_to",
                            "_origin": "ast",
                        }
                    ],
                }
            )
        )
        payload = (
            await c.call_tool("ingest_external_graph", {"graph_path": str(graph)})
        ).data

    assert payload["unknown_relations"] == {"teleports_to": 1}
    assert "unknown_relations_hint" in payload
    # Unknown means unknown: nothing was written on the strength of it.
    assert payload["edges_to_add"] == 0
