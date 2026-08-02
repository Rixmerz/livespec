"""Regression tests for the audit batch P4 tools-layer fixes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.specs import is_legacy_numeric_spec_id


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


def test_legacy_numeric_spec_id_detector():
    assert is_legacy_numeric_spec_id("SPEC-001")
    assert is_legacy_numeric_spec_id("AUTH-12")
    # Multi-segment store ids (BE-RF-102) are not the removed single PREFIX-NNN shape.
    assert not is_legacy_numeric_spec_id("BE-RF-102")
    assert not is_legacy_numeric_spec_id("auth-user-login")
    assert not is_legacy_numeric_spec_id("auth-user-login-2")


@pytest.mark.asyncio
async def test_create_spec_rejects_legacy_numeric_id(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        res = (
            await c.call_tool("create_spec", {"title": "A", "spec_id": "SPEC-100"})
        ).data
        assert res.get("isError") is True
        assert "PREFIX-NNN" in res["error"]


@pytest.mark.asyncio
async def test_create_spec_duplicate_returns_mcp_error(workspace):
    async with Client(mcp) as c:
        # v0.24: mutation tools require an indexed workspace (WorkspaceErrorMiddleware
        # rejects them clean rather than let get_state() silently create a DB).
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "A", "spec_id": "auth-a"})
        res = (
            await c.call_tool("create_spec", {"title": "B", "spec_id": "auth-a"})
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
        await c.call_tool("create_spec", {"title": "Linked", "spec_id": "spec-linked"})
        await c.call_tool(
            "bulk_link_spec_symbols",
            {"mappings": [{"spec_id": "spec-linked", "symbol_qname": "pkg.auth.login"}]},
        )
        await c.call_tool("create_spec", {"title": "Orphan", "spec_id": "spec-orphan"})
        linked = (await c.call_tool("list_specs", {"has_implementation": True})).data
        ids = {s["spec_id"] for s in linked["specs"]}
        assert "spec-linked" in ids and "spec-orphan" not in ids


@pytest.mark.asyncio
async def test_bulk_link_rejects_invalid_relation(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "X", "spec_id": "spec-bulk"})
        res = (
            await c.call_tool(
                "bulk_link_spec_symbols",
                {
                    "mappings": [
                        {
                            "spec_id": "spec-bulk",
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
        await c.call_tool("create_spec", {"title": "S", "spec_id": "spec-impact"})
        await c.call_tool(
            "bulk_link_spec_symbols",
            {"mappings": [{"spec_id": "spec-impact", "symbol_qname": "pkg.auth.login"}]},
        )
        res = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "spec", "target": "spec-impact", "summary_only": True},
            )
        ).data
        assert "counts" in res
        assert "implementing_symbols" in res["counts"]


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


@pytest.mark.asyncio
async def test_a_colliding_slug_stays_a_slug(workspace):
    """A clash used to switch dialects mid-project: SPEC-001 among slugs."""
    (workspace / "a.py").write_text("def f():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        ids = []
        for _ in range(3):
            made = (await c.call_tool(
                "create_spec", {"title": "Theme selection", "module": "ui"}
            )).data
            ids.append(made["spec_id"])
        assert ids == ["ui-theme-selection", "ui-theme-selection-2", "ui-theme-selection-3"]
