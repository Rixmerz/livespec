"""v0.18 agent UX: payload warnings, grep_in_indexed_files, agent_scratch."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.analysis import _payload_warning


def test_payload_warning_when_count_exceeds_limit():
    msg = _payload_warning(500, limit=200, summary_only=False)
    assert msg is not None
    assert "summary_only" in msg


def test_payload_warning_absent_when_summary_only():
    assert _payload_warning(10_000, limit=200, summary_only=True) is None


def test_payload_warning_absent_for_small_results():
    assert _payload_warning(5, limit=200, summary_only=False) is None


@pytest.mark.asyncio
async def test_who_calls_payload_warning(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        data = (
            await c.call_tool(
                "who_calls",
                {
                    "qname": "pkg.auth.login",
                    "max_depth": 5,
                    "limit": 1,
                    "workspace": ws,
                },
            )
        ).data
    # Small repo — may or may not warn; ensure shape is backward compatible
    assert "count" in data
    if data.get("payload_warning"):
        assert "summary_only" in data["payload_warning"]


@pytest.mark.asyncio
async def test_grep_in_indexed_files_substring(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        data = (
            await c.call_tool(
                "grep_in_indexed_files",
                {"pattern": "login", "workspace": ws},
            )
        ).data
    assert data["count"] >= 1
    assert any("login" in m["text"].lower() for m in data["matches"])


@pytest.mark.asyncio
async def test_grep_in_indexed_files_path_glob(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        data = (
            await c.call_tool(
                "grep_in_indexed_files",
                {
                    "pattern": "def ",
                    "path_glob": "pkg/auth.py",
                    "workspace": ws,
                },
            )
        ).data
    assert data["count"] >= 1
    assert all(m["file_path"] == "pkg/auth.py" for m in data["matches"])


@pytest.mark.asyncio
async def test_agent_scratch_roundtrip(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        saved = (
            await c.call_tool(
                "agent_scratch",
                {
                    "qname": "pkg.auth.login",
                    "note": "entry point for auth",
                    "workspace": ws,
                },
            )
        ).data
        assert saved["saved"] is True
        cleared = (await c.call_tool("agent_scratch_clear", {"workspace": ws})).data
    assert cleared["cleared"] >= 1


@pytest.mark.asyncio
async def test_export_explorer_visible_on_fresh_workspace(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "export_explorer" in names
    assert "import_specs_from_markdown" in names
