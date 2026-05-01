"""v0.8 P0: quick-win tools — get_symbol_source, who_calls,
who_does_this_call, quick_orient."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_get_symbol_source_happy_path(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("get_symbol_source", {"qname": "pkg.auth.login"})
        ).data
        assert out["qualified_name"] == "pkg.auth.login"
        assert out["file_path"] == "pkg/auth.py"
        assert "def login" in out["source"]
        assert "verify(user, password)" in out["source"]
        assert out["start_line"] >= 1
        assert out["end_line"] >= out["start_line"]
        assert out["body_hash"]


@pytest.mark.asyncio
async def test_get_symbol_source_unknown_qname(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("get_symbol_source", {"qname": "pkg.auth.lgoin"})
        ).data
        assert out["isError"] is True
        assert "not found" in out["error"]
        # did_you_mean should surface 'login' for the typo
        suggestions = {s["qualified_name"] for s in out.get("did_you_mean", [])}
        assert any("login" in s for s in suggestions)


@pytest.mark.asyncio
async def test_who_calls_returns_caller_set(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("who_calls", {"qname": "pkg.auth.verify"})
        ).data
        names = {n["qualified_name"] for n in out["callers"]}
        assert "pkg.auth.login" in names
        assert out["root"] == "pkg.auth.verify"
        assert out["count"] >= 1
        assert out["max_depth"] == 1


@pytest.mark.asyncio
async def test_who_calls_unknown_qname(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("who_calls", {"qname": "pkg.does.not.exist"})
        ).data
        assert out["isError"] is True


@pytest.mark.asyncio
async def test_who_does_this_call_returns_callees(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "who_does_this_call", {"qname": "pkg.auth.login"}
            )
        ).data
        names = {n["qualified_name"] for n in out["callees"]}
        assert "pkg.auth.verify" in names
        assert out["root"] == "pkg.auth.login"
        assert out["count"] >= 1


@pytest.mark.asyncio
async def test_who_does_this_call_leaf_symbol(sample_repo):
    """A symbol with no outgoing calls returns callees=[]."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "who_does_this_call", {"qname": "pkg.auth.verify"}
            )
        ).data
        assert out["callees"] == []
        assert out["count"] == 0


@pytest.mark.asyncio
async def test_quick_orient_composite(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("quick_orient", {"qname": "pkg.auth.login"})
        ).data
        assert out["qualified_name"] == "pkg.auth.login"
        assert out["kind"] == "function"
        assert out["file_path"] == "pkg/auth.py"
        # docstring lead should be a non-empty stripped first line
        assert out["docstring_lead"]
        # login is called by API.handle (1 caller) and calls verify (1 callee)
        assert out["callers_count"] >= 1
        assert out["callees_count"] >= 1
        callee_names = {c["qualified_name"] for c in out["top_callees"]}
        assert "pkg.auth.verify" in callee_names
        # Each top entry carries pagerank
        for entry in out["top_callers"] + out["top_callees"]:
            assert "pagerank" in entry


@pytest.mark.asyncio
async def test_quick_orient_includes_linked_rfs(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_requirement",
            {"title": "Login flow", "rf_id": "RF-001"},
        )
        # Pick up the @rf:RF-001 in login's docstring
        await c.call_tool("scan_rf_annotations", {})

        out = (
            await c.call_tool("quick_orient", {"qname": "pkg.auth.login"})
        ).data
        rf_ids = {r["rf_id"] for r in out["requirements"]}
        assert "RF-001" in rf_ids


@pytest.mark.asyncio
async def test_quick_orient_unknown_qname(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("quick_orient", {"qname": "totally.not.real"})
        ).data
        assert out["isError"] is True
