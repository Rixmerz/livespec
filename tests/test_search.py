"""Hybrid search tool: FTS5 lane (always) + vector lane (extras-gated).

The FTS lane is exercised on every CI run. The vector lane requires
the [embeddings] extra and is marked so it can be opted into with
`pytest -m embeddings`.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.rag import have_embeddings, have_sqlite_vec
from livespec_mcp.server import mcp
from livespec_mcp.state import get_state


@pytest.mark.asyncio
async def test_index_populates_chunks(sample_repo):
    async with Client(mcp) as c:
        data = (await c.call_tool("index_project", {})).data
        assert "chunks" in data
        assert data["chunks"]["symbol_chunks"] >= 4
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
        assert out["lanes"]["fts5"] is True


@pytest.mark.asyncio
async def test_search_scope_code(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("search", {"query": "verify", "scope": "code"})
        ).data
        for r in out["results"]:
            assert r["text_kind"] == "code"


@pytest.mark.asyncio
async def test_search_empty_query_is_error(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("search", {"query": "  "})).data
        assert out.get("isError") is True
        assert "query" in out["error"]


@pytest.mark.asyncio
async def test_search_no_vector_lane_without_embed(sample_repo):
    """Vector lane stays inactive until embed_chunks runs."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("search", {"query": "login"})).data
        st = get_state()
        # Without embed=True, vec_chunks tables either don't exist or are empty.
        try:
            n = st.conn.execute("SELECT COUNT(*) c FROM chunk_vec_code").fetchone()["c"]
        except Exception:
            n = 0
        assert n == 0
        # Lane reporting reflects capability not data, so just ensure no crash.
        assert "vector" in out["lanes"]


@pytest.mark.asyncio
async def test_chunks_skip_when_no_files_changed(sample_repo):
    async with Client(mcp) as c:
        first = (await c.call_tool("index_project", {})).data
        assert isinstance(first["chunks"], dict)
        assert "symbol_chunks" in first["chunks"]
        second = (await c.call_tool("index_project", {})).data
        assert second["chunks"] == {"skipped": "no file changes"}


# ---------------------------------------------------------------------------
# Embeddings smoke — opt-in via `pytest -m embeddings`.
# Skipped on plain `pytest` runs because the first execution downloads
# ~200MB of model weights from HuggingFace (jinaai/jina-embeddings-v2-base-code
# + paraphrase-multilingual-mpnet-base-v2).
# ---------------------------------------------------------------------------

embeddings_available = pytest.mark.skipif(
    not have_embeddings(),
    reason="fastembed not installed; install with `pip install -e .[embeddings]`",
)


@pytest.mark.embeddings
@pytest.mark.asyncio
@embeddings_available
async def test_embed_chunks_populates_vec_tables(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("embed_chunks", {})).data
        assert out.get("isError") is not True
        assert out["code_embedded"] >= 1

        st = get_state()
        assert have_sqlite_vec(st.conn)
        n_code = st.conn.execute(
            "SELECT COUNT(*) c FROM chunk_vec_code"
        ).fetchone()["c"]
        assert n_code >= 1


@pytest.mark.embeddings
@pytest.mark.asyncio
@embeddings_available
async def test_hybrid_search_uses_vector_lane(sample_repo):
    """Semantic query that FTS5 alone would miss should still surface the
    auth code once vectors are populated."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"embed": True})
        out = (
            await c.call_tool(
                "search", {"query": "authenticate credentials"}
            )
        ).data
        assert out["lanes"]["vector"] is True
        assert out["count"] > 0
        files = {r["file_path"] for r in out["results"] if r["file_path"]}
        assert any("auth.py" in f for f in files)


@pytest.mark.embeddings
@pytest.mark.asyncio
@embeddings_available
async def test_embed_chunks_idempotent(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        first = (await c.call_tool("embed_chunks", {})).data
        second = (await c.call_tool("embed_chunks", {})).data
        assert first["code_embedded"] >= second["code_embedded"]
        assert second["code_embedded"] == 0
