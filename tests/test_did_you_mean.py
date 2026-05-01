"""P2.D3: 'Symbol not found' errors carry top-3 did_you_mean suggestions."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_did_you_mean_in_get_symbol_info(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # 'logn' is a fat-fingered 'login' — sample_repo has pkg.auth.login
        out = (await c.call_tool("get_symbol_info", {"identifier": "logn"})).data
        assert out.get("isError") is True
        suggestions = out.get("did_you_mean")
        assert isinstance(suggestions, list)
        qnames = {s["qualified_name"] for s in suggestions}
        assert "pkg.auth.login" in qnames, (
            f"login should appear in did_you_mean for 'logn': {suggestions}"
        )


@pytest.mark.asyncio
async def test_did_you_mean_in_get_call_graph(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("get_call_graph", {"identifier": "verify_xx", "max_depth": 2})
        ).data
        assert out.get("isError") is True
        qnames = {s["qualified_name"] for s in out["did_you_mean"]}
        assert "pkg.auth.verify" in qnames


@pytest.mark.asyncio
async def test_did_you_mean_in_analyze_impact(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "symbol", "target": "handlx"},
            )
        ).data
        assert out.get("isError") is True
        qnames = {s["qualified_name"] for s in out["did_you_mean"]}
        # `handle` is the closest match in sample_repo
        assert any("handle" in q for q in qnames), (
            f"handle should surface for 'handlx': {qnames}"
        )


@pytest.mark.asyncio
async def test_did_you_mean_in_link_requirement(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_requirement", {"title": "Login", "rf_id": "RF-001"}
        )
        out = (
            await c.call_tool(
                "link_requirement_to_code",
                {"rf_id": "RF-001", "symbol_qname": "pkg.auth.lgn"},
            )
        ).data
        assert out.get("isError") is True
        # short name "lgn" doesn't match anything; ensure structure is still present
        assert isinstance(out.get("did_you_mean"), list)


@pytest.mark.asyncio
async def test_did_you_mean_empty_when_no_match(sample_repo):
    """Garbage identifier still returns a list (possibly empty), never crashes."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("get_symbol_info", {"identifier": "zzzzzz"})).data
        assert out.get("isError") is True
        assert isinstance(out.get("did_you_mean"), list)
