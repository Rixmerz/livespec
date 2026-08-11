"""Corroborating `find_dead_code` against a second extractor's graph.

`find_dead_code` is livespec's least trustworthy output, and its errors are
systematic rather than random: they track what the extractor cannot see. A
symbol kept alive only by `class X extends Base`, by a type annotation, or by a
cross-file call the resolver lost, has no inbound edge here and reads as dead.

Measured on a real TypeScript composer (2026-08-11): 46 candidates, 19 dropped
once a Graphify graph of the same tree was consulted. Three were checked by hand
and all three were genuine livespec misses. The fixture in
`tests/fixtures/external_graph/` is a trimmed copy of that real `graph.json` —
same keys, same `relation`/`confidence` vocabulary, same node-link shape — so
these tests break if Graphify's format moves under us.

The invariants worth protecting are as much about failure as success: an
external file must never be able to break an index, and it must never be able
to quietly vouch for nothing. A graph that cannot be read, or that describes a
different repo, has to fail loudly — reporting "0 dropped" would read as a clean
bill of health for candidates nobody actually checked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_graph import (
    EVIDENCE_RELATIONS,
    STRUCTURAL_RELATIONS,
    load_external_graph,
    overlap_ratio,
)
from livespec_mcp.server import mcp

FIXTURE = (
    Path(__file__).parent / "fixtures" / "external_graph" / "graphify-graph.json"
)


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


def test_reads_real_graphify_node_link_shape():
    g = load_external_graph(FIXTURE)
    assert g.directed is True
    assert g.node_count == 6
    assert g.edge_count == 3
    # `source_location: "L26"` -> line 26
    node = g.by_position[("src/util/error.ts", 26)]
    assert node.label == "DomainError"
    assert node.community == 3
    assert node.origin == "ast"


def test_structural_relations_are_not_evidence_of_use():
    """`contains` is file->symbol containment: every symbol has one."""
    g = load_external_graph(FIXTURE)
    orphan = g.by_position[("src/util/orphan.ts", 5)]
    assert g.evidence_for(orphan) == []
    assert "contains" in STRUCTURAL_RELATIONS
    assert "contains" not in EVIDENCE_RELATIONS


def test_inheritance_counts_as_evidence():
    """The blind spot that motivated this: a base class livespec calls dead."""
    g = load_external_graph(FIXTURE)
    base = g.by_position[("src/util/error.ts", 26)]
    assert g.evidence_for(base) == ["inherits"]


def test_lookup_falls_back_to_name_within_the_same_file():
    """Decorated symbols can be recorded at the decorator line by one extractor
    and the `def` line by the other, so an exact-position miss is not a miss."""
    g = load_external_graph(FIXTURE)
    found = g.lookup("src/util/error.ts", 999, "DomainError")
    assert found is not None and found.label == "DomainError"
    # Scoped to the file: the same name elsewhere must not match.
    assert g.lookup("src/other/file.ts", 999, "DomainError") is None


def test_malformed_rows_are_skipped_not_raised(tmp_path: Path):
    """One bad row must not cost the caller the whole file."""
    p = tmp_path / "graph.json"
    p.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "ok", "label": "Ok", "source_file": "a.py",
                     "source_location": "L1"},
                    {"no_id": True},
                    "not even an object",
                    {"id": "weird", "label": "W", "source_file": "a.py",
                     "source_location": "not-a-line"},
                ],
                "links": [
                    {"source": "ok", "target": "weird", "relation": "calls"},
                    {"missing": "target"},
                ],
            }
        )
    )
    g = load_external_graph(p)
    assert g.node_count == 2  # the two with usable ids
    assert g.edge_count == 1


@pytest.mark.parametrize(
    "payload, reason",
    [
        ("{not json", "not valid JSON"),
        (json.dumps([1, 2, 3]), "expected a JSON object"),
        (json.dumps({"totally": "not a graph"}), "no 'nodes' array"),
    ],
)
def test_unreadable_graphs_raise_rather_than_return_empty(
    tmp_path: Path, payload: str, reason: str
):
    p = tmp_path / "graph.json"
    p.write_text(payload)
    with pytest.raises(ValueError) as exc:
        load_external_graph(p)
    assert reason in str(exc.value)


def test_overlap_ratio_detects_a_graph_of_a_different_repo():
    g = load_external_graph(FIXTURE)
    assert overlap_ratio(g, {"src/util/error.ts", "src/common/httpResponse.ts",
                             "src/common/axiosCommon.ts", "src/util/orphan.ts"}) == 1.0
    assert overlap_ratio(g, {"totally/other/tree.py"}) == 0.0


# --------------------------------------------------------------------------
# find_dead_code integration
# --------------------------------------------------------------------------


def _ts_repo(workspace: Path) -> None:
    """A tree with one genuinely dead symbol and one alive only by inheritance."""
    src = workspace / "src" / "util"
    src.mkdir(parents=True)
    (src / "error.ts").write_text(
        "class DomainError extends Error {\n"
        "  constructor(m: string) { super(m); }\n"
        "}\n"
        "class BadRequestError extends DomainError {\n"
        "  constructor(m: string) { super(m); }\n"
        "}\n"
        "export const handler = () => new BadRequestError('x');\n"
    )
    (src / "orphan.ts").write_text("function trulyUnused() {\n  return 1;\n}\n")


def _graph_for(workspace: Path, entries: list[dict]) -> str:
    """Write a graph.json whose positions match the indexed symbols."""
    nodes, links = [], []
    for i, e in enumerate(entries):
        nodes.append(
            {
                "id": f"n{i}",
                "label": e["label"],
                "source_file": e["file"],
                "source_location": f"L{e['line']}",
                "_origin": "ast",
                "community": 0,
            }
        )
    for i, e in enumerate(entries):
        if e.get("evidence"):
            links.append(
                {
                    "source": "n0",
                    "target": f"n{i}",
                    "relation": e["evidence"],
                    "confidence": "EXTRACTED",
                    "_origin": "ast",
                }
            )
    p = workspace / "graph.json"
    p.write_text(json.dumps({"directed": True, "nodes": nodes, "links": links}))
    return str(p)


@pytest.mark.asyncio
async def test_corroboration_drops_candidates_the_other_extractor_still_sees(
    workspace: Path,
):
    _ts_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        base = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        assert base["count"] >= 1

        dead = (await c.call_tool("find_dead_code", {})).data["dead_symbols"]
        target = dead[0]
        graph = _graph_for(
            workspace,
            [
                {
                    "label": target["qualified_name"].split(".")[-1],
                    "file": target["file_path"],
                    "line": target["start_line"],
                    "evidence": "inherits",
                }
            ],
        )
        out = (
            await c.call_tool(
                "find_dead_code", {"summary_only": True, "corroborate_with": graph}
            )
        ).data
        assert out["count"] == base["count"] - 1
        report = out["corroboration"]
        assert report["dropped_as_referenced"] == 1
        assert report["dropped_by_relation"] == {"inherits": 1}
        assert report["candidates_before"] == base["count"]


@pytest.mark.asyncio
async def test_structural_edges_alone_never_rescue_a_candidate(workspace: Path):
    """A `contains` edge is not a reason to call something alive."""
    _ts_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        base = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        dead = (await c.call_tool("find_dead_code", {})).data["dead_symbols"]
        target = dead[0]
        graph = _graph_for(
            workspace,
            [
                {
                    "label": target["qualified_name"].split(".")[-1],
                    "file": target["file_path"],
                    "line": target["start_line"],
                    "evidence": "contains",
                }
            ],
        )
        out = (
            await c.call_tool(
                "find_dead_code", {"summary_only": True, "corroborate_with": graph}
            )
        ).data
        assert out["count"] == base["count"]
        assert out["corroboration"]["dropped_as_referenced"] == 0


@pytest.mark.asyncio
async def test_missing_graph_is_a_shaped_error_not_a_silent_pass(workspace: Path):
    _ts_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "find_dead_code",
                {"summary_only": True, "corroborate_with": "does-not-exist.json"},
            )
        ).data
        assert out["isError"] is True
        assert "not found" in out["error"]
        assert "graphify" in out["hint"].lower()


@pytest.mark.asyncio
async def test_graph_of_a_different_repo_is_refused(workspace: Path):
    """The dangerous failure: 0 matches reported as 0 dropped reads as clean."""
    _ts_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        graph = _graph_for(
            workspace,
            [{"label": "Whatever", "file": "some/other/repo/file.ts", "line": 3}],
        )
        out = (
            await c.call_tool(
                "find_dead_code", {"summary_only": True, "corroborate_with": graph}
            )
        ).data
        assert out["isError"] is True
        assert "shares almost no files" in out["error"]


# --------------------------------------------------------------------------
# find_orphan_tests corroboration
# --------------------------------------------------------------------------
#
# The mirror image of dead code. There the question is "does anything refer to
# this?" (inbound); here it is "does this reach anything outside the tests?"
# (outbound) — the exact claim find_orphan_tests makes.
#
# Measured, and worth recording because it did NOT generalise:
#   sa-holiday-taxis (Java, JUnit `setUp` doing `new BookingRepositoryImpl()`)
#     17 orphans -> 9. All 8 drops via `calls` into production. Verified by hand.
#   livespec itself (Python, in-process FastMCP `Client(mcp)` harness)
#     26 orphans -> 26. Zero drops.
# The second is not a bug. A harness that dispatches by string name is a blind
# spot BOTH extractors share, and corroboration only helps where blind spots
# differ. The feature is honest about finding nothing.


def _orphan_graph(workspace: Path, test_sym: dict, target_file: str) -> str:
    """Graph where the orphan test has an outbound `calls` edge to target_file."""
    p = workspace / "orphan-graph.json"
    p.write_text(
        json.dumps(
            {
                "directed": True,
                "nodes": [
                    {
                        "id": "t0",
                        "label": test_sym["qualified_name"].split(".")[-1],
                        "source_file": test_sym["file_path"],
                        "source_location": "L1",
                        "_origin": "ast",
                    },
                    {
                        "id": "p0",
                        "label": "target",
                        "source_file": target_file,
                        "source_location": "L1",
                        "_origin": "ast",
                    },
                ],
                "links": [
                    {
                        "source": "t0",
                        "target": "p0",
                        "relation": "calls",
                        "confidence": "EXTRACTED",
                        "_origin": "ast",
                    }
                ],
            }
        )
    )
    return str(p)


def _repo_with_an_orphan_test(workspace: Path) -> None:
    (workspace / "src").mkdir()
    (workspace / "src" / "__init__.py").write_text("")
    (workspace / "src" / "prod.py").write_text("def real_work():\n    return 1\n")
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    # Reaches production only through a name the static cone cannot follow.
    (tests / "test_thing.py").write_text(
        "def test_via_indirection():\n"
        "    fn = globals().get('missing')\n"
        "    assert fn is None\n"
    )


@pytest.mark.asyncio
async def test_orphan_dropped_when_external_graph_shows_it_reaching_production(
    workspace: Path,
):
    _repo_with_an_orphan_test(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        base = (await c.call_tool("find_orphan_tests", {})).data
        assert base["count"] >= 1
        target = base["orphan_tests"][0]

        out = (
            await c.call_tool(
                "find_orphan_tests",
                {
                    "summary_only": True,
                    "corroborate_with": _orphan_graph(
                        workspace, target, "src/prod.py"
                    ),
                },
            )
        ).data
        assert out["count"] == base["count"] - 1
        report = out["corroboration"]
        assert report["dropped_as_reaching_production"] == 1
        assert report["dropped_by_relation"] == {"calls": 1}


@pytest.mark.asyncio
async def test_reaching_only_another_test_file_is_not_a_rescue(workspace: Path):
    """A test calling a test helper is still an orphan — that is the whole
    definition. Only landing on production counts."""
    _repo_with_an_orphan_test(workspace)
    (workspace / "tests" / "helpers.py").write_text("def helper():\n    return 2\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        base = (await c.call_tool("find_orphan_tests", {})).data
        target = base["orphan_tests"][0]
        out = (
            await c.call_tool(
                "find_orphan_tests",
                {
                    "summary_only": True,
                    "corroborate_with": _orphan_graph(
                        workspace, target, "tests/helpers.py"
                    ),
                },
            )
        ).data
        assert out["count"] == base["count"]
        assert out["corroboration"]["dropped_as_reaching_production"] == 0


@pytest.mark.asyncio
async def test_orphan_corroboration_refuses_a_foreign_graph(workspace: Path):
    _repo_with_an_orphan_test(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        p = workspace / "foreign.json"
        p.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": [
                        {
                            "id": "n0",
                            "label": "X",
                            "source_file": "elsewhere/x.py",
                            "source_location": "L1",
                        }
                    ],
                    "links": [],
                }
            )
        )
        out = (
            await c.call_tool(
                "find_orphan_tests",
                {"summary_only": True, "corroborate_with": str(p)},
            )
        ).data
        assert out["isError"] is True
        assert "shares almost no files" in out["error"]


# --------------------------------------------------------------------------
# propose_specs_from_codebase grouping
# --------------------------------------------------------------------------


def _two_module_repo(workspace: Path) -> None:
    """One capability deliberately split across two modules.

    Module-prefix grouping sees two features here. They are one.
    """
    for mod in ("services", "routes"):
        d = workspace / "src" / mod
        d.mkdir(parents=True)
        (d / "checkout.py").write_text(
            "\n\n".join(
                f"def {mod}_step{i}():\n    return {i}" for i in range(1, 5)
            )
            + "\n"
        )
    (workspace / "src" / "__init__.py").write_text("")


def _community_graph_for(workspace: Path, community: int = 4) -> str:
    """Put every indexed symbol in one community."""
    import sqlite3

    db = sqlite3.connect(workspace / ".mcp-docs" / "docs.db")
    db.row_factory = sqlite3.Row
    nodes = [
        {
            "id": f"n{i}",
            "label": r["name"],
            "source_file": r["path"],
            "source_location": f"L{r['start_line']}",
            "community": community,
            "_origin": "ast",
        }
        for i, r in enumerate(
            db.execute(
                "SELECT s.name, s.start_line, f.path FROM symbol s "
                "JOIN file f ON f.id=s.file_id"
            )
        )
    ]
    db.close()
    p = workspace / "communities.json"
    p.write_text(json.dumps({"directed": True, "nodes": nodes, "links": []}))
    return str(p)


@pytest.mark.asyncio
async def test_communities_merge_a_capability_split_across_modules(workspace: Path):
    _two_module_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        by_module = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {"module_depth": 2, "skip_already_covered": False},
            )
        ).data
        by_community = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {
                    "module_depth": 2,
                    "skip_already_covered": False,
                    "community_graph": _community_graph_for(workspace),
                },
            )
        ).data

        # The split capability collapses into a single proposal.
        assert len(by_community["proposals"]) < len(by_module["proposals"])
        keys = [p["module_key"] for p in by_community["proposals"]]
        assert any(k.startswith("community:") for k in keys)
        assert by_community["grouping"]["symbols_grouped_by_community"] > 0


@pytest.mark.asyncio
async def test_title_never_leaks_the_community_id(workspace: Path):
    """Graphify's own community labels are LLM-written, so we derive titles
    from the members instead — but a raw `community:4` title would be worse
    than either."""
    _two_module_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {
                    "module_depth": 2,
                    "skip_already_covered": False,
                    "community_graph": _community_graph_for(workspace),
                },
            )
        ).data
        for p in out["proposals"]:
            assert "community" not in p["title"].lower()
            assert p["title"].strip()
            assert not p["proposed_spec_id"].startswith("community")


@pytest.mark.asyncio
async def test_symbols_outside_the_external_graph_fall_back_to_modules(
    workspace: Path,
):
    """External grouping must only ever add signal — a symbol the graph does
    not cover still deserves a proposal."""
    _two_module_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # A graph that covers nothing in this repo would be refused by the
        # overlap guard, so cover exactly one file and leave the other out.
        empty = workspace / "partial.json"
        empty.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": [
                        {
                            "id": "n0",
                            "label": "services_step1",
                            "source_file": "src/services/checkout.py",
                            "source_location": "L1",
                            "community": 9,
                            "_origin": "ast",
                        }
                    ],
                    "links": [],
                }
            )
        )
        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {
                    "module_depth": 2,
                    "skip_already_covered": False,
                    "community_graph": str(empty),
                },
            )
        ).data
        grouping = out["grouping"]
        assert grouping["symbols_grouped_by_module"] > 0
        keys = [p["module_key"] for p in out["proposals"]]
        assert any(not k.startswith("community:") for k in keys)


@pytest.mark.asyncio
async def test_proposals_refuse_a_graph_of_a_different_repo(workspace: Path):
    _two_module_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        p = workspace / "other.json"
        p.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": [
                        {
                            "id": "n0",
                            "label": "Whatever",
                            "source_file": "some/other/repo/x.py",
                            "source_location": "L1",
                            "community": 1,
                        }
                    ],
                    "links": [],
                }
            )
        )
        out = (
            await c.call_tool(
                "propose_specs_from_codebase", {"community_graph": str(p)}
            )
        ).data
        assert out["isError"] is True
        assert "shares almost no files" in out["error"]


@pytest.mark.asyncio
async def test_non_ast_origin_is_surfaced_as_a_warning(workspace: Path):
    """Graphify's code pass is LLM-free; its semantic pass is not. A graph
    carrying semantic edges must not silently erode the zero-LLM claim."""
    _ts_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        dead = (await c.call_tool("find_dead_code", {})).data["dead_symbols"]
        target = dead[0]
        p = workspace / "graph.json"
        p.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": [
                        {
                            "id": "n0",
                            "label": target["qualified_name"].split(".")[-1],
                            "source_file": target["file_path"],
                            "source_location": f"L{target['start_line']}",
                            "_origin": "ast",
                        }
                    ],
                    "links": [
                        {
                            "source": "n0",
                            "target": "n0",
                            "relation": "references",
                            "_origin": "semantic",
                        }
                    ],
                }
            )
        )
        out = (
            await c.call_tool(
                "find_dead_code",
                {"summary_only": True, "corroborate_with": str(p)},
            )
        ).data
        assert "warning" in out["corroboration"]
        assert "LLM" in out["corroboration"]["warning"]
