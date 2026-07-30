"""v0.18 agent UX: payload warnings, grep_in_indexed_files, explorer gating."""

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
async def test_grep_fresh_index_reports_scope_fresh(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        data = (
            await c.call_tool(
                "grep_in_indexed_files",
                {"pattern": "login", "workspace": ws},
            )
        ).data
    assert data["scope_fresh"] is True
    assert data["count"] >= 1
    assert "hint" not in data
    assert "stale_files" not in data
    assert "unindexed_files" not in data


@pytest.mark.asyncio
async def test_grep_signals_stale_when_indexed_file_edited(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        (sample_repo / "pkg" / "auth.py").write_text(
            "def login(user, password):\n    return NEEDLE_XYZ\n"
        )
        data = (
            await c.call_tool(
                "grep_in_indexed_files",
                {"pattern": "NEEDLE_XYZ", "workspace": ws},
            )
        ).data
    assert data["scope_fresh"] is False
    assert data["stale_files"] == ["pkg/auth.py"]
    assert data["stale_files_count"] == 1
    assert "index_project" in data["hint"]
    # The match itself still comes back — grep reads current bytes.
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_grep_signals_never_indexed_file_on_disk(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        (sample_repo / "pkg" / "brand_new.py").write_text("NEEDLE_XYZ = 1\n")
        data = (
            await c.call_tool(
                "grep_in_indexed_files",
                {"pattern": "NEEDLE_XYZ", "workspace": ws},
            )
        ).data
    # The whole point: the match is invisible to grep...
    assert data["count"] == 0
    # ...but the payload says why, instead of reading as "no matches exist".
    assert data["scope_fresh"] is False
    assert data["unindexed_files"] == ["pkg/brand_new.py"]
    assert data["unindexed_files_count"] == 1
    assert "NOT searched" in data["hint"]


@pytest.mark.asyncio
async def test_grep_staleness_respects_path_glob_scope(sample_repo):
    """The verdict is scope-bound: an unindexed file outside the glob is not
    reported, because it cannot affect this result."""
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        (sample_repo / "pkg" / "elsewhere.py").write_text("x = 1\n")
        data = (
            await c.call_tool(
                "grep_in_indexed_files",
                {"pattern": "def ", "path_glob": "pkg/auth.py", "workspace": ws},
            )
        ).data
    assert data["scope_fresh"] is True
    assert data["count"] >= 1


@pytest.mark.asyncio
async def test_grep_response_contract_unchanged(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        data = (
            await c.call_tool(
                "grep_in_indexed_files",
                {"pattern": "def ", "limit": 1, "workspace": ws},
            )
        ).data
    assert data["pattern"] == "def "
    assert isinstance(data["count"], int) and data["count"] > 1
    assert len(data["matches"]) == 1
    assert set(data["matches"][0]) == {"file_path", "language", "line", "text"}
    assert data["next_cursor"] == 1


@pytest.mark.asyncio
async def test_export_explorer_hidden_on_fresh_without_docs_plugin(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    # Use a visibility-aware minimal surface — full server conftest forces =all.
    from fastmcp import FastMCP

    from livespec_mcp.plugin_visibility import PluginVisibilityMiddleware
    from livespec_mcp.tools import analysis, indexing, search, specs
    from livespec_mcp.tools.plugins import register_all_plugins

    m = FastMCP(name="fresh-vis")
    m.add_middleware(PluginVisibilityMiddleware())
    indexing.register(m)
    analysis.register(m)
    specs.register(m)
    search.register(m)
    register_all_plugins(m)
    async with Client(m) as c:
        names = {t.name for t in await c.list_tools()}
    assert "export_explorer" not in names
    assert "import_specs_from_markdown" in names
    assert "agent_scratch" not in names
