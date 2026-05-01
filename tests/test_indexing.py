"""End-to-end tests via FastMCP in-memory client."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_index_and_overview(sample_repo):
    async with Client(mcp) as c:
        result = await c.call_tool("index_project", {})
        data = result.data
        assert data["files_total"] >= 2
        assert data["symbols_total"] >= 4  # login, verify, API, handle
        assert "python" in data["languages"]

        overview = (await c.call_tool("get_project_overview", {})).data
        assert overview["workspace"] == str(sample_repo)
        assert any(lang["language"] == "python" for lang in overview["languages"])


@pytest.mark.asyncio
async def test_find_symbol_and_info(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        found = (await c.call_tool("find_symbol", {"query": "login"})).data
        names = {m["name"] for m in found["matches"]}
        assert "login" in names

        info = (
            await c.call_tool(
                "get_symbol_info",
                {"identifier": "pkg.auth.login", "detail": "full"},
            )
        ).data
        assert info["name"] == "login"
        assert info["kind"] == "function"
        assert info["callers_count"] >= 1  # API.handle calls it


@pytest.mark.asyncio
async def test_call_graph_and_impact(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        cg = (
            await c.call_tool(
                "get_call_graph",
                {"identifier": "pkg.auth.login", "direction": "both", "max_depth": 3},
            )
        ).data
        node_qnames = {n["qualified_name"] for n in cg["nodes"]}
        assert "pkg.auth.login" in node_qnames
        assert any("verify" in q for q in node_qnames)

        impact = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "symbol", "target": "pkg.auth.verify", "max_depth": 4},
            )
        ).data
        callers = {n["qualified_name"] for n in impact["impacted_callers"]}
        assert "pkg.auth.login" in callers


@pytest.mark.asyncio
async def test_requirement_crud_and_link(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        rf = (
            await c.call_tool(
                "create_requirement",
                {"title": "Login flow", "rf_id": "RF-001", "priority": "high"},
            )
        ).data
        assert rf["rf_id"] == "RF-001"

        rf2 = (
            await c.call_tool(
                "create_requirement",
                {"title": "API surface", "rf_id": "RF-002"},
            )
        ).data
        assert rf2["rf_id"] == "RF-002"

        # Annotation scan should link RF-001 -> pkg.auth.login via @rf: in docstring
        scan = (await c.call_tool("scan_rf_annotations", {})).data
        assert scan["links_created"] >= 1

        impl = (
            await c.call_tool("get_requirement_implementation", {"rf_id": "RF-001"})
        ).data
        qnames = {s["qualified_name"] for s in impl["symbols"]}
        assert "pkg.auth.login" in qnames

        # Manual link
        linked = (
            await c.call_tool(
                "link_rf_symbol",
                {"rf_id": "RF-002", "symbol_qname": "pkg.api.API.handle"},
            )
        ).data
        assert linked["linked"] is True

        impact = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "requirement", "target": "RF-001"},
            )
        ).data
        assert impact["rf_id"] == "RF-001"
        assert len(impact["implementing_symbols"]) >= 1


@pytest.mark.asyncio
async def test_search_keyword(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_requirement",
            {"title": "Login flow with password verification", "rf_id": "RF-001"},
        )
        results = (await c.call_tool("search", {"query": "login password", "limit": 5})).data
        assert len(results["results"]) > 0


@pytest.mark.asyncio
async def test_resource_overview(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        res = await c.read_resource("project://overview")
        body = res[0].text
        data = json.loads(body)
        # v0.8 P3 prep: project://overview is paritetic with get_project_overview
        assert "languages" in data
        assert "top_symbols" in data
        assert "requirements_total" in data
        assert "requirements_linked" in data


@pytest.mark.asyncio
async def test_resource_overview_parity_with_tool(sample_repo):
    """project://overview output must match get_project_overview tool output."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        tool_data = (await c.call_tool("get_project_overview", {})).data
        res = await c.read_resource("project://overview")
        resource_data = json.loads(res[0].text)
        assert tool_data == resource_data


@pytest.mark.asyncio
async def test_resource_index_status_parity_with_tool(sample_repo):
    """project://index/status output must match get_index_status tool output."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        tool_data = (await c.call_tool("get_index_status", {})).data
        res = await c.read_resource("project://index/status")
        resource_data = json.loads(res[0].text)
        assert tool_data == resource_data
