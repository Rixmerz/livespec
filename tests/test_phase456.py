"""Tests for Phase 4 (RAG/search), Phase 5 (docs), Phase 6 (suggest_rf_links).

These tests do NOT require fastembed/sqlite-vec — the FTS5 lane is enough to
exercise hybrid search and ranking. Doc generation is tested with a stub
sampling handler that mimics a Claude client.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from mcp.types import (
    CreateMessageRequestParams,
    CreateMessageResult,
    SamplingMessage,
    TextContent,
)

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_rebuild_chunks_and_search(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        rc = (await c.call_tool("rebuild_chunks", {})).data
        assert rc["chunks_total"] >= 4

        res = (await c.call_tool("search", {"query": "login user password", "limit": 5})).data
        assert res["lanes"]["fts"] is True
        assert len(res["results"]) > 0
        # Top hit should be the login function
        top = res["results"][0]
        assert top["source_type"] in ("symbol", "requirement")


@pytest.mark.asyncio
async def test_suggest_rf_links(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_requirement",
            {
                "title": "User login flow",
                "description": "User authenticates by password verification",
                "rf_id": "RF-100",
            },
        )
        await c.call_tool("rebuild_chunks", {})
        sug = (
            await c.call_tool(
                "suggest_rf_links",
                {"rf_id": "RF-100", "limit": 5, "min_score": -100},
            )
        ).data
        assert sug["rf_id"] == "RF-100"
        names = {c["qualified_name"] for c in sug["candidates"]}
        assert any("login" in n or "verify" in n or "API" in n for n in names), names


@pytest.mark.asyncio
async def test_generate_docs_for_symbol_with_stub_sampling(sample_repo):
    async def sampling_handler(messages, params, context):
        # Mimic the LLM completing a docstring
        return "## pkg.auth.login\n\nAutentica un usuario delegando en `verify`."

    async with Client(mcp, sampling_handler=sampling_handler) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "generate_docs_for_symbol",
                {"identifier": "pkg.auth.login"},
            )
        ).data
        assert result["target"] == "pkg.auth.login"
        assert result["length"] > 10

        listed = (await c.call_tool("list_docs", {"target_type": "symbol"})).data
        assert any(d["target_key"] == "pkg.auth.login" for d in listed["docs"])

        # Resource read
        res = await c.read_resource("doc://symbol/pkg.auth.login")
        assert "verify" in res[0].text


@pytest.mark.asyncio
async def test_detect_stale_docs(sample_repo):
    async def sampling_handler(messages, params, context):
        return "doc v1"

    async with Client(mcp, sampling_handler=sampling_handler) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("generate_docs_for_symbol", {"identifier": "pkg.auth.login"})

        # Mutate the source so body_hash drifts
        login_path = sample_repo / "pkg" / "auth.py"
        login_path.write_text(
            login_path.read_text() + "\n\ndef extra():\n    return 0\n"
        )
        # Re-index to refresh body hashes
        await c.call_tool("index_project", {"force": True})
        # The login function body wasn't actually edited; only a new function was
        # appended, so login's body_hash should remain. Now mutate login itself.
        text = login_path.read_text().replace("return verify", "return verify  # changed")
        login_path.write_text(text)
        await c.call_tool("index_project", {"force": True})

        stale = (await c.call_tool("detect_stale_docs", {"target_type": "symbol"})).data
        targets = {s["target"] for s in stale["stale"]}
        assert "pkg.auth.login" in targets


@pytest.mark.asyncio
async def test_export_documentation(sample_repo, tmp_path):
    async def sampling_handler(messages, params, context):
        return "doc body"

    async with Client(mcp, sampling_handler=sampling_handler) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("generate_docs_for_symbol", {"identifier": "pkg.auth.login"})
        out = (
            await c.call_tool(
                "export_documentation", {"format": "json", "out_subdir": "export"}
            )
        ).data
        assert out["exported"] >= 1
        # Json file should exist on disk
        from pathlib import Path

        p = Path(out["path"])
        assert p.exists()
        data = json.loads(p.read_text())
        assert any(d["target_key"] == "pkg.auth.login" for d in data)
