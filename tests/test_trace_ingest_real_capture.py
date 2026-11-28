"""`ingest_traces` against what FlowTrace really writes.

Every other trace in this suite is typed by hand from the schema. That proves
the resolver against our reading of the contract, not against the capture
layer: a `file` written relative to another root, or a `line` that points at a
decorator, would pass all of them and resolve nothing in the field. This one
ingests a file a real FlowTrace run produced.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.trace_ingest import RUNTIME_ORIGIN
from livespec_mcp.server import mcp

CAPTURE = Path(__file__).parent / "fixtures" / "flowtrace_capture"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(CAPTURE, ws)
    return ws


async def test_a_real_capture_resolves_every_event_by_location(workspace: Path):
    async with Client(mcp) as client:
        await client.call_tool("index_project", {"workspace": str(workspace)})
        plan = (
            await client.call_tool(
                "ingest_traces",
                {"workspace": str(workspace), "trace_path": str(workspace / "flowtrace.jsonl")},
            )
        ).data

    assert plan["enter_events"] == 5
    assert plan["events_with_location"] == 5, "the capture layer stopped writing file/line"
    assert plan["resolved_by"] == {"location": 5}, plan
    assert plan["unresolved"] == {}, plan


async def test_a_real_capture_names_the_dynamic_caller_and_sorts_the_unused(workspace: Path):
    ws = str(workspace)
    async with Client(mcp) as client:
        await client.call_tool("index_project", {"workspace": ws})
        before = (
            await client.call_tool(
                "who_calls", {"workspace": ws, "qname": "shop.handlers.handle_pay"}
            )
        ).data
        await client.call_tool(
            "ingest_traces",
            {"workspace": ws, "trace_path": str(workspace / "flowtrace.jsonl"), "dry_run": False},
        )
        after = (
            await client.call_tool(
                "who_calls", {"workspace": ws, "qname": "shop.handlers.handle_pay"}
            )
        ).data
        dead = (await client.call_tool("find_dead_code", {"workspace": ws})).data

    # `dispatch` reaches the handlers through getattr: invisible statically.
    assert before["count"] == 0
    assert [(c["qualified_name"], c["via_external_edge"]) for c in after["callers"]] == [
        ("shop.registry.dispatch", RUNTIME_ORIGIN)
    ]

    tiers = {d["qualified_name"]: d["runtime"] for d in dead["dead_symbols"]}
    assert tiers == {
        # The run reached handlers.py and this one never executed.
        "shop.handlers.handle_legacy_export": "not_seen",
        # Nothing imports unused.py, so the trace says nothing about it.
        "shop.unused.never_imported": "no_evidence",
    }, "handlers that ran must not be listed as dead"
