"""Closure-capture port to TS/JS + Rust (v0.14, backlog item 11, open
since v0.8): a nested named function whose name is referenced in the
parent's body (passed to a constructor, assigned to a variable) is
reachable as a callback even with zero call edges — must not be
dead-flagged. Go is exempt by design: no named nested functions exist.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.analysis import _treesitter_used_nested_def_names


def test_unit_ts_constructor_capture(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text(
        "function start() {\n"
        "  function onEvent() { return 1; }\n"
        "  function unusedInner() { return 2; }\n"
        "  const w = new Watcher(onEvent);\n"
        "  return w;\n"
        "}\n"
    )
    assert _treesitter_used_nested_def_names(str(f), "typescript") == frozenset({"onEvent"})


def test_unit_rust_assignment_capture(tmp_path):
    f = tmp_path / "a.rs"
    f.write_text(
        "fn start() {\n"
        "    fn on_event() -> i32 { 1 }\n"
        "    fn unused_inner() -> i32 { 2 }\n"
        "    let cb: fn() -> i32 = on_event;\n"
        "    cb();\n"
        "}\n"
    )
    assert _treesitter_used_nested_def_names(str(f), "rust") == frozenset({"on_event"})


def test_unit_nested_internal_ref_does_not_count(tmp_path):
    # A nested fn referencing ITSELF (recursion) or being referenced only
    # from inside another nested fn is not a parent-body use.
    f = tmp_path / "a.ts"
    f.write_text(
        "function start() {\n"
        "  function helperA() { return helperB(); }\n"
        "  function helperB() { return 2; }\n"
        "  return 0;\n"
        "}\n"
    )
    assert _treesitter_used_nested_def_names(str(f), "typescript") == frozenset()


@pytest.mark.asyncio
async def test_find_dead_code_ts_closure_callback(workspace):
    (workspace / "app.ts").write_text(
        "class Watcher {\n"
        "  cb: () => number;\n"
        "  constructor(cb: () => number) { this.cb = cb; }\n"
        "  fire() { return this.cb(); }\n"
        "}\n"
        "\n"
        "function start() {\n"
        "  function onEvent() { return 1; }\n"
        "  function unusedInner() { return 2; }\n"
        "  const w = new Watcher(onEvent);\n"
        "  return w.fire();\n"
        "}\n"
        "\n"
        "export const boot = () => start();\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool(
            "find_dead_code", {"include_non_python": True}
        )).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert "app.start.onEvent" not in qnames, qnames
        assert "app.start.unusedInner" in qnames, qnames


@pytest.mark.asyncio
async def test_find_dead_code_rust_closure_callback(workspace):
    (workspace / "lib.rs").write_text(
        "fn start() -> i32 {\n"
        "    fn on_event() -> i32 { 1 }\n"
        "    fn unused_inner() -> i32 { 2 }\n"
        "    let cb: fn() -> i32 = on_event;\n"
        "    cb()\n"
        "}\n"
        "\n"
        "pub fn boot() -> i32 { start() }\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool(
            "find_dead_code", {"include_non_python": True}
        )).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert "lib.start.on_event" not in qnames, qnames
        assert "lib.start.unused_inner" in qnames, qnames
