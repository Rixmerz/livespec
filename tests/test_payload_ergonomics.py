"""Unreleased payload-ergonomics batch — findings from a dogfood pass on a
real 13-repo polyrepo group.

1. `find_dead_code` reported wildly different counts for the same repo depending
   on flags, with nothing in the payload saying which flags were in play. The
   pre-existing `not_swept` explanation only fired when the count was exactly
   zero, so a non-zero-but-surprising count stayed unexplained. Now
   `filtered_out` attributes every flag-flippable skip to the flag that would
   include it — and survives `summary_only`, the mode most likely to be used.

2. The automatic `links_seed` replay inside `index_project` emitted one result
   row per mapping even when nothing changed (61 rows of `linked: false` to say
   "no-op"). Counts stay exact; the no-op rows are now elided.

(A third finding, about the Flow Explorer host, went with the Flow Explorer
itself: the in-repo explorers were removed in favour of `livespec ui`.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

# --------------------------------------------------------------------------
# 1. find_dead_code: filter attribution
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filtered_out_attributes_test_skips(workspace):
    """Test-path candidates are excluded by default; the payload must say so
    and name the flag that brings them back."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "core.py").write_text("def _unused_helper():\n    return 1\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_thing.py").write_text("def _unused_test_helper():\n    return 2\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data

        assert out["filtered_out"]["tests"] >= 1
        assert "include_tests" in out["filtered_out_hint"]
        # The excluded symbol must NOT be in the reported candidates.
        names = [d["qualified_name"] for d in out["dead_symbols"]]
        assert not any("_unused_test_helper" in n for n in names)


@pytest.mark.asyncio
async def test_filtered_out_survives_summary_only(workspace):
    """`summary_only` is the cheap mode an agent reaches for first — stripping
    the attribution there would hide the answer from whoever needs it most."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "core.py").write_text("def _unused_helper():\n    return 1\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_thing.py").write_text("def _unused_test_helper():\n    return 2\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        full = (await c.call_tool("find_dead_code", {})).data
        summary = (await c.call_tool("find_dead_code", {"summary_only": True})).data

        assert "filtered_out" in summary
        assert summary["filtered_out"] == full["filtered_out"]
        # Counts stay exact across modes (existing pagination contract).
        assert summary["count"] == full["count"]
        assert "dead_symbols" not in summary


@pytest.mark.asyncio
async def test_flipping_the_flag_moves_candidates_out_of_filtered_out(workspace):
    """The attribution has to be actionable: passing the named flag must turn
    the excluded candidates into reported ones."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "core.py").write_text("def _unused_helper():\n    return 1\n")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_thing.py").write_text("def _unused_test_helper():\n    return 2\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        default = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        with_tests = (
            await c.call_tool("find_dead_code", {"include_tests": True, "summary_only": True})
        ).data

        skipped_tests = default["filtered_out"]["tests"]
        assert with_tests["count"] == default["count"] + skipped_tests
        assert "tests" not in (with_tests.get("filtered_out") or {})


@pytest.mark.asyncio
async def test_genuine_clean_sweep_has_no_filtered_out_noise(workspace):
    """No default filter fired ⇒ no `filtered_out` key at all. The field must
    signal something, not decorate every payload."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "core.py").write_text(
        "def _helper():\n    return 1\n\n\ndef _caller():\n    return _helper()\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        assert "filtered_out" not in out


# --------------------------------------------------------------------------
# 2. bulk link: quiet no-op results
# --------------------------------------------------------------------------


def _seed_spec_and_symbol(workspace: Path) -> None:
    (workspace / "pkg").mkdir(exist_ok=True)
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "core.py").write_text("def handler():\n    return 1\n")


@pytest.mark.asyncio
async def test_links_seed_replay_elides_noop_rows_but_keeps_counts(workspace):
    """A steady-state repo re-indexes to "nothing changed" — the payload must
    say that in counts, not in one row per mapping."""
    _seed_spec_and_symbol(workspace)
    (workspace / "openspec" / "specs" / "core").mkdir(parents=True)
    (workspace / "openspec" / "specs" / "core" / "spec.md").write_text(
        "# Core\n\n## Requirements\n\n"
        "### Requirement: Handle a thing\n\n"
        "The service SHALL handle a thing.\n\n"
        "#### Scenario: Happy path\n"
        "- **WHEN** asked\n"
        "- **THEN** it handles\n"
    )
    (workspace / "docs").mkdir(exist_ok=True)
    (workspace / "docs" / "requirements").mkdir(parents=True, exist_ok=True)
    (workspace / "docs" / "requirements" / "spec-links.json").write_text(
        json.dumps([{"spec_id": "core-handle-a-thing", "qname": "pkg.core.handler"}])
    )
    (workspace / ".livespec.toml").write_text(
        '[specs]\nopenspec_dir = "openspec"\nlinks_seed = "docs/requirements/spec-links.json"\n'
    )

    async with Client(mcp) as c:
        first = (await c.call_tool("index_project", {})).data
        links1 = first["specs_sync"]["links"]
        # First pass actually creates the link, so it is reported.
        assert links1["linked"] == 1
        assert len(links1["results"]) == 1

        second = (await c.call_tool("index_project", {"force": True})).data
        links2 = second["specs_sync"]["links"]
        # Second pass is a pure no-op: exact counts, no per-row noise.
        assert links2["total"] == 1
        assert links2["skipped"] == 1
        assert links2["linked"] == 0
        assert links2["results"] == []
        assert links2["results_omitted"] == 1


@pytest.mark.asyncio
async def test_explicit_bulk_link_tool_still_reports_every_row(workspace):
    """The explicit tool is called with mappings the caller chose; it keeps
    reporting each outcome. Only the automatic replay goes quiet."""
    _seed_spec_and_symbol(workspace)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"spec_id": "core-handle", "title": "Handle a thing"})

        mappings = [{"spec_id": "core-handle", "symbol_qname": "pkg.core.handler"}]
        first = (await c.call_tool("bulk_link_spec_symbols", {"mappings": mappings})).data
        assert first["linked"] == 1
        assert len(first["results"]) == 1

        # Re-linking is a no-op, but the explicit tool still itemises it.
        again = (await c.call_tool("bulk_link_spec_symbols", {"mappings": mappings})).data
        assert again["skipped"] == 1
        assert len(again["results"]) == 1
        assert again["results"][0]["linked"] is False
        assert "results_omitted" not in again
