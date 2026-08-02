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
            "create_spec", {"spec_id": "auth-user-login", "title": "Feature"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "auth-user-login", "symbol_qname": "pkg.feature.implementer"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "auth-user-login" in by_id, f"auth-user-login missing from spec_coverage: {out['spec_coverage']}"
    entry = by_id["auth-user-login"]
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
async def test_spec_coverage_harness_tests_link_credits_all_implements(workspace):
    """relation='tests' on a real test-file symbol credits every implements
    symbol — MCP Client suites call tools by string name (no static edges)."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def implementer_a():\n"
        "    return 1\n"
        "\n"
        "def implementer_b():\n"
        "    return 2\n"
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_feature.py").write_text(
        "def test_via_mcp_client():\n"
        "    return True  # would call_tool('feature') by string — no edges\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-HARNESS", "title": "Harness"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-HARNESS", "symbol_qname": "pkg.feature.implementer_a"},
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-HARNESS", "symbol_qname": "pkg.feature.implementer_b"},
        )
        await c.call_tool(
            "link_spec_symbol",
            {
                "spec_id": "SPEC-HARNESS",
                "symbol_qname": "tests.test_feature.test_via_mcp_client",
                "relation": "tests",
            },
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    entry = by_id["SPEC-HARNESS"]
    assert entry["test_coverage_ratio"] == 1.0, entry
    assert entry["coverage_source"] == "explicit", entry
    assert entry["tested_symbols"] == 2, entry


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
            "create_spec", {"spec_id": "auth-session", "title": "Harness-tested"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "auth-session", "symbol_qname": "pkg.feature.implementer"},
        )
        # Explicit tests link to the impl symbol itself (the only signal).
        await c.call_tool(
            "link_spec_symbol",
            {
                "spec_id": "auth-session",
                "symbol_qname": "pkg.feature.implementer",
                "relation": "tests",
            },
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "auth-session" in by_id, f"auth-session missing: {out['spec_coverage']}"
    entry = by_id["auth-session"]
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
            "create_spec", {"spec_id": "untested-feature", "title": "Untested"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "untested-feature", "symbol_qname": "pkg.feature.untested_impl"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "untested-feature" in by_id, f"untested-feature missing: {out['spec_coverage']}"
    entry = by_id["untested-feature"]
    assert entry["test_coverage_ratio"] == 0.0, (
        f"no test reaches the impl, no explicit link → ratio 0: {entry}"
    )
    assert entry["coverage_source"] == "none", (
        f"coverage_source should be none: {entry}"
    )
    assert entry["total_symbols"] == 1
    assert entry["tested_symbols"] == 0


@pytest.mark.asyncio
async def test_spec_coverage_from_lcov_report_without_static_call_edge(workspace):
    """LCOV covers an implementation even when static test reachability cannot."""
    src = workspace / "src"
    src.mkdir()
    source = (
        "export function reportCovered(): number {\n"
        "  return 42;\n"
        "}\n"
    )
    (src / "feature.ts").write_text(source)
    coverage = workspace / "coverage"
    coverage.mkdir()
    (coverage / "lcov.info").write_text(
        "TN:\n"
        f"SF:{src / 'feature.ts'}\n"
        "DA:2,1\n"
        "end_of_record\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "report-covered", "title": "Report-covered"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "report-covered", "symbol_qname": "src.feature.reportCovered"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    entry = {r["spec_id"]: r for r in out["spec_coverage"]}["report-covered"]
    assert entry["test_coverage_ratio"] == 1.0, entry
    assert entry["coverage_source"] == "report", entry


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
            "create_spec", {"spec_id": "tested-feature", "title": "Tested"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "tested-feature", "symbol_qname": "pkg.feature.implementer"},
        )
        await c.call_tool(
            "link_spec_symbol",
            {
                "spec_id": "tested-feature",
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
        r["spec_id"] == "tested-feature" and r["test_count"] == 1
        for r in out["spec_test_coverage"]
    ), f"tested-feature must still be in spec_test_coverage: {out['spec_test_coverage']}"
    # New auto-derived block coexists.
    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "tested-feature" in by_id
