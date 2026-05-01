"""Tests for the file watcher (P2.3)."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_watcher_lifecycle(sample_repo):
    """start_watcher then stop_watcher; active count returns to zero."""
    async with Client(mcp) as c:
        started = (await c.call_tool("start_watcher", {"debounce_seconds": 0.2})).data
        assert started["active_watchers"] == 1
        try:
            status = (await c.call_tool("watcher_status", {})).data
            assert status["active"] is True
            assert status["debounce_seconds"] == 0.2
        finally:
            stopped = (await c.call_tool("stop_watcher", {})).data
        assert stopped["stopped"] is True
        assert stopped["active_watchers"] == 0


@pytest.mark.asyncio
async def test_watcher_reindexes_on_file_change(sample_repo):
    """Touching a Python file under the workspace must trigger a re-index."""
    async with Client(mcp) as c:
        # Initial baseline so files are tracked
        await c.call_tool("index_project", {})
        await c.call_tool("start_watcher", {"debounce_seconds": 0.2})
        try:
            target = sample_repo / "pkg" / "auth.py"
            target.write_text(target.read_text() + "\n# touched by watcher test\n")
            # Wait past debounce + reindex time
            for _ in range(20):  # up to 4s
                await asyncio.sleep(0.2)
                status = (await c.call_tool("watcher_status", {})).data
                if status.get("reindex_runs", 0) >= 1:
                    break
            else:
                pytest.fail(f"watcher never reindexed. status={status}")
            assert status["events_received"] >= 1
            assert status["reindex_runs"] >= 1
        finally:
            await c.call_tool("stop_watcher", {})
