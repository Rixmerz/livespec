"""Server boots with all plugins for multi-tenant workspace= calls."""

from __future__ import annotations

from fastmcp import Client

from livespec_mcp.server import mcp


import pytest


@pytest.mark.asyncio
async def test_mutation_tools_registered():
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
    assert "create_requirement" in tools
    assert "link_rf_symbol" in tools
    assert "generate_docs" in tools
