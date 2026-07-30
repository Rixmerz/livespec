"""FTS5 search tool + AST chunk rebuild on index."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import get_state


@pytest.mark.asyncio
async def test_index_populates_chunks(sample_repo):
    async with Client(mcp) as c:
        data = (await c.call_tool("index_project", {})).data
        assert "chunks" in data
        assert data["chunks"]["symbol_chunks"] >= 4
        assert "embeddings" not in data
        st = get_state()
        n = st.conn.execute(
            "SELECT COUNT(*) c FROM chunk WHERE project_id=?", (st.project_id,)
        ).fetchone()["c"]
        assert n >= 4


@pytest.mark.asyncio
async def test_search_fts_finds_symbol_by_keyword(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("search", {"query": "login user password"})).data
        assert out["count"] > 0
        files = {r["file_path"] for r in out["results"] if r["file_path"]}
        assert any("auth.py" in f for f in files)
        assert out["lanes"] == {"fts5": True}


@pytest.mark.asyncio
async def test_search_scope_code(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("search", {"query": "verify", "scope": "code"})
        ).data
        for r in out["results"]:
            assert r["text_kind"] == "code"


def test_fts_query_tokens_splits_snake_case():
    from livespec_mcp.domain.rag import _fts_query_tokens

    assert _fts_query_tokens("index_project") == ["index", "project"]
    assert _fts_query_tokens("login user") == ["login", "user"]


@pytest.mark.asyncio
async def test_search_empty_query_is_error(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("search", {"query": "  "})).data
        assert out.get("isError") is True
        assert "query" in out["error"]


@pytest.mark.asyncio
async def test_embed_chunks_tool_removed(sample_repo):
    """Vector search / embed_chunks were removed — tool must not register."""
    async with Client(mcp) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
        assert "search" in names
        assert "embed_chunks" not in names


@pytest.mark.asyncio
async def test_chunks_skip_when_no_files_changed(sample_repo):
    async with Client(mcp) as c:
        first = (await c.call_tool("index_project", {})).data
        assert isinstance(first["chunks"], dict)
        assert "symbol_chunks" in first["chunks"]
        second = (await c.call_tool("index_project", {})).data
        assert second["chunks"] == {"skipped": "no file changes"}
