"""v0.14 P3 robustness batch:

- mapped-but-unsupported languages (c/cpp/c_sharp/kotlin/swift/scala) are
  skipped at walk time and reported in `languages_unsupported` instead of
  silently indexing as zero-symbol files;
- resources resolve the most-recently-used workspace when `get_state()`
  has no default (production multi-tenant), and return mcp_error-shaped
  JSON when no workspace was touched yet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp import resources as resources_module
from livespec_mcp import state as state_module
from livespec_mcp.server import mcp
from livespec_mcp.workspace_param import WorkspaceRequiredError


@pytest.mark.asyncio
async def test_unsupported_language_reported_not_silently_empty(workspace: Path):
    (workspace / "app.py").write_text("def app(): pass\n")
    (workspace / "native.cpp").write_text("int main() { return 0; }\n")
    (workspace / "lib.kt").write_text("fun lib() {}\n")
    async with Client(mcp) as c:
        data = (await c.call_tool("index_project", {})).data
        assert data["files_total"] == 1
        assert data["languages"] == {"python": 1}
        assert data["languages_unsupported"] == {"cpp": 1, "kotlin": 1}


@pytest.mark.asyncio
async def test_resources_fall_back_to_mru_workspace(workspace: Path, monkeypatch):
    """Production path: get_state() without workspace raises (multi-tenant
    v0.12); resources must bind to the last workspace a tool touched."""
    (workspace / "app.py").write_text("def app(): pass\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})

        def _raises(path=None):
            raise WorkspaceRequiredError("workspace is required")

        monkeypatch.setattr(resources_module, "get_state", _raises)
        res = await c.read_resource("project://index/status")
        status = json.loads(res[0].text)
        assert status["workspace"] == str(workspace)
        assert status["files"] == 1


@pytest.mark.asyncio
async def test_resources_error_shape_when_no_workspace_yet(monkeypatch):
    def _raises(path=None):
        raise WorkspaceRequiredError("workspace is required")

    monkeypatch.setattr(resources_module, "get_state", _raises)
    state_module.reset_state()  # empty LRU — nothing to bind to
    async with Client(mcp) as c:
        res = await c.read_resource("project://overview")
        payload = json.loads(res[0].text)
        assert payload["isError"] is True
        assert payload["error"] == "No active workspace"
        assert "workspace=" in payload["hint"]


@pytest.mark.asyncio
async def test_resource_not_found_uses_error_shape(workspace: Path):
    (workspace / "app.py").write_text("def app(): pass\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        res = await c.read_resource("project://symbols/no.such.symbol")
        payload = json.loads(res[0].text)
        assert payload["isError"] is True
        assert "not found" in payload["error"]
