"""Tests for v0.4 P2 aggregator tools: find_dead_code, audit_coverage,
find_orphan_tests."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_find_dead_code_basic(workspace):
    """A function nobody calls and that has no RF link is reported."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "lib.py").write_text(
        "def used():\n"
        "    return 1\n"
        "\n"
        "def caller():\n"
        "    return used()\n"
        "\n"
        "def dead_func():\n"
        "    # never invoked, no RF link\n"
        "    return 'orphan'\n"
        "\n"
        "def main():\n"
        "    return caller()\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data

    qnames = {d["qualified_name"] for d in out["dead_symbols"]}
    assert "pkg.lib.dead_func" in qnames, f"dead_func not flagged: {out}"
    # `used` IS called by `caller` -> not dead
    assert "pkg.lib.used" not in qnames
    # `caller` and `main` have callers (themselves or each other) — main is also dead
    # actually main has no caller in-project, so it would be flagged. That's OK.


@pytest.mark.asyncio
async def test_find_dead_code_skips_entry_points(workspace):
    """Symbols under tests/, scripts/, bin/ are not flagged even with no callers."""
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_thing.py").write_text(
        "def test_one():\n"
        "    assert True\n"
    )
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "deploy.py").write_text(
        "def run():\n"
        "    return 0\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data

    qnames = {d["qualified_name"] for d in out["dead_symbols"]}
    assert not any("test_one" in q for q in qnames), (
        f"test_one should be skipped (entry point): {qnames}"
    )
    assert not any("scripts" in d["file_path"] for d in out["dead_symbols"]), (
        f"scripts/* should be skipped: {out}"
    )


@pytest.mark.asyncio
async def test_audit_coverage_signals(workspace):
    """All three coverage signals report under expected conditions."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "linked.py").write_text(
        '"""@rf:RF-001"""\n'
        "def implementer():\n"
        '    """@rf:RF-001"""\n'
        "    return 1\n"
    )
    (pkg / "unlinked.py").write_text(
        "def alone():\n"
        "    return 0\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Two RFs: one with implementer, one without
        await c.call_tool(
            "create_requirement",
            {"rf_id": "RF-001", "title": "Linked"},
        )
        await c.call_tool(
            "create_requirement",
            {"rf_id": "RF-002", "title": "Orphan"},
        )
        # Re-scan so RF-001 picks up the @rf: annotation
        await c.call_tool("scan_rf_annotations", {})

        out = (await c.call_tool("audit_coverage", {})).data

    assert any("unlinked" in p for p in out["modules_without_rf"]), (
        f"pkg/unlinked.py should appear in modules_without_rf: {out}"
    )
    rfs_no_impl_ids = {r["rf_id"] for r in out["rfs_without_implementation"]}
    assert "RF-002" in rfs_no_impl_ids, (
        f"RF-002 should be reported as without implementation: {out}"
    )
    # P0.A1: new fields exist and partition `modules_without_rf`
    assert isinstance(out.get("modules_implicitly_covered"), list)
    assert isinstance(out.get("modules_truly_orphan"), list)
    # Union of the two splits == modules_without_rf
    union = set(out["modules_implicitly_covered"]) | set(out["modules_truly_orphan"])
    assert union == set(out["modules_without_rf"]), (
        f"split must partition modules_without_rf: {out}"
    )


@pytest.mark.asyncio
async def test_audit_coverage_transitive_split(workspace):
    """P0.A1: a data-layer file with no @rf: should appear in
    `modules_implicitly_covered` (because an rf-linked caller reaches it),
    not `modules_truly_orphan`."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # Data layer — NO @rf: annotation
    (pkg / "store.py").write_text(
        "def query():\n"
        "    return [1, 2, 3]\n"
    )
    # API — annotated, calls into store
    (pkg / "api.py").write_text(
        "from pkg.store import query\n"
        "\n"
        "def handle():\n"
        '    """@rf:RF-100"""\n'
        "    return query()\n"
    )
    # Truly orphan — no @rf:, nobody calls it either
    (pkg / "junk.py").write_text(
        "def standalone():\n"
        "    return 'nobody cares'\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_requirement", {"rf_id": "RF-100", "title": "API surface"}
        )
        await c.call_tool("scan_rf_annotations", {})
        out = (await c.call_tool("audit_coverage", {})).data

    implicit = set(out["modules_implicitly_covered"])
    truly = set(out["modules_truly_orphan"])

    assert any("store.py" in p for p in implicit), (
        f"pkg/store.py should be implicitly covered (called by api.handle): {out}"
    )
    assert any("junk.py" in p for p in truly), (
        f"pkg/junk.py should be truly orphan (no callers, no @rf:): {out}"
    )
    assert not any("junk.py" in p for p in implicit), (
        f"junk.py is NOT implicitly covered: {out}"
    )


@pytest.mark.asyncio
async def test_find_dead_code_skips_decorated_handlers(workspace):
    """v0.5 P1: a function decorated with a framework entry-point marker
    (route/command/fixture/tool/...) must NOT be flagged as dead even when
    nobody in the project calls it directly."""
    pkg = workspace / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "routes.py").write_text(
        "app = object()\n"
        "\n"
        "@app.route('/users')\n"
        "def list_users():\n"
        "    return []\n"
        "\n"
        "@app.before_request\n"
        "def setup():\n"
        "    pass\n"
        "\n"
        "@something.task\n"
        "def background_job():\n"
        "    return None\n"
        "\n"
        "def truly_dead_helper():\n"
        "    return 'nobody calls me'\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data

    qnames = {d["qualified_name"] for d in out["dead_symbols"]}
    assert "app.routes.list_users" not in qnames, (
        f"@app.route handler must not be flagged as dead: {qnames}"
    )
    assert "app.routes.setup" not in qnames, (
        f"@app.before_request handler must not be flagged: {qnames}"
    )
    assert "app.routes.background_job" not in qnames, (
        f"@*.task handler must not be flagged: {qnames}"
    )
    assert "app.routes.truly_dead_helper" in qnames, (
        f"plain helper with no callers SHOULD be flagged: {qnames}"
    )


@pytest.mark.asyncio
async def test_find_endpoints_all_and_per_framework(workspace):
    """v0.5 P1: find_endpoints surfaces decorated symbols, with a
    per-framework filter."""
    pkg = workspace / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(
        "app = object()\n"
        "router = object()\n"
        "\n"
        "@app.route('/users')\n"
        "def list_users():\n"
        "    return []\n"
        "\n"
        "@router.get('/items')\n"
        "def get_items():\n"
        "    return []\n"
        "\n"
        "@click.command()\n"
        "def cli_run():\n"
        "    pass\n"
        "\n"
        "@pytest.fixture\n"
        "def db():\n"
        "    return None\n"
        "\n"
        "def plain():\n"
        "    return 1\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})

        # No framework filter -> all entry-point decorators
        all_eps = (await c.call_tool("find_endpoints", {})).data
        all_qnames = {e["qualified_name"] for e in all_eps["endpoints"]}
        assert "app.main.list_users" in all_qnames
        assert "app.main.get_items" in all_qnames
        assert "app.main.cli_run" in all_qnames
        assert "app.main.db" in all_qnames
        assert "app.main.plain" not in all_qnames

        # framework='click' -> only the click command
        click_eps = (await c.call_tool("find_endpoints", {"framework": "click"})).data
        click_qnames = {e["qualified_name"] for e in click_eps["endpoints"]}
        assert "app.main.cli_run" in click_qnames
        assert "app.main.list_users" not in click_qnames
        assert "app.main.db" not in click_qnames

        # framework='pytest' -> only fixtures
        pyt_eps = (await c.call_tool("find_endpoints", {"framework": "pytest"})).data
        pyt_qnames = {e["qualified_name"] for e in pyt_eps["endpoints"]}
        assert pyt_qnames == {"app.main.db"}


@pytest.mark.asyncio
async def test_find_orphan_tests(workspace):
    """A test file whose calls only reach other tests is reported orphan."""
    (workspace / "src").mkdir()
    (workspace / "src" / "__init__.py").write_text("")
    (workspace / "src" / "real.py").write_text(
        "def production_fn():\n"
        "    return 1\n"
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_helper.py").write_text(
        "def test_helper():\n"
        "    return None\n"
    )
    (workspace / "tests" / "test_connected.py").write_text(
        "from src.real import production_fn\n"
        "\n"
        "def test_real():\n"
        "    assert production_fn() == 1\n"
    )
    (workspace / "tests" / "test_orphan.py").write_text(
        "from tests.test_helper import test_helper\n"
        "\n"
        "def test_only_uses_other_tests():\n"
        "    test_helper()\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_orphan_tests", {})).data

    qnames = {o["qualified_name"] for o in out["orphan_tests"]}
    assert any(
        "test_only_uses_other_tests" in q for q in qnames
    ), f"orphan test not flagged: {out}"
    assert not any(
        "test_real" in q for q in qnames
    ), f"connected test wrongly flagged: {out}"
