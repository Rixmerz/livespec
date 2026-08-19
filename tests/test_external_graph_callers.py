"""The external graph answers backwards too: callers livespec's cone lacks.

`find_dead_code(corroborate_with=…)` was the first half of consuming a second
extractor, and it is the half where a livespec miss is *cheap*: a false "dead"
that the payload already tells you to confirm before deleting. The expensive
half is the other direction. A caller the resolver lost is missing from
`who_calls` and `analyze_impact` too, and there the payload reads as a complete
blast radius with nothing to warn you.

Measured on this repository against a code-only Graphify graph (2736 nodes,
5172 edges, `input_tokens: 0`):

    caller pairs both extractors agree on        1235
    caller pairs only the external graph has      139   <- this lane
      of those, with NO path at all in livespec   123
      by relation: calls 67, uses 56, references 14, indirect_call 2

The `uses` + `references` share is the point: 70 of 139. They are type-position usage —
`graph_pagerank(view: GraphView)`, `lookup(...) -> ExternalNode` — which
livespec does not model at all, so those callers are not merely unresolved,
they are unrepresentable. Hand-checked through the MCP wire: `ExternalNode`
gains `ExternalGraph.lookup` and `ExternalGraph._plausibly`, both of which name
it in a signature.

The invariants under test are the boundary ones. The external file annotates
livespec's graph; it never becomes it. Counts stay livespec's own, the lane
stays separate, and a graph that cannot be read fails loudly rather than
reporting an empty lane — which would read as "no missed callers" for a file
nobody could parse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_graph import (
    build_claim_index,
    load_external_graph,
)
from livespec_mcp.server import mcp


def _repo(workspace: Path) -> None:
    """Two callers livespec sees, one it cannot.

    `render` is called outright, so livespec resolves it. `Payload` is only
    ever named in a type annotation, which livespec does not extract — the
    exact shape of the 70 `uses`/`references` misses measured on this repo.
    """
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "model.py").write_text(
        "class Payload:\n"
        "    def __init__(self, body: str) -> None:\n"
        "        self.body = body\n"
    )
    (pkg / "render.py").write_text(
        "from pkg.model import Payload\n"
        "\n"
        "\n"
        "def render(p: Payload) -> str:\n"
        "    return p.body\n"
    )
    (pkg / "api.py").write_text(
        "from pkg.model import Payload\n"
        "from pkg.render import render\n"
        "\n"
        "\n"
        "def handle(raw: str) -> str:\n"
        "    return render(Payload(raw))\n"
    )


def _graph(workspace: Path, nodes: list[dict], links: list[dict]) -> str:
    """Write a Graphify-shaped graph.json and return its path."""
    p = workspace / "graph.json"
    p.write_text(
        json.dumps(
            {
                "directed": True,
                "nodes": [
                    {
                        "id": n["id"],
                        "label": n["label"],
                        "source_file": n["file"],
                        "source_location": f"L{n['line']}",
                        "_callable": n.get("callable", True),
                        "_origin": n.get("origin", "ast"),
                        "community": 0,
                    }
                    for n in nodes
                ],
                "links": [
                    {
                        "source": e["source"],
                        "target": e["target"],
                        "relation": e["relation"],
                        "confidence": "EXTRACTED",
                        "_origin": e.get("origin", "ast"),
                    }
                    for e in links
                ],
            }
        )
    )
    return str(p)


async def _index_and_locate(c: Client, qnames: list[str]) -> dict[str, dict]:
    """Index the workspace and return `find_symbol` metadata per short name."""
    await c.call_tool("index_project", {})
    out: dict[str, dict] = {}
    for q in qnames:
        hits = (await c.call_tool("find_symbol", {"query": q})).data["matches"]
        out[q] = next(h for h in hits if h["qualified_name"].endswith(q))
    return out


# --------------------------------------------------------------------------
# ClaimIndex — external node back to livespec symbol
# --------------------------------------------------------------------------


def test_a_node_two_symbols_both_claim_vouches_for_neither(tmp_path: Path):
    """Graphify slugs node ids case-insensitively, so siblings collapse.

    A `class Fingerprint` and a `def fingerprint()` in one module land on one
    node and pool their edges. Asking about either returns the union, which is
    how `Fingerprint` came back as calling `tokenize` when it is `fingerprint()`
    that does. Measured on this repo: 7 of 1417 matched nodes (0.4%) — small
    enough to move no published total, large enough that the hand-checked
    example was wrong.
    """
    path = _graph(
        tmp_path,
        [{"id": "dup", "label": "Fingerprint", "file": "pkg/dup.py", "line": 5}],
        [],
    )
    graph = load_external_graph(path)
    index = build_claim_index(
        graph,
        [
            (1, "pkg/dup.py", 5, "Fingerprint"),
            # The lowercase sibling misses on position and falls back to the
            # bare-name lookup, which is case-insensitive, so it lands here too.
            (2, "pkg/dup.py", 40, "fingerprint"),
        ],
    )
    assert index.ambiguous == {"dup"}
    assert index.symbol_for("dup") is None
    assert index.matched == 2


def test_an_uncontested_node_maps_back_to_its_one_symbol(tmp_path: Path):
    path = _graph(
        tmp_path,
        [{"id": "only", "label": "render", "file": "pkg/render.py", "line": 4}],
        [],
    )
    index = build_claim_index(
        load_external_graph(path), [(7, "pkg/render.py", 4, "render")]
    )
    assert index.symbol_for("only") == 7
    assert index.ambiguous == set()


def test_callers_of_names_the_source_where_evidence_for_only_counts(tmp_path: Path):
    """The two backward questions need different amounts of the same edge.

    `find_dead_code` makes a negative claim, so a bare relation contradicts it.
    `who_calls` makes a positive one, and a positive claim has to name the
    caller or an agent cannot check it.
    """
    path = _graph(
        tmp_path,
        [
            {"id": "target", "label": "Payload", "file": "pkg/model.py", "line": 1},
            {"id": "caller", "label": "render", "file": "pkg/render.py", "line": 4},
            {"id": "f", "label": "model.py", "file": "pkg/model.py", "line": 1,
             "callable": False},
        ],
        [
            {"source": "caller", "target": "target", "relation": "uses"},
            {"source": "f", "target": "target", "relation": "contains"},
        ],
    )
    graph = load_external_graph(path)
    node = graph.by_id["target"]
    assert graph.evidence_for(node) == ["uses"]
    assert [(rel, src.node_id) for rel, src in graph.callers_of(node)] == [
        ("uses", "caller")
    ]


def test_callers_of_drops_a_file_node_source(tmp_path: Path):
    """A file "using" a symbol is containment wearing another relation's name."""
    path = _graph(
        tmp_path,
        [
            {"id": "target", "label": "Payload", "file": "pkg/model.py", "line": 9},
            {"id": "file", "label": "api.py", "file": "pkg/api.py", "line": 1,
             "callable": False},
        ],
        [{"source": "file", "target": "target", "relation": "imports_from"}],
    )
    graph = load_external_graph(path)
    assert graph.callers_of(graph.by_id["target"]) == []


