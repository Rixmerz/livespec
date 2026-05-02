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
async def test_find_symbol_and_quick_orient(sample_repo):
    """v0.8 P3.3: get_symbol_info dropped — quick_orient is the first-contact
    composite, get_symbol_source the body-extraction primitive."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        found = (await c.call_tool("find_symbol", {"query": "login"})).data
        names = {m["name"] for m in found["matches"]}
        assert "login" in names

        orient = (
            await c.call_tool("quick_orient", {"qname": "pkg.auth.login"})
        ).data
        assert orient["qualified_name"] == "pkg.auth.login"
        assert orient["kind"] == "function"
        assert orient["callers_count"] >= 1  # API.handle calls it


@pytest.mark.asyncio
async def test_who_calls_and_impact(sample_repo):
    """v0.8 P3.3: get_call_graph dropped — who_calls + who_does_this_call are
    the slim alternatives, analyze_impact is the wider blast-radius tool."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        callers = (
            await c.call_tool(
                "who_calls",
                {"qname": "pkg.auth.verify", "max_depth": 2},
            )
        ).data
        caller_qnames = {n["qualified_name"] for n in callers["callers"]}
        assert "pkg.auth.login" in caller_qnames

        impact = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "symbol", "target": "pkg.auth.verify", "max_depth": 4},
            )
        ).data
        impact_callers = {n["qualified_name"] for n in impact["impacted_callers"]}
        assert "pkg.auth.login" in impact_callers


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
    """project://index/status output must match get_index_status tool output.

    v0.8 P3.2: the deprecated tool adds `deprecated`/`replacement`/`removal`
    keys advising agents to migrate; the resource (the canonical surface)
    does not. Parity is over the data payload, not the deprecation envelope.
    """
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        tool_data = (await c.call_tool("get_index_status", {})).data
        res = await c.read_resource("project://index/status")
        resource_data = json.loads(res[0].text)
        for key in ("deprecated", "replacement", "removal"):
            tool_data.pop(key, None)
        assert tool_data == resource_data


@pytest.mark.asyncio
async def test_overview_filters_structural_pattern_names(workspace):
    """v0.8 P2 session-01 fix: short names appearing in ≥3 distinct files
    are demoted from `top_symbols`. PageRank correctly ranks them as
    high-centrality but they're structural patterns (`.get`, `add_parser`,
    `run` in the workflow runner), not semantically distinctive.

    Builds a fixture where `add_parser` is defined in 4 separate CLI
    sub-command files. With the filter (default), `add_parser` should
    NOT appear in top_symbols. With include_structural_patterns=True it
    should reappear. The filter list is reported back in
    `structural_patterns_filtered`."""
    cli = workspace / "cli"
    cli.mkdir()
    (cli / "__init__.py").write_text("")
    # Bodies need to be ≥5 lines so _is_infrastructure doesn't pre-filter
    # them as 1-line wrappers — this test isolates the structural-pattern
    # filter, not the infra one.
    for sub in ("init_cmd", "doctor", "graph_cmd", "memory_cmd"):
        (cli / f"{sub}.py").write_text(
            f'"""{sub} subcommand."""\n'
            "def add_parser(sub):\n"
            "    p = sub.add_parser('x')\n"
            "    p.add_argument('--flag')\n"
            "    p.add_argument('--name')\n"
            "    p.set_defaults(handler='x')\n"
            "    return p\n"
            "\n"
            "def run(args):\n"
            "    if args.flag:\n"
            "        print('flagged')\n"
            "    if args.name:\n"
            "        print(args.name)\n"
            "    return 0\n"
        )
    (workspace / "main.py").write_text(
        "from cli.init_cmd import add_parser as init_add\n"
        "from cli.doctor import add_parser as doc_add\n"
        "from cli.graph_cmd import add_parser as graph_add\n"
        "from cli.memory_cmd import add_parser as mem_add\n"
        "\n"
        "def main():\n"
        "    return [init_add, doc_add, graph_add, mem_add]\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})

        # Default: structural names filtered.
        ov = (await c.call_tool("get_project_overview", {})).data
        names = {s["name"] for s in ov["top_symbols"]}
        assert "add_parser" not in names, (
            f"add_parser leaked into top_symbols despite appearing in 4 files: {names}"
        )
        assert "run" not in names, (
            f"run leaked into top_symbols despite appearing in 4 files: {names}"
        )
        # Filtered list comes back so the agent knows what was hidden.
        assert "add_parser" in ov["structural_patterns_filtered"]
        assert "run" in ov["structural_patterns_filtered"]

        # Opt-in: structural names back in.
        ov_raw = (
            await c.call_tool(
                "get_project_overview",
                {"include_structural_patterns": True},
            )
        ).data
        raw_names = {s["name"] for s in ov_raw["top_symbols"]}
        assert "add_parser" in raw_names, (
            f"include_structural_patterns=True should restore add_parser: {raw_names}"
        )
        assert ov_raw["structural_patterns_filtered"] == []
