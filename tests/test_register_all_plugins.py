"""Server registers plugins at boot; visibility middleware gates the menu."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_mutation_tools_registered_when_plugins_forced():
    """conftest sets LIVESPEC_PLUGINS=all — full surface in list_tools."""
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
    assert "create_spec" in tools
    assert "link_spec_symbol" in tools
    assert "generate_docs" in tools
    assert "export_explorer" in tools
