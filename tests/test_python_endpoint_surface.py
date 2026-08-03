"""Default find_endpoints is HTTP-ish — Click/FastMCP need framework=."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

MIXED = (
    "from fastapi import FastAPI\n"
    "import click\n"
    "from mcp.server.fastmcp import FastMCP\n"
    "\n"
    "app = FastAPI()\n"
    "mcp = FastMCP('demo')\n"
    "\n"
    "@app.get('/health')\n"
    "def health():\n"
    "    return {'ok': True}\n"
    "\n"
    "@click.command()\n"
    "def cli():\n"
    "    pass\n"
    "\n"
    "@mcp.tool()\n"
    "def my_tool() -> str:\n"
    "    return 'x'\n"
)


@pytest.mark.asyncio
async def test_default_endpoints_exclude_click_and_fastmcp(workspace):
    (workspace / "app.py").write_text(MIXED)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        default = (await c.call_tool("find_endpoints", {})).data
        click = (await c.call_tool("find_endpoints", {"framework": "click"})).data
        fastmcp = (await c.call_tool("find_endpoints", {"framework": "fastmcp"})).data
        fastapi = (await c.call_tool("find_endpoints", {"framework": "fastapi"})).data
    def_q = {e["qualified_name"] for e in default["endpoints"]}
    assert any(q.endswith("health") for q in def_q), def_q
    assert not any(q.endswith("cli") for q in def_q), def_q
    assert not any(q.endswith("my_tool") for q in def_q), def_q
    click_q = {e["qualified_name"] for e in click["endpoints"]}
    assert any(q.endswith("cli") for q in click_q), click_q
    mcp_q = {e["qualified_name"] for e in fastmcp["endpoints"]}
    assert any(q.endswith("my_tool") for q in mcp_q), mcp_q
    fa_q = {e["qualified_name"] for e in fastapi["endpoints"]}
    assert any(q.endswith("health") for q in fa_q), fa_q