# --------------------------------------------------------------------------
# who_calls / analyze_impact integration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_who_calls_surfaces_a_type_position_caller_it_cannot_extract(
    workspace: Path,
):
    """The measured majority case: a caller that only names the type."""
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["Payload", "render"])
        base = (await c.call_tool("who_calls", {"qname": "Payload"})).data
        assert not any(
            m["qualified_name"].endswith("render") for m in base["callers"]
        ), "livespec should not resolve a type annotation — that is the premise"

        graph = _graph(
            workspace,
            [
                {"id": "payload", "label": "Payload",
                 "file": found["Payload"]["file_path"],
                 "line": found["Payload"]["start_line"]},
                {"id": "render", "label": "render",
                 "file": found["render"]["file_path"],
                 "line": found["render"]["start_line"]},
            ],
            [{"source": "render", "target": "payload", "relation": "uses"}],
        )
        out = (
            await c.call_tool(
                "who_calls", {"qname": "Payload", "corroborate_with": graph}
            )
        ).data

        external = out["external_callers"]
        assert [m["qualified_name"] for m in external] == [
            found["render"]["qualified_name"]
        ]
        assert external[0]["relations"] == ["uses"]
        assert out["external_evidence"]["count"] == 1
        assert out["external_evidence"]["by_relation"] == {"uses": 1}
        assert out["external_evidence"]["roots_matched"] == 1


