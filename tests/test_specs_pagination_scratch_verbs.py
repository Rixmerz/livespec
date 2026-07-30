"""Follow-up fixes from the audit-fix wave:

1. `list_specs` pagination (cursor/summary_only/truncated) — a flat,
   un-paginated dump exceeded MCP's token limit on a 185-spec repo.
2. `scan_annotation_verbs`'s `did_you_mean` uses the payload shape
   (`BE-RF-080`-like) to suggest `@spec`, not pure edit distance
   (which picks `@see` for `@rf`).
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _make_spec_heavy_workspace(workspace, n: int) -> None:
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text("def handler():\n    return 1\n")


@pytest.mark.asyncio
async def test_list_specs_paginates_with_cursor(workspace):
    _make_spec_heavy_workspace(workspace, 5)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        for i in range(5):
            await c.call_tool(
                "create_spec", {"title": f"Spec {i}", "spec_id": f"SPEC-{i:03d}"}
            )

        page1 = (await c.call_tool("list_specs", {"limit": 2, "cursor": 0})).data
        assert len(page1["specs"]) == 2
        assert page1["total"] == 5
        assert page1["truncated"] is True
        assert page1["next_cursor"] == 2

        page2 = (
            await c.call_tool("list_specs", {"limit": 2, "cursor": page1["next_cursor"]})
        ).data
        assert len(page2["specs"]) == 2
        assert page2["next_cursor"] == 4

        page3 = (
            await c.call_tool("list_specs", {"limit": 2, "cursor": page2["next_cursor"]})
        ).data
        assert len(page3["specs"]) == 1
        assert page3["next_cursor"] is None
        assert page3["truncated"] is False

        # union of all pages covers every spec exactly once
        seen = {s["spec_id"] for p in (page1, page2, page3) for s in p["specs"]}
        assert seen == {f"SPEC-{i:03d}" for i in range(5)}


@pytest.mark.asyncio
async def test_list_specs_summary_only_has_no_bodies(workspace):
    _make_spec_heavy_workspace(workspace, 3)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        for i in range(3):
            await c.call_tool(
                "create_spec",
                {
                    "title": f"Spec {i}",
                    "spec_id": f"SPEC-{i:03d}",
                    "description": "x" * 500,
                },
            )
        out = (await c.call_tool("list_specs", {"summary_only": True})).data
        assert out["total"] == 3
        assert set(out["spec_ids"]) == {"SPEC-000", "SPEC-001", "SPEC-002"}
        assert "specs" not in out


@pytest.mark.asyncio
async def test_list_specs_limit_hard_capped(workspace):
    _make_spec_heavy_workspace(workspace, 1)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"title": "One", "spec_id": "SPEC-001"})
        out = (await c.call_tool("list_specs", {"limit": 100_000})).data
        # still returns fine (only 1 spec exists) — the cap governs the SQL
        # LIMIT, not correctness for small sets.
        assert out["total"] == 1
        assert len(out["specs"]) == 1


@pytest.mark.asyncio
async def test_scan_annotation_verbs_suggests_spec_for_rf_payload(workspace):
    """Locks in the fix: @rf:BE-RF-080's did_you_mean must be @spec, not
    the pure-Levenshtein answer @see (distance 3 vs 4 from 'rf')."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text(
        'def suspend_tenant():\n    """@rf:BE-RF-080"""\n    return 1\n'
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_annotation_verbs", {})).data
        group = out["verb_groups"][0]
        assert group["verb"] == "@rf"
        assert group["did_you_mean"] == "@spec"
        assert "BE-RF-080" in group["did_you_mean_reason"]


@pytest.mark.asyncio
async def test_scan_annotation_verbs_falls_back_to_edit_distance_without_spec_payload(
    workspace,
):
    """No spec-id-shaped payload -> falls back to edit distance (unchanged
    behavior for the non-spec-reference case)."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text(
        'def handler():\n    """@sees:something unrelated"""\n    return 1\n'
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_annotation_verbs", {})).data
        group = out["verb_groups"][0]
        assert group["verb"] == "@sees"
        assert group["did_you_mean"] == "@see"
        assert group["did_you_mean_reason"] == "nearest recognized verb by edit distance"
