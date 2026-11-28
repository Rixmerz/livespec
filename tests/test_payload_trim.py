"""Payload size: `analyze_impact` lists direct neighbours (the rest is counted
per depth, `include_transitive=True` lists it) and `get_project_overview` caps
the structural-pattern names it names."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

CHAIN = (
    "def target():\n    return 1\n\n"
    "def direct_a():\n    return target()\n\n"
    "def direct_b():\n    return target()\n\n"
    "def hop2():\n    return direct_a()\n\n"
    "def hop3():\n    return hop2()\n\n"
    "def callee():\n    return 0\n\n"
    "def target2():\n    return callee()\n"
)


@pytest.mark.asyncio
async def test_analyze_impact_lists_depth_one_and_counts_the_rest(workspace):
    (workspace / "m.py").write_text(CHAIN)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        args = {"target_type": "symbol", "target": "m.target"}
        slim = (await c.call_tool("analyze_impact", args)).data
        full = (await c.call_tool("analyze_impact", {**args, "include_transitive": True})).data
        summary = (await c.call_tool("analyze_impact", {**args, "summary_only": True})).data

    assert {r["name"] for r in slim["impacted_callers"]} == {"direct_a", "direct_b"}
    assert {r["name"] for r in full["impacted_callers"]} == {
        "direct_a",
        "direct_b",
        "hop2",
        "hop3",
    }
    # counts are the whole cone either way, with its shape per depth
    for out in (slim, full, summary):
        assert out["counts"]["impacted_callers"] == 4
        assert out["counts"]["impacted_callers_by_depth"] == {"1": 2, "2": 1, "3": 1}
    assert "transitive_hint" in slim and "transitive_hint" not in full
    assert len(str(slim)) < len(str(full))


@pytest.mark.asyncio
async def test_analyze_impact_calls_into_follows_the_same_rule(workspace):
    (workspace / "m.py").write_text(
        "def leaf():\n    return 0\n\ndef mid():\n    return leaf()\n\n"
        "def top():\n    return mid()\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        args = {"target_type": "symbol", "target": "m.top"}
        slim = (await c.call_tool("analyze_impact", args)).data
        full = (await c.call_tool("analyze_impact", {**args, "include_transitive": True})).data
    assert [r["name"] for r in slim["calls_into"]] == ["mid"]
    assert {r["name"] for r in full["calls_into"]} == {"mid", "leaf"}
    assert slim["counts"]["calls_into"] == 2
    assert slim["counts"]["calls_into_by_depth"] == {"1": 1, "2": 1}


@pytest.mark.asyncio
async def test_overview_caps_structural_names_and_counts_the_rest(workspace):
    # 40 names each defined in three non-test files, plus one defined only in tests.
    names = [f"helper_{i:02d}" for i in range(40)]
    body = "\n".join(f"def {n}():\n    return 1\n" for n in names)
    for i in range(3):
        (workspace / f"mod{i}.py").write_text(body)
    (workspace / "tests").mkdir()
    for i in range(3):
        (workspace / "tests" / f"test_x{i}.py").write_text("def only_in_tests():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("get_project_overview", {})).data
    assert len(out["structural_patterns_filtered"]) == 30
    assert out["structural_patterns_filtered_total"] >= 41
    assert out["structural_patterns_filtered_test_only"] >= 1
    assert "only_in_tests" not in out["structural_patterns_filtered"]