@pytest.mark.asyncio
async def test_the_external_lane_never_moves_livespec_counts(workspace: Path):
    """The boundary: the external file annotates the cone, never joins it."""
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["Payload", "render"])
        base = (await c.call_tool("who_calls", {"qname": "Payload"})).data
        graph = _graph(
            workspace,
            [
                {"id": "payload", "label": "Payload",
                 "file": found["Payload"]["file_path"],
                 "line": found["Payload"]["start_line"]},
                {"id": "render", "label": "render",
                 "file": found["render"]["file_path"],
                 "line": found["render"]["start_line"]},
            ],
            [{"source": "render", "target": "payload", "relation": "uses"}],
        )
        out = (
            await c.call_tool(
                "who_calls", {"qname": "Payload", "corroborate_with": graph}
            )
        ).data
        assert out["count"] == base["count"]
        assert out["callers"] == base["callers"]
        assert out["external_callers"] not in (base.get("callers"), [])


@pytest.mark.asyncio
async def test_a_caller_livespec_already_has_is_not_repeated(workspace: Path):
    """Agreement is the common case; only the difference is worth payload."""
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["render", "handle"])
        base = (await c.call_tool("who_calls", {"qname": "render"})).data
        assert any(m["qualified_name"].endswith("handle") for m in base["callers"])

        graph = _graph(
            workspace,
            [
                {"id": "render", "label": "render",
                 "file": found["render"]["file_path"],
                 "line": found["render"]["start_line"]},
                {"id": "handle", "label": "handle",
                 "file": found["handle"]["file_path"],
                 "line": found["handle"]["start_line"]},
            ],
            [{"source": "handle", "target": "render", "relation": "calls"}],
        )
        out = (
            await c.call_tool(
                "who_calls", {"qname": "render", "corroborate_with": graph}
            )
        ).data
        assert out["external_callers"] == []
        assert out["external_evidence"]["count"] == 0


@pytest.mark.asyncio
async def test_structural_relations_never_produce_an_external_caller(
    workspace: Path,
):
    """`contains` and `method` are containment: every symbol has one."""
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["Payload", "render"])
        graph = _graph(
            workspace,
            [
                {"id": "payload", "label": "Payload",
                 "file": found["Payload"]["file_path"],
                 "line": found["Payload"]["start_line"]},
                {"id": "render", "label": "render",
                 "file": found["render"]["file_path"],
                 "line": found["render"]["start_line"]},
            ],
            [{"source": "render", "target": "payload", "relation": "method"}],
        )
        out = (
            await c.call_tool(
                "who_calls", {"qname": "Payload", "corroborate_with": graph}
            )
        ).data
        assert out["external_callers"] == []


@pytest.mark.asyncio
async def test_summary_only_reports_the_exact_external_count_without_the_list(
    workspace: Path,
):
    """The pagination contract: counts are exact regardless of the page."""
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["Payload", "render"])
        graph = _graph(
            workspace,
            [
                {"id": "payload", "label": "Payload",
                 "file": found["Payload"]["file_path"],
                 "line": found["Payload"]["start_line"]},
                {"id": "render", "label": "render",
                 "file": found["render"]["file_path"],
                 "line": found["render"]["start_line"]},
            ],
            [{"source": "render", "target": "payload", "relation": "uses"}],
        )
        out = (
            await c.call_tool(
                "who_calls",
                {"qname": "Payload", "corroborate_with": graph, "summary_only": True},
            )
        ).data
        assert "external_callers" not in out
        assert out["external_evidence"]["count"] == 1


