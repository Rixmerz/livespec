"""v0.8 P2 prep: aggregator over agent_log.jsonl streams."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.agent_log_analyze import aggregate, load_logs, render_markdown


def _write_jsonl(p: Path, entries: list[dict]) -> None:
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


@pytest.fixture
def fake_log(tmp_path: Path) -> Path:
    """Three sessions across two workspaces, plus one malformed line."""
    log = tmp_path / "agent_log.jsonl"
    rows = [
        # Session A in ws1: index_project -> find_symbol -> get_symbol_info
        {"ts": "2026-05-01T00:00:00", "tool_name": "index_project",
         "args_redacted": {}, "latency_ms": 100, "result_chars": 200,
         "error": None, "session_id": "A", "workspace": "/ws1"},
        {"ts": "2026-05-01T00:00:01", "tool_name": "find_symbol",
         "args_redacted": {"query": "X"}, "latency_ms": 5, "result_chars": 50,
         "error": None, "session_id": "A", "workspace": "/ws1"},
        {"ts": "2026-05-01T00:00:02", "tool_name": "get_symbol_info",
         "args_redacted": {"identifier": "X"}, "latency_ms": 10,
         "result_chars": 800, "error": None,
         "session_id": "A", "workspace": "/ws1"},
        # Session B in ws1: same A->B pattern
        {"ts": "2026-05-01T01:00:00", "tool_name": "find_symbol",
         "args_redacted": {"query": "Y"}, "latency_ms": 4, "result_chars": 80,
         "error": None, "session_id": "B", "workspace": "/ws1"},
        {"ts": "2026-05-01T01:00:01", "tool_name": "get_symbol_info",
         "args_redacted": {"identifier": "Y"}, "latency_ms": 8,
         "result_chars": 700, "error": None,
         "session_id": "B", "workspace": "/ws1"},
        # Session C in ws2: error case
        {"ts": "2026-05-01T02:00:00", "tool_name": "find_symbol",
         "args_redacted": {"query": "Z"}, "latency_ms": 3, "result_chars": 0,
         "error": "ValueError: bad", "session_id": "C", "workspace": "/ws2"},
    ]
    text = "\n".join(json.dumps(r) for r in rows) + "\nNOT-JSON-LINE\n\n"
    log.write_text(text)
    return log


def test_load_logs_skips_malformed(fake_log):
    entries = load_logs([fake_log])
    assert len(entries) == 6  # NOT-JSON-LINE + blank dropped


def test_load_logs_resolves_workspace_dir(tmp_path):
    """Passing a directory resolves to <dir>/.mcp-docs/agent_log.jsonl."""
    ws = tmp_path / "ws"
    (ws / ".mcp-docs").mkdir(parents=True)
    log = ws / ".mcp-docs" / "agent_log.jsonl"
    _write_jsonl(log, [
        {"ts": "t", "tool_name": "x", "args_redacted": {},
         "latency_ms": 0, "result_chars": 0, "error": None,
         "session_id": "s", "workspace": str(ws)}
    ])
    entries = load_logs([ws])
    assert len(entries) == 1
    assert entries[0]["tool_name"] == "x"


def test_aggregate_totals(fake_log):
    agg = aggregate(load_logs([fake_log]))
    assert agg["totals"]["calls"] == 6
    assert agg["totals"]["sessions"] == 3
    assert agg["totals"]["workspaces"] == 2
    assert agg["totals"]["tools_called"] == 3


def test_aggregate_per_tool_stats(fake_log):
    agg = aggregate(load_logs([fake_log]))
    by_name = {t["name"]: t for t in agg["tools"]}
    assert by_name["find_symbol"]["calls"] == 3
    assert by_name["find_symbol"]["errors"] == 1
    assert by_name["get_symbol_info"]["calls"] == 2
    assert by_name["index_project"]["calls"] == 1
    # Tools sorted by call count descending
    assert agg["tools"][0]["name"] == "find_symbol"


def test_aggregate_follow_up_pairs(fake_log):
    agg = aggregate(load_logs([fake_log]))
    pairs = {(p["from"], p["to"]): p["count"] for p in agg["follow_up_pairs"]}
    # find_symbol -> get_symbol_info appears in both A and B
    assert pairs[("find_symbol", "get_symbol_info")] == 2
    # index_project -> find_symbol only in A
    assert pairs[("index_project", "find_symbol")] == 1
    # Cross-session sequences should NOT appear (no spurious pair from
    # session A's last call to session B's first call)
    assert ("get_symbol_info", "find_symbol") not in pairs


def test_aggregate_silent_tools(fake_log):
    known = ["find_symbol", "get_symbol_info", "index_project",
             "audit_coverage", "quick_orient"]
    agg = aggregate(load_logs([fake_log]), known_tools=known)
    assert agg["silent_tools"] == ["audit_coverage", "quick_orient"]


def test_render_markdown_smoke(fake_log):
    agg = aggregate(load_logs([fake_log]),
                    known_tools=["find_symbol", "audit_coverage"])
    md = render_markdown(agg)
    assert "# Agent Log Aggregate" in md
    assert "find_symbol" in md
    assert "Calls per workspace" in md
    assert "follow-up pairs" in md.lower()
    assert "audit_coverage" in md  # silent_tools section


def test_aggregate_empty():
    agg = aggregate([], known_tools=["x", "y"])
    assert agg["totals"]["calls"] == 0
    assert agg["tools"] == []
    assert agg["follow_up_pairs"] == []
    assert agg["silent_tools"] == ["x", "y"]
