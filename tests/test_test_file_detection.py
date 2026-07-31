"""Test-file detection across languages (`_is_test_file_path`).

The detector used to be Python/Go/Rust only: `tests/`, `test_*`, `*_test.py`,
`*_test.go`. On a TypeScript repo that means ZERO test files detected, which
silently corrupted two tools — `find_orphan_tests` reported `count: 0` (reads
as "no orphans", actually means "no tests found") and `compute_spec_test_
coverage` derived 0.0 for every Spec because its BFS seed set was empty.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.analysis import (
    _is_test_file_path,
    _is_test_scaffold_path,
    filter_api_endpoints,
)

# A body long enough to clear the `_is_infrastructure` "<5 lines" wrapper
# filter, so these fixture symbols actually reach the PageRank ranking.
_BODY = "\n".join(f"  const v{i} = {i};" for i in range(8))


@pytest.mark.parametrize(
    "path",
    [
        # --- JS/TS conventions the detector was blind to ---
        "src/foo.test.ts",
        "src/foo.spec.tsx",
        "__tests__/foo.js",
        "src/__tests__/foo.jsx",
        "src/test/helpers.ts",
        "src/foo.test.js",
        "src/foo.spec.js",
        "src/foo.test.mjs",
        "src/foo.spec.cjs",
        "src/foo_spec.ts",
        # --- Ruby ---
        "spec/foo_spec.rb",
        "spec/support/helper.rb",
        # --- Java / Maven layout ---
        "src/test/java/com/acme/FooTest.java",
        # --- already matching before this change (must not regress) ---
        "tests/test_x.py",
        "tests/helpers.py",
        "pkg/tests/thing.py",
        "test_x.py",
        "x_test.go",
        "x_test.py",
        "pkg/x_test.rs",
        # --- windows separators / leading slash normalization ---
        "src\\foo.test.ts",
        "/src/foo.test.ts",
    ],
)
def test_is_test_file_path_true(path):
    assert _is_test_file_path(path), f"{path!r} should be detected as a test file"


@pytest.mark.parametrize(
    "path",
    [
        # Substring near-misses — the reason matching is segment/suffix
        # anchored rather than `"test" in path`.
        "src/contest/index.ts",
        "src/latest.ts",
        "src/protest.ts",
        "src/attestation.ts",
        "src/latest/index.ts",
        "src/greatest_hits.py",
        "src/testing.ts",  # no `test_` prefix, no `.test.` suffix
        # `specs/` (plural) is the OpenSpec/docs convention, not a test one.
        "openspec/specs/inbound.ts",
        # Plain source
        "src/index.ts",
        "lib/foo.py",
        "",
    ],
)
def test_is_test_file_path_false(path):
    assert not _is_test_file_path(path), f"{path!r} must NOT be detected as a test file"


def _write_ts_repo(workspace):
    """A minimal TS repo: one impl symbol, one test symbol calling it, one
    orphan test symbol calling nothing."""
    src = workspace / "src"
    src.mkdir()
    (src / "core.ts").write_text(
        f"export function chargeCard(amount: number) {{\n{_BODY}\n  return amount * 2;\n}}\n"
    )
    (src / "core.test.ts").write_text(
        "import { chargeCard } from './core';\n"
        f"export function createMockDb() {{\n{_BODY}\n  return chargeCard(1) === 2;\n}}\n"
        f"export function orphanHelper() {{\n{_BODY}\n  return 7;\n}}\n"
    )


@pytest.mark.asyncio
async def test_find_orphan_tests_jest_anonymous_honest_zero(workspace):
    """Jest-style anonymous `test()` leaves only module symbols — count=0
    must carry diagnostics so agents don't read it as 'no test files'."""
    (workspace / "src").mkdir()
    (workspace / "src" / "math.ts").write_text(
        "export function add(a: number, b: number) { return a + b; }\n"
    )
    (workspace / "src" / "math.test.ts").write_text(
        "import { add } from './math';\n"
        'test("adds", () => { expect(add(1, 2)).toBe(3); });\n'
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_orphan_tests", {"summary_only": True})).data
    assert out["count"] == 0
    assert out["test_files_count"] >= 1
    assert out["test_function_symbols"] == 0
    assert "Jest" in (out.get("hint") or "") or "anonymous" in (out.get("hint") or "")


@pytest.mark.asyncio
async def test_find_orphan_tests_sees_typescript_tests(workspace):
    """`find_orphan_tests` must not report a flat zero on a TS repo.

    `orphanHelper` lives in `*.test.ts` and calls nothing → orphan.
    `createMockDb` reaches production code → not an orphan. Before the fix
    neither was even recognised as a test, so count was 0 for the wrong
    reason.
    """
    _write_ts_repo(workspace)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_orphan_tests", {})).data

    qnames = {o["qualified_name"] for o in out["orphan_tests"]}
    assert out["count"] == 1, f"expected exactly 1 orphan, got {out}"
    assert any(q.endswith("orphanHelper") for q in qnames), qnames
    assert not any(q.endswith("createMockDb") for q in qnames), qnames


@pytest.mark.asyncio
async def test_derived_spec_coverage_credits_typescript_test(workspace):
    """A Spec whose implementing symbol is reached from a `*.test.ts` file
    must get a non-zero DERIVED coverage ratio (no explicit `tests` link)."""
    _write_ts_repo(workspace)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"spec_id": "SPEC-900", "title": "Charging"})
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-900", "symbol_qname": "src.core.chargeCard"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    assert "SPEC-900" in by_id, out["spec_coverage"]
    assert by_id["SPEC-900"]["test_coverage_ratio"] > 0, by_id["SPEC-900"]
    assert by_id["SPEC-900"]["coverage_source"] == "derived", by_id["SPEC-900"]
    assert out["counts"]["specs_with_derived_test_coverage"] >= 1, out["counts"]
    # No explicit relation='tests' row was created, so the explicit-link
    # count stays 0 — the two counts measure different mechanisms.
    assert out["counts"]["specs_with_linked_tests"] == 0, out["counts"]


