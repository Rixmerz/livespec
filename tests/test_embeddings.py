"""End-to-end smoke for the embeddings lane (P1.2).

These tests are skipped if `fastembed` or `sqlite-vec` are not installed.
Run them with:

    uv pip install -e ".[embeddings]"
    uv run pytest -m embeddings

The first run downloads the two models (~600MB) into HuggingFace's cache.
"""

from __future__ import annotations

import importlib.util

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

pytestmark = pytest.mark.embeddings


def _has_extras() -> bool:
    return all(
        importlib.util.find_spec(mod) is not None
        for mod in ("fastembed", "sqlite_vec")
    )


pytestmark = [
    pytest.mark.embeddings,
    pytest.mark.skipif(not _has_extras(), reason="[embeddings] extras not installed"),
]


@pytest.mark.asyncio
async def test_rebuild_chunks_embed_yes_runs_fastembed(sample_repo):
    """rebuild_chunks(embed='yes') must actually populate chunk.embedded_at and
    write rows to chunk_vec_code/chunk_vec_text."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (await c.call_tool("rebuild_chunks", {"embed": "yes"})).data
        # Tool may take a while (model download on first run)
        assert "chunks_total" in result
        assert result["chunks_total"] > 0
        # Embed stats are returned when embeddings ran
        assert "embeddings" in result
        emb = result["embeddings"]
        assert emb.get("code_embedded", 0) + emb.get("text_embedded", 0) > 0


@pytest.mark.asyncio
async def test_search_uses_vector_lane(sample_repo):
    """search() reports `lanes.vector=True` once embeddings exist and returns
    results that include both lanes."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("rebuild_chunks", {"embed": "yes"})
        result = (
            await c.call_tool("search", {"query": "user authentication password", "limit": 5})
        ).data
        assert result["lanes"]["fts"] is True
        assert result["lanes"]["vector"] is True
        assert len(result["results"]) > 0
        assert all(r["score"] > 0 for r in result["results"])
