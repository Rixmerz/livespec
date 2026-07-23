"""v0.21: Python callback-argument edges. A function passed as an argument is
invoked later by the callee — the extractor now emits a `callback_arg` ref so
it gets a real caller edge (not just a find_dead_code exemption), mirroring the
TS side. Conservative: only registration/scheduling callees or callback-ish
keyword names, so plain data args don't inflate the graph.

Regression origin: dogfooding livespec on itself flagged `index_project.
_do_reindex` (passed as `Watcher(on_reindex=_do_reindex)`) as false dead code.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_keyword_callback_to_constructor_creates_edge(workspace):
    """The exact dogfood case: `Runner(on_reindex=_cb)` inside a function makes
    `_cb` a callee of that function — a real who_calls edge, not dead."""
    (workspace / "mod.py").write_text(
        "def _cb():\n"
        "    return 1\n"
        "\n"
        "def setup():\n"
        "    return Runner(on_reindex=_cb, debounce=2)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        callers = (await c.call_tool("who_calls", {"qname": "mod._cb"})).data
        assert any(
            x["qualified_name"] == "mod.setup" for x in callers["callers"]
        ), f"setup should be a caller of _cb via on_reindex=: {callers}"
        dead = (await c.call_tool("find_dead_code", {})).data
        assert not any("_cb" in d["qualified_name"] for d in dead["dead_symbols"])


@pytest.mark.asyncio
async def test_nested_callback_inside_outer_function_creates_edge(workspace):
    """The precise dogfood regression: a callback DEFINED and PASSED inside the
    same outer function — `index_project` does `Watcher(on_reindex=_do_reindex)`
    where `_do_reindex` is a nested def. The outer function is the caller."""
    (workspace / "mod.py").write_text(
        "def index_project(watch=False):\n"
        "    if watch:\n"
        "        def _do_reindex():\n"
        "            return 1\n"
        "        return Watcher(on_reindex=_do_reindex, debounce=2)\n"
        "    return None\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        callers = (
            await c.call_tool(
                "who_calls", {"qname": "mod.index_project._do_reindex"}
            )
        ).data
        assert any(
            x["qualified_name"] == "mod.index_project" for x in callers["callers"]
        ), f"outer fn should call its nested callback via on_reindex=: {callers}"
        dead = (await c.call_tool("find_dead_code", {})).data
        assert not any(
            "_do_reindex" in d["qualified_name"] for d in dead["dead_symbols"]
        )


@pytest.mark.asyncio
async def test_atexit_register_positional_creates_edge(workspace):
    """`atexit.register(cleanup)` — a positional arg to a registration call."""
    (workspace / "mod.py").write_text(
        "import atexit\n"
        "def cleanup():\n"
        "    pass\n"
        "\n"
        "def install():\n"
        "    atexit.register(cleanup)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        callers = (await c.call_tool("who_calls", {"qname": "mod.cleanup"})).data
        assert any(x["qualified_name"] == "mod.install" for x in callers["callers"])


@pytest.mark.asyncio
async def test_key_function_keyword_creates_edge(workspace):
    """`sorted(xs, key=weight)` — the `key=` callback is genuinely invoked."""
    (workspace / "mod.py").write_text(
        "def weight(item):\n"
        "    return item.n\n"
        "\n"
        "def order(xs):\n"
        "    return sorted(xs, key=weight)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        callers = (await c.call_tool("who_calls", {"qname": "mod.weight"})).data
        assert any(x["qualified_name"] == "mod.order" for x in callers["callers"])


@pytest.mark.asyncio
async def test_plain_data_arg_does_not_create_spurious_edge(workspace):
    """Conservative boundary: a bare-Name arg passed POSITIONALLY to a
    non-registration callee is NOT treated as a callback, so no spurious edge
    even when the name collides with a defined function."""
    (workspace / "mod.py").write_text(
        "def handler():\n"
        "    return 1\n"
        "\n"
        "def dispatch(x):\n"
        "    return x\n"
        "\n"
        "def run(handler):\n"           # param named `handler`, shadows the fn
        "    return dispatch(handler)\n"  # positional, callee 'dispatch' not a reg verb
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        callers = (await c.call_tool("who_calls", {"qname": "mod.handler"})).data
        assert not any(
            x["qualified_name"] == "mod.run" for x in callers["callers"]
        ), f"run must NOT spuriously 'call' handler via a data arg: {callers}"
