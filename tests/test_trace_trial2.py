"""Second end-to-end trial: `find_dead_code(path=)`, the coverage denominator,
and stale runtime evidence (migration 35).

Reuses the dispatch fixture of `test_trace_ingest`: `dispatch` reaches the
handlers through getattr, so only a trace can say they run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.storage.db import MIGRATIONS, connect
from tests.test_trace_ingest import (
    _call,
    _dispatch_trace,
    _refund_trace,
    _repo,
    _state,
    _Trace,
)


@pytest.fixture
def repo(workspace: Path) -> Path:
    _repo(workspace)
    return workspace


# --------------------------------------------------------------------------
# find_dead_code(path=)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_code_path_restricts_candidates_and_counts(repo: Path):
    async with Client(mcp) as c:
        await _call(c, "index_project")
        whole = await _call(c, "find_dead_code")
        one_file = await _call(c, "find_dead_code", path="pkg/handlers.py")
        as_dir = await _call(c, "find_dead_code", path="./pkg/")
        summary = await _call(c, "find_dead_code", path="pkg/handlers.py", summary_only=True)

    names = {r["qualified_name"] for r in one_file["dead_symbols"]}
    assert names == {
        "pkg.handlers.handle_pay",
        "pkg.handlers.handle_refund",
        "pkg.handlers.never_called",
    }
    assert one_file["count"] == 3 and one_file["path"] == "pkg/handlers.py"
    assert one_file["by_top_dir"] == {"pkg": 3}
    assert "dead_symbols" not in summary and summary["count"] == 3
    # The directory spelling is the whole package; nothing was lost to the prefix.
    assert as_dir["count"] == whole["count"]
    assert "path" not in whole


@pytest.mark.asyncio
async def test_dead_code_path_applies_to_the_runtime_tiers(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        out = await _call(c, "find_dead_code", path="pkg/idle.py")
        handlers = await _call(c, "find_dead_code", path="pkg/handlers.py")
        main = await _call(c, "find_dead_code", path="pkg/main.py")

    assert [r["qualified_name"] for r in out["dead_symbols"]] == ["pkg.idle.lonely"]
    assert out["runtime"]["no_evidence"] == 1 and out["runtime"]["not_seen"] == 0
    assert out["runtime"]["rescued_by_runtime"] == 0
    # handle_pay ran; the other two share its file and never did.
    assert handlers["runtime"]["not_seen"] == 2 and handlers["count"] == 2
    # `run` ran: rescued, so nothing is left in its file, and the count says so.
    assert main["count"] == 0
    assert [r["qualified_name"] for r in main["runtime"]["rescued"]] == ["pkg.main.run"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["nope", "pkg/handl", "pkg/handlers.py/x"])
async def test_dead_code_unknown_path_is_a_shaped_error(repo: Path, bad: str):
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "find_dead_code", path=bad)

    assert out["isError"] is True and bad in out["error"]
    assert "workspace-relative" in out["hint"]
    assert set(out) <= {"error", "isError", "hint", "did_you_mean"}


# --------------------------------------------------------------------------
# get_project_overview: the coverage denominator
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_runtime_evidence_has_the_non_test_denominator(repo: Path):
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_pay.py").write_text("def test_pay():\n    return 1\n")
    trace = _dispatch_trace()
    trace.enter("test_pay", module="tests.test_pay", file="tests/test_pay.py", line=1)
    path = trace.write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        before = await _call(c, "get_project_overview")
        await _call(c, "ingest_traces", trace_path=path, dry_run=False)
        ev = (await _call(c, "get_project_overview"))["runtime_evidence"]

    assert "runtime_evidence" not in before
    # 10 non-test functions/methods in the fixture: handle_pay, handle_refund,
    # never_called, dispatch, run, lonely, 2 renders, 2 totals = 10.
    assert ev["symbols_non_test"] == 10
    # run, dispatch and handle_pay ran; the test function is counted in
    # symbols_with_hits only.
    assert ev["symbols_with_hits_non_test"] == 3
    assert ev["symbols_with_hits"] == 4


# --------------------------------------------------------------------------
# Migration 35
# --------------------------------------------------------------------------


def test_m035_adds_index_run_id_and_backfills(tmp_path: Path):
    assert dict((v, n) for v, n, _ in MIGRATIONS)[35] == "runtime_source_index_run"
    db = tmp_path / "old.db"
    conn = connect(db)
    assert "index_run_id" in {r["name"] for r in conn.execute("PRAGMA table_info(runtime_source)")}
    conn.execute("INSERT INTO project(id, name, root) VALUES(1, 'p', '/p')")
    for rid, changed, fin in ((1, 5, "2020-01-01 00:00:00"), (2, 0, "2020-01-02 00:00:00")):
        conn.execute(
            "INSERT INTO index_run(id, project_id, started_at, finished_at, files_changed) "
            "VALUES(?, 1, ?, ?, ?)",
            (rid, fin, fin, changed),
        )
    # ingested 2020-01-03: the latest run that changed files before it is run 1
    conn.execute(
        "INSERT INTO runtime_source(project_id, origin, path, events, resolved_events, "
        "symbols_hit, edges_observed, ingested_at) VALUES(1, 'o', 'a', 1, 1, 1, 0, 1578009600)"
    )
    conn.execute("ALTER TABLE runtime_source DROP COLUMN index_run_id")
    conn.execute("DELETE FROM schema_migrations WHERE version = 35")
    conn.commit()
    conn.close()
    conn = connect(db)
    assert conn.execute("SELECT index_run_id FROM runtime_source").fetchone()[0] == 1
    conn.close()


# --------------------------------------------------------------------------
# Stale runtime evidence
# --------------------------------------------------------------------------


def _switch_callers(repo: Path) -> None:
    """`run` no longer calls anything the trace saw: its file is re-extracted."""
    (repo / "pkg" / "main.py").write_text(
        "from pkg.registry import dispatch\n\n\ndef run():\n    return 0\n\n\ndef other():\n    return dispatch('x', 1)\n"
    )


@pytest.mark.asyncio
async def test_rescue_by_a_pre_switch_trace_is_marked_stale_not_dropped(repo: Path):
    old = _refund_trace().write(repo / "traces" / "old.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        await _call(c, "ingest_traces", trace_path=old, dry_run=False)
        fresh = await _call(c, "find_dead_code")
        orient_fresh = await _call(c, "quick_orient", qname="pkg.handlers.handle_refund")

        _switch_callers(repo)
        await _call(c, "index_project")
        stale = await _call(c, "find_dead_code")
        orient_stale = await _call(c, "quick_orient", qname="pkg.handlers.handle_refund")

        # a source ingested after the switch: no longer ALL sources are stale
        after = _Trace()
        after.enter("handle_refund", module="pkg.handlers", file="pkg/handlers.py", line=5)
        newer = after.write(repo / "traces" / "new.jsonl")
        await _call(c, "ingest_traces", trace_path=newer, dry_run=False)
        mixed = await _call(c, "find_dead_code")

        # the old source goes: only post-switch evidence is left
        await _call(c, "ingest_traces", remove=True, trace_path=[old])
        clean = await _call(c, "find_dead_code")

    assert all("stale_evidence" not in r for r in fresh["runtime"]["rescued"])
    assert "runtime_stale_evidence" not in orient_fresh

    rescued = {r["qualified_name"]: r for r in stale["runtime"]["rescued"]}
    # still rescued (not dropped), and marked, with the source named
    entry = rescued["pkg.handlers.handle_refund"]
    assert entry["stale_evidence"] is True and entry["sources"] == [old]
    assert stale["runtime"]["rescued_stale_evidence"] >= 1
    assert "re-trace or remove these sources" in stale["runtime"]["stale_evidence_hint"].lower()
    assert old in stale["runtime"]["stale_evidence_hint"]
    assert orient_stale["runtime_calls"] == 1
    assert orient_stale["runtime_stale_evidence"] is True
    assert orient_stale["runtime_stale_sources"] == [old]

    for out in (mixed, clean):
        got = {r["qualified_name"]: r for r in out["runtime"]["rescued"]}
        assert "stale_evidence" not in got["pkg.handlers.handle_refund"]
        assert "stale_evidence_hint" not in out["runtime"]


@pytest.mark.asyncio
async def test_no_code_change_after_ingest_means_no_stale_mark(repo: Path):
    trace = _refund_trace().write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        await _call(c, "index_project")  # no-op run: files_changed == 0
        out = await _call(c, "find_dead_code")

    assert "stale_evidence_hint" not in out["runtime"]
    assert _state(repo).conn.execute("SELECT index_run_id FROM runtime_source").fetchone()[0] > 0
