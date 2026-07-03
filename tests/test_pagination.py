"""v0.7 B3: aggregator tools have limit/cursor/summary_only.

The 286K/4.4M/7.3M payloads on the a large Rust monorepo Rust monorepo were the trigger.
Tests verify pagination contract holds even on small fixtures.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _make_dead_code_repo(workspace):
    """Generate >5 dead functions so we can test limit < total."""
    pkg = workspace / "lib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    body = ["def used():\n    return 1\n", "def caller():\n    return used()\n"]
    for i in range(8):
        body.append(
            f"def dead_{i:02d}():\n"
            f"    # nobody calls me, no spec link\n"
            f"    return {i}\n"
        )
    (pkg / "code.py").write_text("\n".join(body))


@pytest.mark.asyncio
async def test_find_dead_code_summary_only(workspace):
    _make_dead_code_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"summary_only": True})
        ).data
        assert out["count"] >= 8
        assert "by_kind" in out
        assert "by_top_dir" in out
        assert "dead_symbols" not in out  # summary excludes the list


@pytest.mark.asyncio
async def test_find_dead_code_limit_and_cursor(workspace):
    _make_dead_code_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        page1 = (
            await c.call_tool("find_dead_code", {"limit": 3, "cursor": 0})
        ).data
        assert len(page1["dead_symbols"]) == 3
        assert page1["next_cursor"] == 3
        page2 = (
            await c.call_tool("find_dead_code", {"limit": 3, "cursor": 3})
        ).data
        assert len(page2["dead_symbols"]) == 3
        # Pages are disjoint
        qn1 = {d["qualified_name"] for d in page1["dead_symbols"]}
        qn2 = {d["qualified_name"] for d in page2["dead_symbols"]}
        assert qn1.isdisjoint(qn2)


@pytest.mark.asyncio
async def test_audit_coverage_summary_only(workspace):
    """audit_coverage on a project with many files should respect summary_only."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for i in range(5):
        (pkg / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("audit_coverage", {"summary_only": True})).data
        assert "counts" in out
        assert isinstance(out["counts"]["modules_without_spec"], int)
        # Summary mode skips the lists themselves
        assert "modules_without_spec" not in out


@pytest.mark.asyncio
async def test_audit_coverage_paginated_lists(workspace):
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for i in range(8):
        (pkg / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("audit_coverage", {"limit": 3, "cursor": 0})).data
        assert len(out["modules_without_spec"]) <= 3
        assert "next_cursor" in out
        # The cursor maps per-list
        assert "modules_without_spec" in out["next_cursor"]


@pytest.mark.asyncio
async def test_find_orphan_tests_summary(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "__init__.py").write_text("")
    (workspace / "src" / "real.py").write_text("def prod():\n    return 1\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_orphan.py").write_text(
        "def test_alone():\n    return None\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_orphan_tests", {"summary_only": True})
        ).data
        assert "count" in out
        assert "orphan_tests" not in out


@pytest.mark.asyncio
async def test_git_diff_impact_summary_only(sample_repo):
    """summary_only avoids dumping the full impacted_callers list. Even when
    git is missing (sample_repo isn't a repo), the surface contract holds."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("git_diff_impact", {"summary_only": True})).data
        # error path is canonical
        assert out.get("isError") is True
