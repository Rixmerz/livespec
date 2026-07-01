"""Per-workspace plugin visibility middleware (v0.18)."""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from livespec_mcp.plugin_visibility import PluginVisibilityMiddleware
from livespec_mcp.state import get_state
from livespec_mcp.tools import analysis, indexing, requirements, search
from livespec_mcp.tools.plugins import register_all_plugins


def _minimal_mcp() -> FastMCP:
    mcp = FastMCP(name="visibility-test")
    mcp.add_middleware(PluginVisibilityMiddleware())
    indexing.register(mcp)
    analysis.register(mcp)
    requirements.register(mcp)
    search.register(mcp)
    register_all_plugins(mcp)
    return mcp


def _seed_rf(state) -> None:
    state.conn.execute(
        "INSERT INTO rf (project_id, rf_id, title) VALUES (?, ?, ?)",
        (state.project_id, "RF-001", "seed"),
    )
    state.conn.commit()


@pytest.mark.asyncio
async def test_list_tools_hides_plugins_on_fresh_workspace(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "index_project" in names
    assert "create_requirement" not in names
    assert "generate_docs" not in names
    assert "export_explorer" not in names
    assert "bulk_link_rf_symbols" in names


@pytest.mark.asyncio
async def test_list_tools_shows_rf_after_workspace_touch(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state()
    _seed_rf(state)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        await c.call_tool(
            "get_project_overview",
            {"workspace": str(workspace)},
        )
        names = {t.name for t in await c.list_tools()}
    assert "create_requirement" in names
    assert "link_rf_symbol" in names


@pytest.mark.asyncio
async def test_call_plugin_tool_blocked_without_rf_rows(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        result = await c.call_tool(
            "create_requirement",
            {"title": "x", "workspace": str(workspace)},
        )
    data = result.data if hasattr(result, "data") else result.structured_content
    assert data.get("isError") is True
    assert "not active" in data.get("error", "").lower()
    assert data.get("hint")


@pytest.mark.asyncio
async def test_call_plugin_tool_allowed_when_rf_rows_exist(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state()
    _seed_rf(state)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        result = await c.call_tool(
            "create_requirement",
            {"title": "from test", "workspace": str(workspace)},
        )
    data = result.data if hasattr(result, "data") else result.structured_content
    assert not data.get("isError")
    assert data.get("rf_id")


@pytest.mark.asyncio
async def test_env_all_shows_plugins_without_workspace_touch(workspace, monkeypatch):
    monkeypatch.setenv("LIVESPEC_PLUGINS", "all")
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "create_requirement" in names
    assert "export_explorer" in names
