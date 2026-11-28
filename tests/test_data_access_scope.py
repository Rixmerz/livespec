"""`find_data_access`: `path` scope, test files left out by default, and a
`summary_only` inventory that carries the unresolved-model COUNT, not the list."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures" / "data_access"

FILES = {
    "app/billing/repo.py": (
        "def save_invoice(conn):\n"
        '    conn.execute("INSERT INTO invoices (id) VALUES (1)")\n\n'
        "def load_invoice(conn):\n"
        '    return conn.execute("SELECT * FROM invoices")\n'
    ),
    "app/users/repo.py": (
        'def load_user(conn):\n    return conn.execute("SELECT * FROM users")\n'
    ),
    "tests/test_repo.py": (
        "def test_fixture(conn):\n"
        '    conn.execute("INSERT INTO invoices (id) VALUES (2)")\n'
        '    conn.execute("INSERT INTO test_only_rows (id) VALUES (2)")\n'
    ),
}


async def _setup(c: Client, root: Path) -> None:
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    await c.call_tool("index_project", {})


async def _fda(c: Client, **args: Any) -> dict[str, Any]:
    return (await c.call_tool("find_data_access", args)).data


@pytest.mark.asyncio
async def test_inventory_excludes_tests_and_scopes_to_a_path(workspace):
    async with Client(mcp) as c:
        await _setup(c, workspace)
        default = await _fda(c)
        assert {r["table"] for r in default["tables"]} == {"invoices", "users"}
        assert default["counts"]["filtered_out"] == {"tests": 2, "test_only_tables": 1}
        assert default["tests_excluded"] == 2
        withtests = await _fda(c, include_tests=True)
        assert {r["table"] for r in withtests["tables"]} == {"invoices", "users", "test_only_rows"}
        assert "filtered_out" not in withtests["counts"]

        scoped = await _fda(c, path="app/billing")
        assert [r["table"] for r in scoped["tables"]] == ["invoices"]
        assert scoped["path"] == "app/billing"
        assert (scoped["tables"][0]["read"], scoped["tables"][0]["write"]) == (1, 1)
        # a file works as a path too, and a prefix is a directory, not a substring
        assert [r["table"] for r in (await _fda(c, path="app/users/repo.py"))["tables"]] == ["users"]
        assert (await _fda(c, path="app/bill"))["tables"] == []


@pytest.mark.asyncio
async def test_table_mode_honours_path_and_tests(workspace):
    async with Client(mcp) as c:
        await _setup(c, workspace)
        out = await _fda(c, table="invoices")
        assert out["counts"]["total"] == 2 and out["tests_excluded"] == 1
        out = await _fda(c, table="invoices", include_tests=True)
        assert out["counts"]["total"] == 3
        out = await _fda(c, table="invoices", path="app/users")
        assert out["counts"]["total"] == 0


@pytest.mark.asyncio
async def test_summary_only_inventory_drops_the_unresolved_models_list(tmp_path: Path):
    ws = tmp_path / "flask"
    shutil.copytree(FIXTURES / "flask", ws)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(ws)})
        full = await _fda(c, workspace=str(ws))
        n = full["counts"]["unresolved_models"]
        assert n >= 4 and len(full["unresolved_models"]) == n
        summary = await _fda(c, workspace=str(ws), summary_only=True)
        assert summary["counts"]["unresolved_models"] == n
        assert "unresolved_models" not in summary and "tables" not in summary
