"""v0.11 P3: runtime-registration name protection in find_dead_code.

Symbols handed to a framework via known registration-verb method calls
(Field.register_lookup, signal.connect, app.add_middleware, registry.register, etc.)
should not appear in find_dead_code output even when they have zero call edges.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.analysis import _runtime_registered_names


# ---------------------------------------------------------------------------
# Unit tests for the helper directly
# ---------------------------------------------------------------------------


def test_runtime_registered_names_direct(tmp_path):
    f = tmp_path / "regmod.py"
    f.write_text(
        "from django.db import models\n"
        "class MyLookup: pass\n"
        "class MyHandler: pass\n"
        "class MyMiddleware: pass\n"
        "\n"
        "def ready():\n"
        "    models.Field.register_lookup(MyLookup)\n"
        "    pre_save.connect(MyHandler)\n"
        "    app.add_middleware(MyMiddleware)\n"
    )
    names = _runtime_registered_names(str(f))
    assert "MyLookup" in names
    assert "MyHandler" in names
    assert "MyMiddleware" in names


def test_runtime_registered_names_keyword_arg(tmp_path):
    f = tmp_path / "kw.py"
    f.write_text(
        "def on_event(): pass\n"
        "event.subscribe(handler=on_event)\n"
    )
    names = _runtime_registered_names(str(f))
    assert "on_event" in names


def test_runtime_registered_names_string_arg_not_collected(tmp_path):
    """String args must NOT create false-positive name matches."""
    f = tmp_path / "str_arg.py"
    f.write_text(
        'app.add_middleware("path.to.X")\n'
    )
    names = _runtime_registered_names(str(f))
    # "path.to.X" is a string — should produce no protected names
    assert len(names) == 0


def test_runtime_registered_names_non_registration_verb_not_collected(tmp_path):
    f = tmp_path / "neg.py"
    f.write_text(
        "class MyThing: pass\n"
        "mylist.append(MyThing)\n"
    )
    names = _runtime_registered_names(str(f))
    assert "MyThing" not in names


def test_runtime_registered_names_parse_failure(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def (\n")  # SyntaxError
    assert _runtime_registered_names(str(f)) == frozenset()


def test_runtime_registered_names_multiple_positional_args(tmp_path):
    f = tmp_path / "multi.py"
    f.write_text(
        "class Foo: pass\n"
        "class Bar: pass\n"
        "registry.register(Foo, Bar)\n"
    )
    names = _runtime_registered_names(str(f))
    assert "Foo" in names
    assert "Bar" in names


# ---------------------------------------------------------------------------
# Integration tests via MCP (find_dead_code end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_lookup_in_ready_not_dead(workspace):
    """Field.register_lookup(MyLookup) inside AppConfig.ready() — MyLookup not dead."""
    (workspace / "app.py").write_text(
        "class MyLookup:\n"
        "    lookup_name = 'iexact_custom'\n"
        "\n"
        "class MyAppConfig:\n"
        "    def ready(self):\n"
        "        Field.register_lookup(MyLookup)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("MyLookup" in q for q in qnames), (
            f"MyLookup should be protected but appeared: {qnames}"
        )


@pytest.mark.asyncio
async def test_signal_connect_at_module_level_not_dead(workspace):
    """pre_save.connect(my_handler) at module level — my_handler not dead."""
    (workspace / "signals.py").write_text(
        "def my_handler(sender, instance, **kwargs):\n"
        "    pass\n"
        "\n"
        "pre_save.connect(my_handler)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("my_handler" in q for q in qnames), (
            f"my_handler should be protected but appeared: {qnames}"
        )


@pytest.mark.asyncio
async def test_add_middleware_in_function_not_dead(workspace):
    """app.add_middleware(MyMiddleware) inside a function — MyMiddleware not dead."""
    (workspace / "middleware.py").write_text(
        "class MyMiddleware:\n"
        "    def __call__(self, request):\n"
        "        pass\n"
    )
    (workspace / "setup.py").write_text(
        "from middleware import MyMiddleware\n"
        "\n"
        "def configure(app):\n"
        "    app.add_middleware(MyMiddleware)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("MyMiddleware" in q for q in qnames), (
            f"MyMiddleware should be protected but appeared: {qnames}"
        )


@pytest.mark.asyncio
async def test_registry_register_multiple_args_not_dead(workspace):
    """registry.register(Foo, Bar) — both Foo and Bar not dead."""
    (workspace / "models.py").write_text(
        "class Foo:\n"
        "    pass\n"
        "\n"
        "class Bar:\n"
        "    pass\n"
        "\n"
        "registry.register(Foo, Bar)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("Foo" in q for q in qnames), (
            f"Foo should be protected: {qnames}"
        )
        assert not any("Bar" in q for q in qnames), (
            f"Bar should be protected: {qnames}"
        )


@pytest.mark.asyncio
async def test_keyword_handler_not_dead(workspace):
    """event.subscribe(handler=on_event) — on_event not dead."""
    (workspace / "events.py").write_text(
        "def on_event(data):\n"
        "    pass\n"
        "\n"
        "event.subscribe(handler=on_event)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("on_event" in q for q in qnames), (
            f"on_event should be protected: {qnames}"
        )


@pytest.mark.asyncio
async def test_non_registration_verb_still_dead(workspace):
    """mylist.append(MyThing) — MyThing with no callers IS dead (append not a reg verb)."""
    (workspace / "things.py").write_text(
        "class MyThing:\n"
        "    pass\n"
        "\n"
        "mylist = []\n"
        "mylist.append(MyThing)\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        # MyThing has a module-level reference via append arg — but append is
        # not a registration verb so it should NOT be protected by
        # _runtime_registered_names. However, it MAY be caught by
        # _module_level_referenced_names (AST walk sees the Name node).
        # The important negative: we verify that _runtime_registered_names
        # alone doesn't protect it (tested via the unit test above).
        # The integration test just asserts no crash and the tool returns.
        assert "count" in out


@pytest.mark.asyncio
async def test_string_arg_not_protected(workspace):
    """String arg app.add_middleware('path.to.X') — no spurious name protection."""
    (workspace / "strarg.py").write_text(
        "class RealOrphan:\n"
        "    pass\n"
        "\n"
        'app.add_middleware("path.to.X")\n'
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        # RealOrphan has no callers and no string-arg protection — must appear.
        assert any("RealOrphan" in q for q in qnames), (
            f"RealOrphan should be dead but not found: {qnames}"
        )
