"""v0.9 P2: pagination on who_calls / who_does_this_call / analyze_impact.

Surfaced by Django battle-test (session 04) where max_depth=2 on
`BaseBackend.authenticate` returned 102KB / 400 callers and
analyze_impact at depth=3 returned 332KB / 664 callers / 848 calls_into.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _make_fanout_repo(workspace: Path) -> None:
    """Build a chain so target() has many transitive callers."""
    pkg = workspace / "lib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "target.py").write_text("def target():\n    return 1\n")
    # 12 callers, each in its own file, each calling target() directly
    for i in range(12):
        (pkg / f"caller_{i:02d}.py").write_text(
            f"from lib.target import target\n"
            f"\n"
            f"def call_{i:02d}():\n"
            f"    return target()\n"
        )


@pytest.mark.asyncio
async def test_who_calls_summary_only(workspace):
    _make_fanout_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "who_calls",
                {"qname": "lib.target.target", "max_depth": 1, "summary_only": True},
            )
        ).data
    assert "callers" not in out
    assert out["count"] >= 12
    assert out["root"] == "lib.target.target"


@pytest.mark.asyncio
async def test_who_calls_limit_and_cursor(workspace):
    _make_fanout_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        first = (
            await c.call_tool(
                "who_calls",
                {"qname": "lib.target.target", "max_depth": 1, "limit": 5},
            )
        ).data
        assert len(first["callers"]) == 5
        assert first["count"] >= 12
        assert first["next_cursor"] == 5

        second = (
            await c.call_tool(
                "who_calls",
                {
                    "qname": "lib.target.target",
                    "max_depth": 1,
                    "limit": 5,
                    "cursor": 5,
                },
            )
        ).data
        # Page 2 must not duplicate page 1
        first_qnames = {c["qualified_name"] for c in first["callers"]}
        second_qnames = {c["qualified_name"] for c in second["callers"]}
        assert first_qnames.isdisjoint(second_qnames)
        # Together they cover at least 10 of the 12+ callers
        assert len(first_qnames | second_qnames) >= 10


@pytest.mark.asyncio
async def test_who_does_this_call_summary_only(workspace):
    """Forward cone summary_only mirrors who_calls."""
    pkg = workspace / "lib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # root() calls 6 helpers
    helpers = "\n".join(f"def helper_{i}():\n    return {i}\n" for i in range(6))
    (pkg / "helpers.py").write_text(helpers)
    (pkg / "main.py").write_text(
        "from lib.helpers import "
        + ", ".join(f"helper_{i}" for i in range(6))
        + "\n\n"
        + "def root():\n"
        + "    return ("
        + " + ".join(f"helper_{i}()" for i in range(6))
        + ")\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "who_does_this_call",
                {"qname": "lib.main.root", "max_depth": 1, "summary_only": True},
            )
        ).data
    assert "callees" not in out
    assert out["count"] >= 6


@pytest.mark.asyncio
async def test_analyze_impact_symbol_summary_only(workspace):
    _make_fanout_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "analyze_impact",
                {
                    "target_type": "symbol",
                    "target": "lib.target.target",
                    "max_depth": 2,
                    "summary_only": True,
                },
            )
        ).data
    # No payload arrays — only counts
    assert "impacted_callers" not in out
    assert "calls_into" not in out
    assert out["counts"]["impacted_callers"] >= 12
    assert "affected_requirements" in out["counts"]


@pytest.mark.asyncio
async def test_analyze_impact_symbol_pagination(workspace):
    _make_fanout_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        first = (
            await c.call_tool(
                "analyze_impact",
                {
                    "target_type": "symbol",
                    "target": "lib.target.target",
                    "max_depth": 2,
                    "limit": 5,
                },
            )
        ).data
        assert len(first["impacted_callers"]) <= 5
        assert first["counts"]["impacted_callers"] >= 12
        assert first["next_cursor"] is not None


@pytest.mark.asyncio
async def test_analyze_impact_file_pagination(workspace):
    _make_fanout_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "analyze_impact",
                {
                    "target_type": "file",
                    "target": "lib/target.py",
                    "max_depth": 1,
                    "limit": 4,
                },
            )
        ).data
        assert "impacted_callers" in out
        assert len(out["impacted_callers"]) <= 4
        assert out["counts"]["impacted_callers"] >= 12
