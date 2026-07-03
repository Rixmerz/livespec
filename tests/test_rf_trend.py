"""v0.16: Spec coverage drill-down, diff→Spec impact helper, and trend recording.

Covers three additive features:

* ``compute_spec_test_coverage`` now reports per-Spec ``uncovered_symbols``
  (impl symbols neither test-reached nor explicitly ``tests``-linked).
* ``compute_diff_spec_impact`` maps a git range to the Specs it touches.
* ``storage/trends`` persists a coverage snapshot per ``audit_coverage`` run
  and reads them back chronologically; the migration applies cleanly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import get_state
from livespec_mcp.storage.trends import read_trend, record_snapshot
from livespec_mcp.tools.analysis import compute_diff_spec_impact


def _git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


# ---------- Feature B: uncovered_symbols drill-down ----------


@pytest.mark.asyncio
async def test_uncovered_symbols_lists_untested_impl(workspace):
    """An Spec with one TESTED impl and one UNTESTED impl: only the untested
    qname appears in `uncovered_symbols`; the tested one does not."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def covered_impl():\n"
        "    return 1\n"
        "\n"
        "def uncovered_impl():\n"
        "    return 2\n"
    )
    (workspace / "tests").mkdir()
    # Test reaches only covered_impl — uncovered_impl has no test path.
    (workspace / "tests" / "test_feature.py").write_text(
        "from pkg.feature import covered_impl\n"
        "\n"
        "def test_covered():\n"
        "    assert covered_impl() == 1\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-001", "title": "Feature"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-001", "symbol_qname": "pkg.feature.covered_impl"},
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-001", "symbol_qname": "pkg.feature.uncovered_impl"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    by_id = {r["spec_id"]: r for r in out["spec_coverage"]}
    entry = by_id["SPEC-001"]
    assert "uncovered_symbols" in entry, f"field missing: {entry}"
    assert entry["uncovered_symbols"] == ["pkg.feature.uncovered_impl"], (
        f"only the untested impl should be listed: {entry['uncovered_symbols']}"
    )
    assert "pkg.feature.covered_impl" not in entry["uncovered_symbols"]
    assert entry["uncovered_symbols_count"] == 1
    assert entry["total_symbols"] == 2
    assert entry["tested_symbols"] == 1


@pytest.mark.asyncio
async def test_uncovered_symbols_empty_when_fully_tested(workspace):
    """A fully-tested Spec has an empty `uncovered_symbols` list."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def implementer():\n"
        "    return 1\n"
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_feature.py").write_text(
        "from pkg.feature import implementer\n"
        "\n"
        "def test_implementer():\n"
        "    assert implementer() == 1\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-010", "title": "Done"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-010", "symbol_qname": "pkg.feature.implementer"},
        )
        out = (await c.call_tool("audit_coverage", {})).data

    entry = {r["spec_id"]: r for r in out["spec_coverage"]}["SPEC-010"]
    assert entry["uncovered_symbols"] == []
    assert entry["uncovered_symbols_count"] == 0


# ---------- Feature A: compute_diff_spec_impact ----------


@pytest.fixture
def git_repo_with_rf(sample_repo: Path) -> Path:
    """sample_repo committed, then auth.py mutated in a second commit.
    HEAD~1..HEAD touches pkg/auth.py (which carries login → Spec link)."""
    _git(sample_repo, "init", "-q")
    _git(sample_repo, "config", "user.email", "test@example.com")
    _git(sample_repo, "config", "user.name", "test")
    _git(sample_repo, "add", ".")
    _git(sample_repo, "commit", "-q", "-m", "initial")
    auth = sample_repo / "pkg" / "auth.py"
    auth.write_text(auth.read_text() + "\n\ndef extra_helper():\n    return 42\n")
    _git(sample_repo, "add", ".")
    _git(sample_repo, "commit", "-q", "-m", "add extra_helper")
    return sample_repo


@pytest.mark.asyncio
async def test_compute_diff_spec_impact_returns_touched_rfs(git_repo_with_rf):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Link SPEC-100 to the changed file's symbol so the diff touches it.
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-100", "title": "Auth login"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-100", "symbol_qname": "pkg.auth.login"},
        )

        st = get_state()
        result = compute_diff_spec_impact(st, "HEAD~1", "HEAD")

    assert result["base"] == "HEAD~1"
    assert result["head"] == "HEAD"
    assert "pkg/auth.py" in result["files_changed"]
    touched_ids = {r["spec_id"] for r in result["specs_touched"]}
    assert "SPEC-100" in touched_ids, (
        f"SPEC-100 should be touched by the auth.py diff: {result['specs_touched']}"
    )
    entry = next(r for r in result["specs_touched"] if r["spec_id"] == "SPEC-100")
    assert "pkg/auth.py" in entry["files"]
    assert entry["title"] == "Auth login"
    assert isinstance(entry["test_coverage_ratio"], (int, float))


@pytest.mark.asyncio
async def test_compute_diff_spec_impact_empty_shape_without_git(workspace, sample_repo):
    """No git history → clear empty shape (caller omits the section)."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        st = get_state()
        result = compute_diff_spec_impact(st, "HEAD~1", "HEAD")

    assert result == {
        "base": "HEAD~1",
        "head": "HEAD",
        "files_changed": [],
        "specs_touched": [],
    }


# ---------- Feature D: trend recording ----------


def test_trend_record_and_read_two_snapshots(tmp_path):
    """record_snapshot twice → read_trend returns both chronologically."""
    from livespec_mcp.storage.db import connect, get_or_create_project

    conn = connect(tmp_path / "trend.db")
    pid = get_or_create_project(conn, "p", str(tmp_path))

    record_snapshot(
        conn,
        pid,
        per_spec={"SPEC-001": 0.5, "SPEC-002": 1.0},
        avg=0.75,
        verified_count=1,
        ts="2026-06-25T10:00:00+00:00",
    )
    record_snapshot(
        conn,
        pid,
        per_spec={"SPEC-001": 1.0, "SPEC-002": 1.0},
        avg=1.0,
        verified_count=2,
        ts="2026-06-25T11:00:00+00:00",
    )

    trend = read_trend(conn, pid)
    assert len(trend) == 2
    assert [t["ts"] for t in trend] == [
        "2026-06-25T10:00:00+00:00",
        "2026-06-25T11:00:00+00:00",
    ]
    assert trend[0]["avg_test_coverage"] == 0.75
    assert trend[0]["verified_count"] == 1
    assert trend[1]["avg_test_coverage"] == 1.0
    assert trend[1]["verified_count"] == 2
    conn.close()


def test_trend_handles_no_rfs_avg_none(tmp_path):
    """A snapshot with no Specs records avg=None and reads back as None."""
    from livespec_mcp.storage.db import connect, get_or_create_project

    conn = connect(tmp_path / "trend2.db")
    pid = get_or_create_project(conn, "p", str(tmp_path))
    record_snapshot(
        conn, pid, per_spec={}, avg=None, verified_count=0,
        ts="2026-06-25T12:00:00+00:00",
    )
    trend = read_trend(conn, pid)
    assert len(trend) == 1
    assert trend[0]["avg_test_coverage"] is None
    assert trend[0]["verified_count"] == 0
    conn.close()


def test_trend_dedups_unchanged_consecutive(tmp_path):
    """Identical consecutive snapshots collapse to one; a change adds a point."""
    from livespec_mcp.storage.db import connect, get_or_create_project

    conn = connect(tmp_path / "trend_dedup.db")
    pid = get_or_create_project(conn, "p", str(tmp_path))
    for ts in ("2026-06-25T10:00:00+00:00", "2026-06-25T10:05:00+00:00"):
        record_snapshot(
            conn, pid, per_spec={"SPEC-001": 0.5}, avg=0.5, verified_count=1, ts=ts
        )
    record_snapshot(
        conn, pid, per_spec={"SPEC-001": 1.0}, avg=1.0, verified_count=1,
        ts="2026-06-25T10:10:00+00:00",
    )
    trend = read_trend(conn, pid)
    assert len(trend) == 2, f"dedup identical, keep on change: {trend}"
    assert [t["avg_test_coverage"] for t in trend] == [0.5, 1.0]
    conn.close()


@pytest.mark.asyncio
async def test_audit_coverage_records_a_snapshot(workspace):
    """audit_coverage records a trend snapshot; identical re-audits dedup (record-on-change)."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text("def implementer():\n    return 1\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"spec_id": "SPEC-500", "title": "F"}
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-500", "symbol_qname": "pkg.feature.implementer"},
        )
        await c.call_tool("audit_coverage", {})
        await c.call_tool("audit_coverage", {})  # unchanged -> deduped

        st = get_state()
        trend = read_trend(st.conn, st.project_id)

    assert len(trend) == 1, f"unchanged re-audits should dedup to one snapshot: {trend}"
    # avg is a float (SPEC-500 exists) and verified_count is an int.
    assert all(isinstance(t["verified_count"], int) for t in trend)
