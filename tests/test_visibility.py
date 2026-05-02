"""v0.7 B4: visibility extraction + find_dead_code skips public items.

The a large Rust monorepo Rust monorepo flagged 23K symbols as dead — most were `pub`
items called across crate boundaries. With visibility-aware filtering,
the dead list shrinks to truly-orphan symbols.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import extract
from livespec_mcp.server import mcp


def test_rust_visibility_extracted(tmp_path: Path):
    src = (
        "pub struct A;\n"
        "struct B;\n"
        "pub(crate) struct C;\n"
        "pub(super) struct D;\n"
        "impl A {\n"
        "    pub fn pub_method() -> i32 { 1 }\n"
        "    fn private_method() -> i32 { 2 }\n"
        "    pub(crate) fn crate_method() -> i32 { 3 }\n"
        "}\n"
    )
    p = tmp_path / "lib.rs"
    p.write_text(src)
    _, result = extract(p, src, tmp_path)
    # Multiple symbols can share `name` (struct A + impl A both emit 'A').
    # Look up by (qualified_name, start_line) for unambiguous picks.
    by_qname_kind: dict[tuple[str, str], str | None] = {
        (s.qualified_name, s.kind): s.visibility for s in result.symbols
    }
    # Pick the struct entries (kind=class), not the impl-block aggregator.
    structs = {
        qn.split(".")[-1]: vis
        for (qn, kind), vis in by_qname_kind.items()
        if kind == "class" and "::" not in qn
    }
    # The struct + impl-aggregator share the same qname; use the first
    # (struct) which has the actual visibility modifier. Fall back via
    # min-line lookup.
    structs_by_first_line: dict[str, str | None] = {}
    for s in sorted(result.symbols, key=lambda x: x.start_line):
        if s.kind == "class" and "::" not in s.qualified_name:
            name = s.name
            if name not in structs_by_first_line:
                structs_by_first_line[name] = s.visibility
    assert structs_by_first_line["A"] == "pub"
    assert structs_by_first_line["B"] == "private"
    assert structs_by_first_line["C"] == "pub(crate)"
    assert structs_by_first_line["D"] == "pub(super)"

    methods_by_name = {
        s.name: s.visibility for s in result.symbols if s.kind == "method"
    }
    assert methods_by_name["pub_method"] == "pub"
    assert methods_by_name["private_method"] == "private"
    assert methods_by_name["crate_method"] == "pub(crate)"


@pytest.mark.asyncio
async def test_find_dead_code_skips_pub_rust_items(workspace):
    """A `pub fn` that nobody calls in-project is NOT flagged as dead by
    default — Rust pub items have callers across crate boundaries that the
    in-project graph can't see. Plain `fn` (private) without a caller IS
    flagged."""
    src = workspace / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "pub fn public_api() -> i32 { 1 }\n"
        "\n"
        "fn truly_private_dead() -> i32 { 2 }\n"
        "\n"
        "pub(crate) fn crate_only_dead() -> i32 { 3 }\n"
        "\n"
        "fn used() -> i32 { 4 }\n"
        "\n"
        "fn caller() -> i32 { used() }\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}

        assert "src.lib.public_api" not in qnames, (
            f"pub fn must NOT be flagged as dead: {qnames}"
        )
        assert "src.lib.truly_private_dead" in qnames, (
            f"private fn with no caller MUST be flagged: {qnames}"
        )
        # pub(crate) is callable only within this scope; no caller -> flagged
        assert "src.lib.crate_only_dead" in qnames, (
            f"pub(crate) fn with no in-project caller MUST be flagged: {qnames}"
        )

        # include_public=True surfaces the pub fn too
        out2 = (
            await c.call_tool(
                "find_dead_code",
                {"include_public": True, "include_non_python": True},
            )
        ).data
        qnames2 = {d["qualified_name"] for d in out2["dead_symbols"]}
        assert "src.lib.public_api" in qnames2
