"""v0.8 P1: agent dispatch logging middleware."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.fixture(autouse=True)
def _enable_agent_log(sample_repo, monkeypatch):
    """Instrumentation is opt-in per repo; most tests force it on via env."""
    monkeypatch.setenv("LIVESPEC_AGENT_LOG", "1")
    log = sample_repo / ".mcp-docs" / "agent_log.jsonl"
    if log.exists():
        log.unlink()


def _read_log(workspace: Path) -> list[dict]:
    log = workspace / ".mcp-docs" / "agent_log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


@pytest.mark.asyncio
async def test_every_dispatch_logs_one_line(sample_repo):
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        await c.call_tool("list_specs", {"workspace": ws})
        await c.call_tool("find_symbol", {"query": "login", "workspace": ws})

    entries = _read_log(sample_repo)
    names = [e["tool_name"] for e in entries]
    assert names == ["index_project", "list_specs", "find_symbol"]
    # Schema fields present on every line
    for e in entries:
        assert set(e.keys()) >= {
            "timestamp",
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
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": ws})
        await c.call_tool("quick_orient", {"qname": "does.not.exist", "workspace": ws})

    entries = _read_log(sample_repo)
    last = entries[-1]
    assert last["tool_name"] == "quick_orient"
    assert last["error"] is None  # mcp_error is a value, not a raise
    assert last["result_chars"] > 0


@pytest.mark.asyncio
async def test_logging_disabled_via_env(sample_repo, monkeypatch):
    monkeypatch.setenv("LIVESPEC_AGENT_LOG", "0")
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("list_specs", {"workspace": ws})

    log = sample_repo / ".mcp-docs" / "agent_log.jsonl"
    assert not log.exists()


@pytest.mark.asyncio
async def test_log_respects_repo_config_log_calls(sample_repo, monkeypatch):
    monkeypatch.delenv("LIVESPEC_AGENT_LOG", raising=False)
    (sample_repo / ".livespec.toml").write_text("[agent]\nlog_calls = true\n")
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("list_specs", {"workspace": ws})
    assert _read_log(sample_repo)


@pytest.mark.asyncio
async def test_log_off_by_default_without_config(sample_repo, monkeypatch):
    monkeypatch.delenv("LIVESPEC_AGENT_LOG", raising=False)
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("list_specs", {"workspace": ws})
    log = sample_repo / ".mcp-docs" / "agent_log.jsonl"
    assert not log.exists()


@pytest.mark.asyncio
async def test_log_file_lives_under_resolved_workspace(sample_repo):
    """Log lands under the workspace path passed on the tool call."""
    ws = str(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("list_specs", {"workspace": ws})

    log = sample_repo / ".mcp-docs" / "agent_log.jsonl"
    assert log.exists()
    entries = _read_log(sample_repo)
    assert len(entries) == 1
    assert entries[0]["workspace"] == str(sample_repo)