@pytest.mark.asyncio
async def test_analyze_impact_carries_the_same_lane_for_a_file_target(
    workspace: Path,
):
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["Payload", "render"])
        graph = _graph(
            workspace,
            [
                {"id": "payload", "label": "Payload",
                 "file": found["Payload"]["file_path"],
                 "line": found["Payload"]["start_line"]},
                {"id": "render", "label": "render",
                 "file": found["render"]["file_path"],
                 "line": found["render"]["start_line"]},
            ],
            [{"source": "render", "target": "payload", "relation": "uses"}],
        )
        out = (
            await c.call_tool(
                "analyze_impact",
                {
                    "target_type": "file",
                    "target": found["Payload"]["file_path"],
                    "corroborate_with": graph,
                },
            )
        ).data
        assert [m["qualified_name"] for m in out["external_callers"]] == [
            found["render"]["qualified_name"]
        ]
        # Several roots, so each entry says which of them it reaches.
        assert out["external_evidence"]["roots"] >= 1
        assert out["external_callers"][0].get("targets") in (
            None,
            [found["Payload"]["qualified_name"]],
        )


@pytest.mark.asyncio
async def test_a_graph_that_cannot_be_read_fails_loudly(workspace: Path):
    """An empty lane would read as "no missed callers". It is not the same."""
    _repo(workspace)
    async with Client(mcp) as c:
        await _index_and_locate(c, ["Payload"])
        out = (
            await c.call_tool(
                "who_calls",
                {"qname": "Payload", "corroborate_with": "nope/graph.json"},
            )
        ).data
        assert out["isError"] is True
        assert "external_callers" not in out


@pytest.mark.asyncio
async def test_a_graph_of_another_repo_is_refused_rather_than_reported_empty(
    workspace: Path,
):
    _repo(workspace)
    async with Client(mcp) as c:
        await _index_and_locate(c, ["Payload"])
        graph = _graph(
            workspace,
            [{"id": "x", "label": "other", "file": "some/other/tree.go", "line": 3}],
            [],
        )
        out = (
            await c.call_tool(
                "who_calls", {"qname": "Payload", "corroborate_with": graph}
            )
        ).data
        assert out["isError"] is True
        assert "shares almost no files" in out["error"]


@pytest.mark.asyncio
async def test_a_non_ast_edge_is_surfaced_as_a_warning(workspace: Path):
    """Zero-LLM has to stay checkable, not merely claimed."""
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["Payload", "render"])
        graph = _graph(
            workspace,
            [
                {"id": "payload", "label": "Payload",
                 "file": found["Payload"]["file_path"],
                 "line": found["Payload"]["start_line"]},
                {"id": "render", "label": "render",
                 "file": found["render"]["file_path"],
                 "line": found["render"]["start_line"]},
            ],
            [
                {
                    "source": "render",
                    "target": "payload",
                    "relation": "uses",
                    "origin": "semantic",
                }
            ],
        )
        out = (
            await c.call_tool(
                "who_calls", {"qname": "Payload", "corroborate_with": graph}
            )
        ).data
        assert "_origin: ast" in out["external_evidence"]["warning"]


@pytest.mark.asyncio
async def test_a_graph_on_disk_is_announced_but_never_consumed(workspace: Path):
    """Same rule as dead code: an index that changed its answers because a
    file appeared on disk would be worse than one you have to ask."""
    _repo(workspace)
    async with Client(mcp) as c:
        found = await _index_and_locate(c, ["Payload", "render"])
        out_dir = workspace / "graphify-out"
        out_dir.mkdir()
        (out_dir / "graph.json").write_text(
            Path(
                _graph(
                    workspace,
                    [
                        {"id": "payload", "label": "Payload",
                         "file": found["Payload"]["file_path"],
                         "line": found["Payload"]["start_line"]},
                        {"id": "render", "label": "render",
                         "file": found["render"]["file_path"],
                         "line": found["render"]["start_line"]},
                    ],
                    [{"source": "render", "target": "payload", "relation": "uses"}],
                )
            ).read_text()
        )
        out = (await c.call_tool("who_calls", {"qname": "Payload"})).data
        assert "graphify-out/graph.json" in out["corroboration_available"]
        assert "external_callers" not in out
