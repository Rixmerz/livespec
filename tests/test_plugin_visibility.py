"""Per-workspace plugin visibility middleware (v0.18)."""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from livespec_mcp.plugin_visibility import PluginVisibilityMiddleware
from livespec_mcp.state import get_state
from livespec_mcp.tools import analysis, indexing, search, specs
from livespec_mcp.tools.plugins import register_all_plugins


def _minimal_mcp() -> FastMCP:
    mcp = FastMCP(name="visibility-test")
    mcp.add_middleware(PluginVisibilityMiddleware())
    indexing.register(mcp)
    analysis.register(mcp)
    specs.register(mcp)
    search.register(mcp)
    register_all_plugins(mcp)
    return mcp


def _seed_rf(state) -> None:
    state.conn.execute(
        "INSERT INTO spec (project_id, spec_id, title) VALUES (?, ?, ?)",
        (state.project_id, "SPEC-001", "seed"),
    )
    state.conn.commit()


@pytest.mark.asyncio
async def test_list_tools_hides_plugins_on_fresh_workspace(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "index_project" in names
    assert "create_spec" not in names
    assert "generate_docs" not in names
    assert "export_explorer" not in names
    assert "export_flow_explorer" not in names
    assert "import_specs_from_markdown" in names
    assert "bulk_link_spec_symbols" in names


@pytest.mark.asyncio
async def test_list_tools_shows_rf_after_workspace_touch(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state(create=True)
    _seed_rf(state)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        await c.call_tool(
            "get_project_overview",
            {"workspace": str(workspace)},
        )
        names = {t.name for t in await c.list_tools()}
    assert "create_spec" in names
    assert "link_spec_symbol" in names


@pytest.mark.asyncio
async def test_call_plugin_tool_blocked_without_rf_rows(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        result = await c.call_tool(
            "create_spec",
            {"title": "x", "workspace": str(workspace)},
        )
    data = result.data if hasattr(result, "data") else result.structured_content
    assert data.get("isError") is True
    assert "not active" in data.get("error", "").lower()
    assert data.get("hint")


@pytest.mark.asyncio
async def test_call_plugin_tool_allowed_when_rf_rows_exist(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state(create=True)
    _seed_rf(state)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        result = await c.call_tool(
            "create_spec",
            {"title": "from test", "workspace": str(workspace)},
        )
    data = result.data if hasattr(result, "data") else result.structured_content
    assert not data.get("isError")
    assert data.get("spec_id")


@pytest.mark.asyncio
async def test_export_explorer_gated_until_docs_plugin(workspace, monkeypatch):
    """export_explorer lives in the docs plugin — not always-visible core."""
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert "export_explorer" not in names
        blocked = (
            await c.call_tool(
                "export_explorer",
                {"workspace": str(workspace)},
            )
        ).data
        assert blocked.get("isError") is True
    monkeypatch.setenv("LIVESPEC_PLUGINS", "docs")
    mcp2 = _minimal_mcp()
    async with Client(mcp2) as c:
        names = {t.name for t in await c.list_tools()}
    assert "export_explorer" in names
    assert "generate_docs" in names


@pytest.mark.asyncio
async def test_env_all_shows_plugins_without_workspace_touch(workspace, monkeypatch):
    monkeypatch.setenv("LIVESPEC_PLUGINS", "all")
    mcp = _minimal_mcp()
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert "create_spec" in names
    assert "export_explorer" in names


def test_env_override_without_workspace_never_touches_state(monkeypatch):
    """C5 regression (v0.20): tools/list with LIVESPEC_PLUGINS set and no
    cached session workspace must parse the env var directly. The old path
    called get_state(None), which raises WorkspaceRequiredError before any
    workspace could be cached — permanently bricking fresh sessions."""
    from livespec_mcp import plugin_visibility as pv

    def _boom(_ws):
        raise AssertionError("get_state must not be called without a workspace")

    monkeypatch.setattr(pv, "get_state", _boom)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "all")
    assert pv._active_plugins(None) == {"spec", "docs"}
    monkeypatch.setenv("LIVESPEC_PLUGINS", "none")
    assert pv._active_plugins(None) == set()
    monkeypatch.setenv("LIVESPEC_PLUGINS", "spec")
    assert pv._active_plugins(None) == {"spec"}
