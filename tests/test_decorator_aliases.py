"""v0.13 P0: dual-decorator alias detection.

The plugin-framework pattern ``agentic_tool = mcp.tool if X else _noop``
hid the real decorator from `_has_entry_point_decorator` — the stored
decorator name is the alias, whose last segment is not in
`_ENTRY_POINT_DECORATOR_LASTSEG`. Surfaced as 22 false positives when
force-reindexing livespec-mcp itself (HANDOFF v0.12 item 0).
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_conditional_alias_protects_decorated_fn(workspace):
    """`agentic_tool = mcp.tool if X else _noop` + `@agentic_tool()` fn
    must not be dead-flagged."""
    (workspace / "tools.py").write_text(
        "def _noop_decorator(*a, **k):\n"
        "    def deco(fn):\n"
        "        return fn\n"
        "    return deco\n"
        "\n"
        "class _FakeMCP:\n"
        "    def tool(self, *a, **k):\n"
        "        return _noop_decorator()\n"
        "\n"
        "mcp = _FakeMCP()\n"
        "FLAG = True\n"
        "\n"
        "agentic_tool = mcp.tool if FLAG else _noop_decorator\n"
        "\n"
        "@agentic_tool()\n"
        "def my_registered_handler():\n"
        "    return 1\n"
        "\n"
        "def genuinely_dead():\n"
        "    return 2\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("my_registered_handler" in q for q in qnames), (
            f"conditional alias should protect decorated fn: {qnames}"
        )
        assert any("genuinely_dead" in q for q in qnames)


@pytest.mark.asyncio
async def test_plain_alias_inside_register_body(workspace):
    """Alias assigned INSIDE a function body (the real livespec pattern:
    `register()` assigns `mutation_tool = mcp.tool`) still protects."""
    (workspace / "plug.py").write_text(
        "def register(mcp):\n"
        "    mutation_tool = mcp.tool\n"
        "\n"
        "    @mutation_tool()\n"
        "    def inner_tool():\n"
        "        return 3\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("inner_tool" in q for q in qnames), (
            f"in-body alias should protect decorated fn: {qnames}"
        )


@pytest.mark.asyncio
async def test_non_entry_point_alias_still_flagged(workspace):
    """An alias whose source is NOT an entry-point name gives no
    protection — decorated fn with zero callers stays dead."""
    (workspace / "misc.py").write_text(
        "def some_helper(fn):\n"
        "    return fn\n"
        "\n"
        "plainwrap = some_helper\n"
        "\n"
        "@plainwrap\n"
        "def wrapped_but_dead():\n"
        "    return 4\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert any("wrapped_but_dead" in q for q in qnames), (
            f"non-entry-point alias must NOT protect: {qnames}"
        )
