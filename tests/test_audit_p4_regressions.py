"""Regression tests for the audit batch P4 tools-layer fixes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import get_state
from livespec_mcp.tools.specs import _next_spec_id


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, check=True)


@pytest.mark.asyncio
async def test_git_diff_impact_detects_rename(sample_repo: Path):
    """M2: a renamed file's OLD path (with the indexed symbols) is included."""
    ws = sample_repo
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@e.com")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "init")
    _git(ws, "mv", "pkg/auth.py", "pkg/authn.py")
    _git(ws, "commit", "-q", "-am", "rename auth -> authn")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "git_diff_impact", {"base_ref": "HEAD~1", "head_ref": "HEAD"}
            )
        ).data
        changed = result["changed_files"]
        assert "pkg/auth.py" in changed and "pkg/authn.py" in changed, changed


def test_next_spec_id_uses_max_not_last_inserted(workspace):
    """M9: next id is MAX+1, robust to out-of-order inserts."""
    st = get_state(create=True)
    pid = st.project_id
    st.conn.execute(
        "INSERT INTO spec(project_id, spec_id, title) VALUES (?, 'SPEC-005', 't')", (pid,)
    )
    st.conn.execute(
        "INSERT INTO spec(project_id, spec_id, title) VALUES (?, 'SPEC-002', 't')", (pid,)
    )
    st.conn.commit()
    assert _next_spec_id(st.conn, pid) == "SPEC-006"


@pytest.mark.asyncio
async def test_create_spec_duplicate_returns_mcp_error(workspace):
    async with Client(mcp) as c:
        # v0.24: mutation tools require an indexed workspace (WorkspaceErrorMiddleware
        # rejects them clean rather than let get_state() silently create a DB).
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "A", "spec_id": "SPEC-100"})
        res = (
            await c.call_tool("create_spec", {"title": "B", "spec_id": "SPEC-100"})
        ).data
        assert res.get("isError") is True
        assert "already exists" in res["error"]


@pytest.mark.asyncio
async def test_find_symbol_limit_clamped_and_like_escaped(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # negative limit must not become unbounded
        res = (await c.call_tool("find_symbol", {"query": "login", "limit": -1})).data
        assert "matches" in res
        # a query with a LIKE wildcard matches literally (no symbol named with %)
        res2 = (await c.call_tool("find_symbol", {"query": "log%in"})).data
        assert res2["matches"] == []


@pytest.mark.asyncio
async def test_list_specs_has_implementation_filtered_in_sql(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "Linked", "spec_id": "SPEC-201"})
        await c.call_tool(
            "bulk_link_spec_symbols",
            {"mappings": [{"spec_id": "SPEC-201", "symbol_qname": "pkg.auth.login"}]},
        )
        await c.call_tool("create_spec", {"title": "Orphan", "spec_id": "SPEC-202"})
        linked = (await c.call_tool("list_specs", {"has_implementation": True})).data
        ids = {s["spec_id"] for s in linked["specs"]}
        assert "SPEC-201" in ids and "SPEC-202" not in ids


@pytest.mark.asyncio
async def test_bulk_link_rejects_invalid_relation(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "X", "spec_id": "SPEC-301"})
        res = (
            await c.call_tool(
                "bulk_link_spec_symbols",
                {
                    "mappings": [
                        {
                            "spec_id": "SPEC-301",
                            "symbol_qname": "pkg.auth.login",
                            "relation": "implement",  # typo
                        }
                    ]
                },
            )
        ).data
        assert res["failed"] >= 1
        assert any("invalid relation" in (r.get("error") or "") for r in res["results"])


@pytest.mark.asyncio
async def test_analyze_impact_spec_branch_paginates(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "S", "spec_id": "SPEC-401"})
        await c.call_tool(
            "bulk_link_spec_symbols",
            {"mappings": [{"spec_id": "SPEC-401", "symbol_qname": "pkg.auth.login"}]},
        )
        res = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "spec", "target": "SPEC-401", "summary_only": True},
            )
        ).data
        assert "counts" in res
        assert "implementing_symbols" in res["counts"]


@pytest.mark.asyncio
async def test_quick_orient_surfaces_scratch_note(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "agent_scratch", {"qname": "pkg.auth.login", "note": "hot path"}
        )
        res = (await c.call_tool("quick_orient", {"qname": "pkg.auth.login"})).data
        assert res["scratch_note"] and res["scratch_note"]["note"] == "hot path"


@pytest.mark.asyncio
async def test_workspace_error_returns_shaped_mcp_error():
    """M8: a missing/invalid workspace surfaces {error,isError,hint}, not a
    raw protocol error. Uses a real bad path (no conftest binding needed —
    this test does not request the `workspace` fixture)."""
    async with Client(mcp) as c:
        r = await c.call_tool(
            "find_symbol", {"query": "x", "workspace": "/no/such/dir/xyz123"}
        )
        assert r.data.get("isError") is True
        assert "hint" in r.data
