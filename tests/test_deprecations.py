"""v0.8 P3.2 — deprecation envelope on get_index_status.

Tool stays functional; payload now carries `deprecated`/`replacement`/
`removal` keys, and the first call per process emits a stderr warning.
Drop scheduled for v0.9.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools import indexing as indexing_module


@pytest.fixture(autouse=True)
def _reset_deprecation_warned_set():
    """Each test starts with a fresh once-per-process state."""
    indexing_module._DEPRECATION_WARNED.clear()
    yield
    indexing_module._DEPRECATION_WARNED.clear()


@pytest.mark.asyncio
async def test_get_index_status_payload_has_deprecation_marker(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        data = (await c.call_tool("get_index_status", {})).data
        assert data["deprecated"] is True
        assert data["replacement"] == "project://index/status"
        assert data["removal"] == "v0.9"


@pytest.mark.asyncio
async def test_get_index_status_still_returns_full_payload(sample_repo):
    """Deprecation must not strip the data — agents using the tool keep working."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        data = (await c.call_tool("get_index_status", {})).data
        for key in ("workspace", "project_id", "files", "symbols", "edges",
                    "requirements", "last_run"):
            assert key in data, f"missing key {key!r}"


@pytest.mark.asyncio
async def test_stderr_warning_emitted_once_per_process(sample_repo, capsys):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        capsys.readouterr()  # drop any indexing chatter
        await c.call_tool("get_index_status", {})
        first = capsys.readouterr().err
        assert "DEPRECATED" in first
        assert "get_index_status" in first
        assert "project://index/status" in first

        await c.call_tool("get_index_status", {})
        second = capsys.readouterr().err
        assert "DEPRECATED" not in second, (
            "deprecation warning must fire only once per process"
        )


def test_resource_payload_has_no_deprecation_marker(sample_repo):
    """The resource is the canonical surface — no deprecation envelope."""
    from livespec_mcp.state import get_state
    from livespec_mcp.tools.indexing import compute_index_status

    payload = compute_index_status(get_state())
    assert "deprecated" not in payload
    assert "replacement" not in payload
