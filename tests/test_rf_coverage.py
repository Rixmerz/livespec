"""v0.15: auto-derived per-Spec test coverage from the call graph.

`audit_coverage` (and the shared `compute_coverage` helper) now derive
per-Spec test coverage automatically: a test function whose forward call
graph reaches an Spec's `implements` symbol within `_TEST_REACH_DEPTH` hops
credits that Spec — no hand-linked `relation='tests'` row required. Explicit
links still count (the MCP-Client indirection blind-spot). These tests pin
the three contributing sources: derived, explicit, none.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_spec_coverage_derived_from_call_graph(workspace):
    """A test function that DIRECTLY calls an Spec's implementing symbol
    credits coverage automatically (no `relation='tests'` link) — proves
    auto-derivation works for normal projects."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def implementer():\n"
        "    return 1\n"
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_feature.py").write_text(
        "from pkg.feature import implementer\n"
        "\n"
        "def test_implementer():\n"
        "    assert implementer() == 1\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-001", "title": "Feature"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-001", "symbol_qname": "pkg.feature.implementer"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "SPEC-001" in by_id, f"SPEC-001 missing from spec_coverage: {out['spec_coverage']}"
    entry = by_id["SPEC-001"]
    assert entry["test_coverage_ratio"] > 0, (
        f"derived coverage should be > 0: {entry}"
    )
    assert entry["total_symbols"] == 1
    assert entry["tested_symbols"] == 1
    assert entry["coverage_source"] in ("derived", "both"), (
        f"coverage_source should include derived: {entry}"
    )
    # Rollups present and reflect the credited Spec.
    assert out["specs_with_derived_test_coverage"] >= 1
    assert out["avg_test_coverage"] > 0
    assert out["counts"]["specs_with_derived_test_coverage"] >= 1
    assert out["counts"]["avg_test_coverage"] > 0


@pytest.mark.asyncio
async def test_spec_coverage_explicit_link_without_call_edge(workspace):
    """An impl symbol with ONLY an explicit `relation='tests'` link and NO
    call edge from any test is still credited via 'explicit' — the
    indirection blind-spot (e.g. MCP-Client suites that dispatch by string
    name, leaving zero static call edges to the impl)."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # Impl + a test symbol that does NOT call the impl (no call edge).
    (pkg / "feature.py").write_text(
        "def implementer():\n"
        "    return 1\n"
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_feature.py").write_text(
        "def test_via_harness():\n"
        "    # dispatches by string name through an in-process harness —\n"
        "    # no static call edge to pkg.feature.implementer\n"
        "    return True\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-002", "title": "Harness-tested"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-002", "symbol_qname": "pkg.feature.implementer"},
        )
        # Explicit tests link to the impl symbol itself (the only signal).
        await c.call_tool(
            "link_spec_symbol",
            {
                "spec_id": "SPEC-002",
                "symbol_qname": "pkg.feature.implementer",
                "relation": "tests",
            },
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "SPEC-002" in by_id, f"SPEC-002 missing: {out['spec_coverage']}"
    entry = by_id["SPEC-002"]
    assert entry["test_coverage_ratio"] > 0, (
        f"explicit-link coverage should be > 0: {entry}"
    )
    assert entry["coverage_source"] == "explicit", (
        f"coverage_source should be explicit (no call edge from a test): {entry}"
    )


@pytest.mark.asyncio
async def test_spec_coverage_zero_when_nothing_reaches_impl(workspace):
    """An Spec with impl symbols that no test reaches and no explicit link →
    ratio 0.0, coverage_source 'none'."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def untested_impl():\n"
        "    return 42\n"
    )
    # A test file exists but exercises something unrelated.
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_other.py").write_text(
        "def test_other():\n"
        "    assert True\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-003", "title": "Untested"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-003", "symbol_qname": "pkg.feature.untested_impl"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "SPEC-003" in by_id, f"SPEC-003 missing: {out['spec_coverage']}"
    entry = by_id["SPEC-003"]
    assert entry["test_coverage_ratio"] == 0.0, (
        f"no test reaches the impl, no explicit link → ratio 0: {entry}"
    )
    assert entry["coverage_source"] == "none", (
        f"coverage_source should be none: {entry}"
    )
    assert entry["total_symbols"] == 1
    assert entry["tested_symbols"] == 0


@pytest.mark.asyncio
async def test_spec_coverage_backward_compat_explicit_fields_intact(workspace):
    """The v0.8 explicit-link fields (`spec_test_coverage` /
    `counts.specs_with_linked_tests`) must remain exactly as before alongside
    the new auto-derived `spec_coverage` block."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def implementer():\n"
        "    return 1\n"
        "\n"
        "def test_runner():\n"
        "    return implementer() == 1\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-200", "title": "Tested"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-200", "symbol_qname": "pkg.feature.implementer"},
        )
        await c.call_tool(
            "link_spec_symbol",
            {
                "spec_id": "SPEC-200",
                "symbol_qname": "pkg.feature.test_runner",
                "relation": "tests",
            },
        )
        out = (await c.call_tool("audit_coverage", {})).data

    # Backward-compat: explicit-link signal still present and correct.
    assert out["counts"]["specs_with_linked_tests"] == 1, (
        f"explicit specs_with_linked_tests must be intact: {out['counts']}"
    )
    assert any(
        r["spec_id"] == "SPEC-200" and r["test_count"] == 1
        for r in out["spec_test_coverage"]
    ), f"SPEC-200 must still be in spec_test_coverage: {out['spec_test_coverage']}"
    # New auto-derived block coexists.
    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "SPEC-200" in by_id
