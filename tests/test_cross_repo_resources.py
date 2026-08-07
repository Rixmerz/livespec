"""project://group + guide://cross-repo resources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import reset_state


@pytest.mark.asyncio
async def test_guide_cross_repo_no_workspace():
    reset_state()
    async with Client(mcp) as c:
        for uri in ("project://cross-repo", "guide://cross-repo"):
            res = await c.read_resource(uri)
            text = res[0].text if isinstance(res, list) else res.contents[0].text
            assert "group_db" in text
            assert "xrepo-" in text


@pytest.mark.asyncio
async def test_get_cross_repo_guide_tool(tmp_path: Path):
    reset_state()
    (tmp_path / "app.py").write_text("def hello():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(tmp_path)})
        out = (await c.call_tool("get_cross_repo_guide", {"workspace": str(tmp_path)})).data
        assert "xrepo-" in out["guide"]
        assert out["group"]["grouped"] is False
        assert "project://cross-repo" in out["resources"]
        assert out["prompt"] == "cross_repo_workflow"


@pytest.mark.asyncio
async def test_project_group_ungrouped_hint(tmp_path: Path):
    reset_state()
    (tmp_path / "app.py").write_text("def hello():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(tmp_path)})
        res = await c.read_resource("project://group")
        raw = res[0].text if isinstance(res, list) else res.contents[0].text
        data = json.loads(raw)
        assert data["grouped"] is False
        assert data["group_db"] is None
        assert data["how_to"] == "project://cross-repo"
        assert "group_db" in (data.get("hint") or "")
        assert data["tool"] == "get_cross_repo_guide"


@pytest.mark.asyncio
async def test_project_group_xrepo_rollup(tmp_path: Path):
    reset_state()
    db = tmp_path / ".livespec-group" / "flow.db"
    (tmp_path / ".livespec-group").mkdir()
    a = tmp_path / "hub"
    b = tmp_path / "composer"
    for root in (a, b):
        root.mkdir()
        (root / "app.py").write_text(
            "def run():\n    '''@spec:xrepo-search-orchestrate'''\n    return 1\n"
        )
        (root / ".livespec.toml").write_text(
            f'[workspace]\ngroup_db = "{db}"\n',
            encoding="utf-8",
        )
        (root / "openspec" / "specs" / "xrepo").mkdir(parents=True)
        (root / "openspec" / "specs" / "xrepo" / "spec.md").write_text(
            "# xrepo\n\n"
            "### Requirement: Search Orchestrate\n\n"
            "#### Scenario: happy path\n"
            "- **WHEN** search runs\n"
            "- **THEN** it orchestrates\n",
            encoding="utf-8",
        )

    async with Client(mcp) as c:
        for root in (a, b):
            await c.call_tool("index_project", {"workspace": str(root)})
            await c.call_tool("sync_openspec", {"workspace": str(root)})
        res = await c.read_resource("project://group")
        raw = res[0].text if isinstance(res, list) else res.contents[0].text
        data = json.loads(raw)
        assert data["grouped"] is True
        assert data["counts"]["projects"] == 2
        ids = {s["spec_id"] for s in data["xrepo_specs"]}
        assert "xrepo-search-orchestrate" in ids
        row = next(s for s in data["xrepo_specs"] if s["spec_id"] == "xrepo-search-orchestrate")
        assert row["repo_count"] == 2
