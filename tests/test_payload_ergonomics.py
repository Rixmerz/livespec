"""v0.31.4 payload-ergonomics batch — three findings from a dogfood pass on a
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

3. The Flow Explorer host skipped any repo whose `local_explorer` was null in
   `data.json`. That field records disk state at *export* time, so a Spec
   Explorer generated afterwards stayed unmountable until the flow bundle was
   re-exported. Disk is now the authority.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.flow_explorer import create_flow_host_app


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
    (workspace / "tests" / "test_thing.py").write_text(
        "def _unused_test_helper():\n    return 2\n"
    )

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
    (workspace / "tests" / "test_thing.py").write_text(
        "def _unused_test_helper():\n    return 2\n"
    )

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
    (workspace / "tests" / "test_thing.py").write_text(
        "def _unused_test_helper():\n    return 2\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        default = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        with_tests = (
            await c.call_tool(
                "find_dead_code", {"include_tests": True, "summary_only": True}
            )
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
        json.dumps(
            [{"spec_id": "core-handle-a-thing", "qname": "pkg.core.handler"}]
        )
    )
    (workspace / ".livespec.toml").write_text(
        '[specs]\nopenspec_dir = "openspec"\n'
        'links_seed = "docs/requirements/spec-links.json"\n'
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
        await c.call_tool(
            "create_spec", {"spec_id": "core-handle", "title": "Handle a thing"}
        )

        mappings = [{"spec_id": "core-handle", "symbol_qname": "pkg.core.handler"}]
        first = (
            await c.call_tool("bulk_link_spec_symbols", {"mappings": mappings})
        ).data
        assert first["linked"] == 1
        assert len(first["results"]) == 1

        # Re-linking is a no-op, but the explicit tool still itemises it.
        again = (
            await c.call_tool("bulk_link_spec_symbols", {"mappings": mappings})
        ).data
        assert again["skipped"] == 1
        assert len(again["results"]) == 1
        assert again["results"][0]["linked"] is False
        assert "results_omitted" not in again


# --------------------------------------------------------------------------
# 3. Flow Explorer host: disk beats stale metadata
# --------------------------------------------------------------------------


def _write_bundle(root: Path) -> None:
    bundle = root / ".mcp-docs" / "explorer"
    bundle.mkdir(parents=True)
    (bundle / "index.html").write_text("<h1>spec explorer</h1>")


def test_flow_host_mounts_repo_whose_metadata_predates_its_bundle(tmp_path: Path):
    """`local_explorer` records disk state at export time. A Spec Explorer
    generated afterwards must still mount — this is the bug that made the hop
    target of a live demo 404."""
    repo = tmp_path / "svc-a"
    repo.mkdir()
    _write_bundle(repo)

    flow_dir = tmp_path / "flow-explorer"
    flow_dir.mkdir()
    (flow_dir / "index.html").write_text("<h1>flow</h1>")
    # Exported BEFORE the bundle existed: local_explorer is null.
    (flow_dir / "data.json").write_text(
        json.dumps(
            {"projects": [{"name": "svc-a", "root": str(repo), "local_explorer": None}]}
        )
    )

    _app, mounted = create_flow_host_app(flow_dir)
    assert mounted == ["svc-a"]


def test_flow_host_still_skips_repo_with_no_bundle_on_disk(tmp_path: Path):
    """The fallback derives a path; it must not invent a mount for a repo that
    genuinely has no Explorer bundle."""
    repo = tmp_path / "svc-b"
    repo.mkdir()

    flow_dir = tmp_path / "flow-explorer"
    flow_dir.mkdir()
    (flow_dir / "index.html").write_text("<h1>flow</h1>")
    (flow_dir / "data.json").write_text(
        json.dumps(
            {"projects": [{"name": "svc-b", "root": str(repo), "local_explorer": None}]}
        )
    )

    _app, mounted = create_flow_host_app(flow_dir)
    assert mounted == []


def test_flow_host_prefers_recorded_path_when_it_is_valid(tmp_path: Path):
    """The hint is still used when it points at a real bundle."""
    repo = tmp_path / "svc-c"
    repo.mkdir()
    _write_bundle(repo)

    flow_dir = tmp_path / "flow-explorer"
    flow_dir.mkdir()
    (flow_dir / "index.html").write_text("<h1>flow</h1>")
    (flow_dir / "data.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "svc-c",
                        "root": str(repo),
                        "local_explorer": str(
                            repo / ".mcp-docs" / "explorer" / "index.html"
                        ),
                    }
                ]
            }
        )
    )

    _app, mounted = create_flow_host_app(flow_dir)
    assert mounted == ["svc-c"]
