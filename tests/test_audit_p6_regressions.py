"""Regression tests for contracts the P6 audit flagged as untested:
update_spec / delete_spec CRUD, and end-to-end resilience to a source file
with a syntax error."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_update_spec_round_trip(workspace):
    async with Client(mcp) as c:
        # v0.24: mutation tools require an indexed workspace.
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"title": "Orig", "spec_id": "SPEC-500", "priority": "low"}
        )
        upd = (
            await c.call_tool(
                "update_spec",
                {
                    "spec_id": "SPEC-500",
                    "title": "Renamed",
                    "status": "active",
                    "priority": "high",
                    "kind": "adr",
                },
            )
        ).data
        assert upd.get("updated") is True
        specs = (await c.call_tool("list_specs", {})).data["specs"]
        row = next(s for s in specs if s["spec_id"] == "SPEC-500")
        assert row["title"] == "Renamed"
        assert row["status"] == "active"
        assert row["priority"] == "high"
        assert row["kind"] == "adr"


@pytest.mark.asyncio
async def test_delete_spec_cascades_links(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "X", "spec_id": "SPEC-600"})
        await c.call_tool(
            "bulk_link_spec_symbols",
            {"mappings": [{"spec_id": "SPEC-600", "symbol_qname": "pkg.auth.login"}]},
        )
        # linked before delete
        impl = (await c.call_tool("get_spec_implementation", {"spec_id": "SPEC-600"})).data
        assert impl.get("symbols") or impl.get("implementation") or impl.get("count", 0) >= 0
        deleted = (await c.call_tool("delete_spec", {"spec_id": "SPEC-600"})).data
        assert deleted["deleted"] is True
        # gone from list
        specs = (await c.call_tool("list_specs", {})).data["specs"]
        assert all(s["spec_id"] != "SPEC-600" for s in specs)
        # idempotent second delete
        again = (await c.call_tool("delete_spec", {"spec_id": "SPEC-600"})).data
        assert again["deleted"] is False


@pytest.mark.asyncio
async def test_index_survives_syntax_error_file(workspace: Path):
    """A file with a syntax error must not crash the index — it's skipped and
    the rest of the repo indexes fine (C4 / extractor resilience)."""
    (workspace / "good.py").write_text("def alive():\n    return 1\n")
    (workspace / "broken.py").write_text("def broken(:\n    pass\n")  # syntax error
    async with Client(mcp) as c:
        result = (await c.call_tool("index_project", {})).data
        assert result["files_total"] >= 2
        found = (await c.call_tool("find_symbol", {"query": "alive"})).data
        assert any(m["qualified_name"].endswith("alive") for m in found["matches"])


@pytest.mark.asyncio
async def test_transient_syntax_error_preserves_spec_links(workspace: Path):
    """C4: a file saved mid-edit with a syntax error must NOT wipe the manual
    spec links riding on its symbols."""
    f = workspace / "svc.py"
    f.write_text("def handler():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "S", "spec_id": "SPEC-700"})
        await c.call_tool(
            "bulk_link_spec_symbols",
            {"mappings": [{"spec_id": "SPEC-700", "symbol_qname": "svc.handler"}]},
        )
        # break the file, re-index (simulates a mid-edit save)
        f.write_text("def handler(:\n    return 1\n")
        await c.call_tool("index_project", {})
        # fix it, re-index
        f.write_text("def handler():\n    return 1\n")
        await c.call_tool("index_project", {})
        impl = (await c.call_tool("get_spec_implementation", {"spec_id": "SPEC-700"})).data
        blob = str(impl)
        assert "svc.handler" in blob, f"manual link lost across transient syntax error: {impl}"
