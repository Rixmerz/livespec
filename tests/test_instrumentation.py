"""v0.8 P1: agent dispatch logging middleware."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _read_log(workspace: Path) -> list[dict]:
    log = workspace / ".mcp-docs" / "agent_log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


@pytest.mark.asyncio
async def test_every_dispatch_logs_one_line(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("list_requirements", {})
        await c.call_tool("find_symbol", {"query": "login"})

    entries = _read_log(sample_repo)
    names = [e["tool_name"] for e in entries]
    assert names == ["index_project", "list_requirements", "find_symbol"]
    # Schema fields present on every line
    for e in entries:
        assert set(e.keys()) >= {
            "ts",
            "tool_name",
            "args_redacted",
            "latency_ms",
            "result_chars",
            "error",
            "session_id",
            "workspace",
        }
        assert isinstance(e["latency_ms"], int)
        assert e["latency_ms"] >= 0
        assert e["result_chars"] >= 0
        assert e["error"] is None
        assert e["workspace"] == str(sample_repo)


@pytest.mark.asyncio
async def test_args_redacted_strips_workspace_path(sample_repo):
    """Absolute paths under the workspace get rewritten to <workspace>/..."""
    abs_path = str(sample_repo / "pkg" / "auth.py")
    async with Client(mcp) as c:
        # workspace= explicitly passed; would normally land verbatim in args
        await c.call_tool(
            "find_symbol",
            {"query": abs_path, "workspace": str(sample_repo)},
        )

    entries = _read_log(sample_repo)
    last = entries[-1]
    assert "<workspace>" in last["args_redacted"]["query"]
    assert str(sample_repo) not in last["args_redacted"]["query"]
    assert last["args_redacted"]["workspace"] == "<workspace>"


@pytest.mark.asyncio
async def test_log_records_isError_results_with_error_field_none(sample_repo):
    """Tools that return mcp_error() payloads aren't exceptions — `error`
    stays None but the result_chars covers the error envelope."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("quick_orient", {"qname": "does.not.exist"})

    entries = _read_log(sample_repo)
    last = entries[-1]
    assert last["tool_name"] == "quick_orient"
    assert last["error"] is None  # mcp_error is a value, not a raise
    assert last["result_chars"] > 0


@pytest.mark.asyncio
async def test_logging_disabled_via_env(sample_repo, monkeypatch):
    monkeypatch.setenv("LIVESPEC_AGENT_LOG", "0")
    async with Client(mcp) as c:
        await c.call_tool("list_requirements", {})

    log = sample_repo / ".mcp-docs" / "agent_log.jsonl"
    assert not log.exists()


@pytest.mark.asyncio
async def test_log_file_lives_under_resolved_workspace(sample_repo):
    """No-arg tool calls still resolve the workspace via env -> log lands
    in the right .mcp-docs/."""
    async with Client(mcp) as c:
        await c.call_tool("list_requirements", {})

    log = sample_repo / ".mcp-docs" / "agent_log.jsonl"
    assert log.exists()
    entries = _read_log(sample_repo)
    assert len(entries) == 1
    assert entries[0]["workspace"] == str(sample_repo)
