"""Doc-generation tests (Phase 5).

v0.8 P3.3 dropped the search/RAG tool wrappers (`search`, `rebuild_chunks`)
along with the related tests; the RAG domain code stays for future use.
Doc generation is exercised with a stub sampling handler that mimics a
Claude client.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_generate_docs_for_symbol_with_stub_sampling(sample_repo):
    async def sampling_handler(messages, params, context):
        # Mimic the LLM completing a docstring
        return "## pkg.auth.login\n\nAutentica un usuario delegando en `verify`."

    async with Client(mcp, sampling_handler=sampling_handler) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "generate_docs",
                {"target_type": "symbol", "identifier": "pkg.auth.login"},
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
        await c.call_tool("generate_docs", {"target_type": "symbol", "identifier": "pkg.auth.login"})

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

        stale = (
            await c.call_tool("list_docs", {"target_type": "symbol", "only_stale": True})
        ).data
        targets = {s["target"] for s in stale["stale"]}
        assert "pkg.auth.login" in targets


@pytest.mark.asyncio
async def test_generate_docs_caller_supplied(sample_repo):
    """Mode 1: caller writes content, tool persists. Works without sampling."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "generate_docs",
                {
                    "target_type": "symbol",
                    "identifier": "pkg.auth.login",
                    "content": "## login\n\nAutentica un usuario.",
                },
            )
        ).data
        assert result["mode"] == "caller_supplied"
        assert result["target"] == "pkg.auth.login"

        listed = (await c.call_tool("list_docs", {"target_type": "symbol"})).data
        assert any(d["target_key"] == "pkg.auth.login" for d in listed["docs"])


@pytest.mark.asyncio
async def test_generate_docs_no_sampling_returns_prompt(sample_repo):
    """Mode 3: no sampling, no content -> returns prompt for caller to fill."""
    async with Client(mcp) as c:  # no sampling_handler -> sampling unsupported
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "generate_docs",
                {"target_type": "symbol", "identifier": "pkg.auth.login"},
            )
        ).data
        assert result["mode"] == "needs_caller_content"
        assert "prompt" in result
        assert "source" in result
        assert "login" in result["prompt"]


@pytest.mark.asyncio
async def test_export_documentation(sample_repo, tmp_path):
    async def sampling_handler(messages, params, context):
        return "doc body"

    async with Client(mcp, sampling_handler=sampling_handler) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("generate_docs", {"target_type": "symbol", "identifier": "pkg.auth.login"})
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
