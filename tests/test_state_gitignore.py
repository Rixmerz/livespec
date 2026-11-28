"""`.mcp-docs/` keeps itself out of git: index_project drops a `.gitignore` of `*` in it."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.mark.asyncio
async def test_index_project_writes_a_self_ignoring_gitignore(workspace):
    (workspace / "m.py").write_text("def f():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    gi = workspace / ".mcp-docs" / ".gitignore"
    assert gi.read_text() == "*\n"


@pytest.mark.asyncio
async def test_an_existing_gitignore_is_left_alone(workspace):
    (workspace / "m.py").write_text("def f():\n    return 1\n")
    state = workspace / ".mcp-docs"
    state.mkdir()
    (state / ".gitignore").write_text("docs.db-wal\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    assert (state / ".gitignore").read_text() == "docs.db-wal\n"


@requires_git
@pytest.mark.asyncio
async def test_git_does_not_see_the_state_directory(workspace):
    (workspace / "m.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".mcp-docs" not in status
    assert "m.py" in status
