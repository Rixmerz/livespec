"""Tier-B noise reductions: dead_code / orphan / audit / grep / search."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.rag import _fts_match_expr
from livespec_mcp.server import mcp
from livespec_mcp.tools.analysis import _grep_compile_pattern, _package_marker_is_emptyish


def test_fts_phrase_mode():
    expr, mode = _fts_match_expr('"create user" login')
    assert mode == "mixed"
    assert '"create user"' in expr or '"create" "user"' in expr or "create" in expr


def test_grep_redos_falls_back_to_literal():
    regex, mode, hint = _grep_compile_pattern("(a+)+b")
    assert regex is None
    assert mode == "literal"
    assert hint


def test_package_marker_emptyish(tmp_path):
    p = tmp_path / "index.ts"
    p.write_text("export * from './a';\n")
    assert _package_marker_is_emptyish(tmp_path, "index.ts")
    p.write_text("export function real() { return 1; }\n")
    assert not _package_marker_is_emptyish(tmp_path, "index.ts")


@pytest.mark.asyncio
async def test_dead_code_protects_express_imported_handler(workspace):
    """Import-mapped Express handler must not be dead when routed."""
    (workspace / "controllers").mkdir()
    (workspace / "controllers" / "details.ts").write_text(
        "export default function details(req: any, res: any) {\n"
        "  res.json({ ok: true });\n"
        "}\n"
    )
    (workspace / "routes.ts").write_text(
        "import express from 'express';\n"
        "import details from './controllers/details';\n"
        "\n"
        "const app = express();\n"
        "function wrap(h: any) { return h; }\n"
        "app.get('/details', wrap(details));\n"
        "\n"
        "function neverUsed() { return 1; }\n"
        "export default app;\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "find_dead_code",
                {"include_non_python": True, "include_public": True},
            )
        ).data
    qnames = {d["qualified_name"] for d in out.get("dead_symbols", [])}
    assert not any("details" in q and "neverUsed" not in q for q in qnames if q.endswith("details") or ".details" in q), qnames
    # default export may be named details
    assert not any(q.endswith("details") for q in qnames), qnames
    assert any(q.endswith("neverUsed") for q in qnames), qnames


@pytest.mark.asyncio
async def test_dead_code_reports_fs_routing_skipped(workspace):
    (workspace / "islands").mkdir()
    (workspace / "islands" / "Counter.tsx").write_text(
        "export default function Counter() { return null; }\n"
    )
    (workspace / "lib.ts").write_text("export function unused() { return 1; }\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "find_dead_code",
                {"include_non_python": True, "include_public": True},
            )
        ).data
    assert out.get("skipped_fs_routing_count", 0) >= 1


@pytest.mark.asyncio
async def test_orphan_skips_harness_and_fixtures(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("def ping():\n    return 'ok'\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "conftest.py").write_text(
        "def make_user():\n    return {}\n"
    )
    (workspace / "tests" / "test_harness.py").write_text(
        "from fastmcp import Client\n"
        "\n"
        "def test_via_client():\n"
        "    Client(mcp)\n"
        "    assert True\n"
    )
    (workspace / "tests" / "test_lonely.py").write_text(
        "def test_lonely():\n"
        "    assert True\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_orphan_tests", {})).data
        full = (
            await c.call_tool(
                "find_orphan_tests",
                {"include_harness": True, "include_fixtures": True},
            )
        ).data
    assert out["harness_skipped_count"] >= 1
    assert out["fixture_skipped_count"] >= 1
    qnames = {o["qualified_name"] for o in out["orphan_tests"]}
    assert any("test_lonely" in q for q in qnames), out
    assert not any("test_via_client" in q for q in qnames), out
    assert any("confidence" in o for o in out["orphan_tests"])
    assert full["count"] >= out["count"]


@pytest.mark.asyncio
async def test_audit_coverage_summary_sample_and_cursors(workspace):
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for i in range(6):
        (pkg / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        summary = (await c.call_tool("audit_coverage", {"summary_only": True})).data
        assert "modules_truly_orphan_sample" in summary
        page = (
            await c.call_tool(
                "audit_coverage",
                {"limit": 2, "cursors": {"modules_without_spec": 2}},
            )
        ).data
        assert len(page["modules_without_spec"]) <= 2


@pytest.mark.asyncio
async def test_grep_match_mode_and_per_file_limit(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "grep_in_indexed_files",
                {"pattern": "def ", "per_file_limit": 1, "limit": 50},
            )
        ).data
        assert out["match_mode"] in ("regex", "literal")
        by_file: dict[str, int] = {}
        for m in out["matches"]:
            by_file[m["file_path"]] = by_file.get(m["file_path"], 0) + 1
        assert all(v <= 1 for v in by_file.values()), by_file


@pytest.mark.asyncio
async def test_search_index_fresh_and_query_mode(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("search", {"query": "login"})).data
        assert out["index_fresh"] is True
        assert out["query_mode"] in ("tokens", "phrase", "mixed")
        assert out["lanes"] == {"fts5": True}
