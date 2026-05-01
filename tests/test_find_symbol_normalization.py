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