@pytest.mark.asyncio
async def test_overview_top_symbols_excludes_test_symbols(workspace):
    """Test scaffolding must not rank as the architectural core.

    Pins the mechanism, not just the absence: `createMockDb` is gone from
    `top_symbols` AND accounted for in `test_symbols_filtered`.
    """
    _write_ts_repo(workspace)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        ov = (await c.call_tool("get_project_overview", {})).data

    top = {s["qualified_name"] for s in ov["top_symbols"]}
    assert not any("createMockDb" in q for q in top), f"test helper leaked: {top}"
    assert not any("orphanHelper" in q for q in top), f"test helper leaked: {top}"
    assert any("chargeCard" in q for q in top), f"real symbol missing: {top}"
    filtered = ov["test_symbols_filtered"]
    assert any("createMockDb" in q for q in filtered), filtered
    assert any("orphanHelper" in q for q in filtered), filtered


@pytest.mark.asyncio
async def test_overview_test_symbols_filtered_is_capped(workspace):
    """`test_symbols_filtered` must stay bounded.

    The ranking loop only breaks at 20 KEPT symbols. With fewer than 20
    non-test survivors it scans the whole PageRank ordering, so without a
    cap every test symbol in the project would land in a response that has
    no pagination and was fixed-size by design.
    """
    src = workspace / "src"
    src.mkdir()
    (src / "core.ts").write_text(
        f"export function chargeCard(amount: number) {{\n{_BODY}\n  return amount * 2;\n}}\n"
    )
    (src / "core.test.ts").write_text(
        "\n".join(
            f"export function mockHelper{i}() {{\n{_BODY}\n  return {i};\n}}"
            for i in range(25)
        )
        + "\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        ov = (await c.call_tool("get_project_overview", {})).data

    # Precondition: fewer than 20 survivors, so the loop ran to exhaustion.
    assert len(ov["top_symbols"]) < 20, ov["top_symbols"]
    assert len(ov["test_symbols_filtered"]) <= 20, len(ov["test_symbols_filtered"])
    assert ov["test_symbols_filtered"], "cap must not empty the field"


# --- sibling detectors: the same blindness, two more places -----------------
#
# `_is_test_scaffold_path` (endpoint filtering) and the nested `_is_test_path`
# in specs.py each carried their own narrower copy of the heuristic, so both
# stayed Python-only after the main one was widened. Verified live before
# fixing: find_endpoints on a real Hono backend listed POST /register, /login,
# /refresh and /logout from `src/routes/v1/auth.test.ts` alongside the genuine
# routes of the same name in `auth.ts` — nothing distinguished them.

@pytest.mark.parametrize(
    "path",
    [
        "src/routes/v1/auth.test.ts",
        "src/routes/v1/auth.spec.ts",
        "__tests__/routes.js",
        "src/test/helpers.ts",
    ],
)
def test_scaffold_detector_now_sees_js_ts_tests(path):
    assert _is_test_scaffold_path(path)


@pytest.mark.parametrize(
    "path",
    ["conftest.py", "conftest_extra.py", "src/fixtures/data.py", "src/test_helpers/x.py"],
)
def test_scaffold_detector_keeps_its_pytest_only_extras(path):
    # These are scaffolding but not test *files*; delegating must not drop them.
    assert _is_test_scaffold_path(path)


@pytest.mark.parametrize("path", ["src/routes/v1/auth.ts", "src/contest/index.ts", "src/latest.ts"])
def test_scaffold_detector_still_rejects_real_sources(path):
    assert not _is_test_scaffold_path(path)


def test_find_endpoints_drops_routes_declared_in_a_test_file():
    endpoints = [
        {"file_path": "src/routes/v1/auth.test.ts", "hono_path": "/login"},
        {"file_path": "src/routes/v1/auth.ts", "hono_path": "/login"},
    ]
    kept = [e["file_path"] for e in filter_api_endpoints(endpoints, "hono")]
    assert kept == ["src/routes/v1/auth.ts"]
