"""v0.7 B5: find_symbol normalizes `::` and `/` to `.` so Rust-style
queries match across the indexer's qname format.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_find_symbol_matches_double_colon_query(workspace):
    """Rust qname like `mod.Type::method` should match `Type::method` query."""
    pkg = workspace / "src"
    pkg.mkdir()
    (pkg / "lib.rs").write_text(
        "pub struct Greeter;\n"
        "\n"
        "impl Greeter {\n"
        "    pub fn greet() -> i32 { 42 }\n"
        "    pub fn shout() -> i32 { 99 }\n"
        "}\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})

        # Query with :: should resolve to mod::Type::method qnames
        out = (await c.call_tool("find_symbol", {"query": "Greeter::greet"})).data
        qnames = {m["qualified_name"] for m in out["matches"]}
        assert any("Greeter::greet" in q for q in qnames), (
            f"Greeter::greet should match: {qnames}"
        )

        # Plain Greeter still works (existing behavior)
        out = (await c.call_tool("find_symbol", {"query": "Greeter"})).data
        qnames = {m["qualified_name"] for m in out["matches"]}
        assert any("Greeter" in q for q in qnames)


@pytest.mark.asyncio
async def test_find_symbol_matches_dot_against_double_colon_qname(workspace):
    """Even if the user types `Type.method`, it should resolve to a qname
    that uses `::` (Rust impl method separator)."""
    pkg = workspace / "src"
    pkg.mkdir()
    (pkg / "lib.rs").write_text(
        "pub struct API;\n"
        "impl API {\n"
        "    pub fn handle() -> i32 { 1 }\n"
        "}\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_symbol", {"query": "API.handle"})).data
        qnames = {m["qualified_name"] for m in out["matches"]}
        # The stored qname uses ::; `API.handle` query should still find it
        assert any("API::handle" in q for q in qnames), (
            f"API.handle query should reach API::handle qname: {qnames}"
        )


@pytest.mark.asyncio
async def test_find_symbol_path_separator_normalized(workspace):
    """A query like `pkg/auth/login` should reach `pkg.auth.login` qnames."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "auth.py").write_text(
        "def login(u, p):\n    return True\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_symbol", {"query": "auth/login"})).data
        qnames = {m["qualified_name"] for m in out["matches"]}
        assert any("pkg.auth.login" in q for q in qnames), (
            f"auth/login query should reach pkg.auth.login qname: {qnames}"
        )


@pytest.mark.asyncio
async def test_find_symbol_pages_and_counts(workspace):
    """A `limit` with no count made a truncated answer look complete."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "handlers.py").write_text(
        "".join(f"def handle_{i}():\n    return {i}\n\n" for i in range(7))
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})

        first = (await c.call_tool(
            "find_symbol", {"query": "handle_", "limit": 3}
        )).data
        assert first["count"] == 7, first
        assert len(first["matches"]) == 3
        assert first["next_cursor"] == 3

        second = (await c.call_tool(
            "find_symbol", {"query": "handle_", "limit": 3, "cursor": first["next_cursor"]}
        )).data
        assert second["count"] == 7
        assert second["next_cursor"] == 6

        last = (await c.call_tool(
            "find_symbol", {"query": "handle_", "limit": 3, "cursor": 6}
        )).data
        assert last["next_cursor"] is None
        assert len(last["matches"]) == 1

        seen = {
            m["qualified_name"]
            for page in (first, second, last)
            for m in page["matches"]
        }
        assert len(seen) == 7  # the three pages cover the set exactly once


@pytest.mark.asyncio
async def test_paginated_tools_agree_on_the_word_for_a_total(workspace):
    """`count` means the same thing everywhere — `list_specs` said `total`."""
    (workspace / "a.py").write_text("def f():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "One rule"})

        specs = (await c.call_tool("list_specs", {})).data
        assert specs["count"] == specs["total"] == 1

        summary = (await c.call_tool("list_specs", {"summary_only": True})).data
        assert summary["count"] == summary["total"] == 1

        for tool in ("find_dead_code", "find_endpoints", "find_orphan_tests"):
            payload = (await c.call_tool(tool, {})).data
            assert "count" in payload, (tool, sorted(payload))
