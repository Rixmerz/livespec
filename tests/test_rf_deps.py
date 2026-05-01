"""v0.5 P2: RF dependency graph — link/unlink, cycle prevention, traversal,
and analyze_impact cascade through dependents."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


async def _create_rfs(client, *rf_ids: str) -> None:
    for rf_id in rf_ids:
        await client.call_tool("create_requirement", {"rf_id": rf_id, "title": rf_id})


@pytest.mark.asyncio
async def test_link_and_walk_dependencies(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "RF-001", "RF-002", "RF-003")

        # RF-002 requires RF-001; RF-003 extends RF-002
        out = (
            await c.call_tool(
                "link_rf_dependency",
                {"parent_rf_id": "RF-002", "child_rf_id": "RF-001"},
            )
        ).data
        assert out["linked"] is True
        assert out["kind"] == "requires"
        out = (
            await c.call_tool(
                "link_rf_dependency",
                {"parent_rf_id": "RF-003", "child_rf_id": "RF-002", "kind": "extends"},
            )
        ).data
        assert out["linked"] is True

        # Forward from RF-003: should reach RF-002 and RF-001
        fwd = (
            await c.call_tool(
                "get_rf_dependency_graph",
                {"rf_id": "RF-003", "direction": "forward"},
            )
        ).data
        node_ids = {n["rf_id"] for n in fwd["nodes"]}
        assert {"RF-001", "RF-002", "RF-003"} <= node_ids
        edge_pairs = {(e["parent"], e["child"]) for e in fwd["edges"]}
        assert ("RF-003", "RF-002") in edge_pairs
        assert ("RF-002", "RF-001") in edge_pairs

        # Backward from RF-001: who depends on me?
        back = (
            await c.call_tool(
                "get_rf_dependency_graph",
                {"rf_id": "RF-001", "direction": "backward"},
            )
        ).data
        back_ids = {n["rf_id"] for n in back["nodes"]}
        assert {"RF-001", "RF-002", "RF-003"} <= back_ids


@pytest.mark.asyncio
async def test_cycle_is_rejected(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "RF-A", "RF-B", "RF-C")
        await c.call_tool("link_rf_dependency", {"parent_rf_id": "RF-A", "child_rf_id": "RF-B"})
        await c.call_tool("link_rf_dependency", {"parent_rf_id": "RF-B", "child_rf_id": "RF-C"})
        # Now RF-A -> RF-B -> RF-C; adding RF-C -> RF-A would create a cycle
        out = (
            await c.call_tool(
                "link_rf_dependency",
                {"parent_rf_id": "RF-C", "child_rf_id": "RF-A"},
            )
        ).data
        assert out.get("isError") is True
        assert "cycle" in out["error"].lower()


@pytest.mark.asyncio
async def test_self_link_rejected(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "RF-X")
        out = (
            await c.call_tool(
                "link_rf_dependency",
                {"parent_rf_id": "RF-X", "child_rf_id": "RF-X"},
            )
        ).data
        assert out.get("isError") is True


@pytest.mark.asyncio
async def test_unlink(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "RF-P", "RF-Q")
        await c.call_tool(
            "link_rf_dependency",
            {"parent_rf_id": "RF-P", "child_rf_id": "RF-Q"},
        )
        out = (
            await c.call_tool(
                "unlink_rf_dependency",
                {"parent_rf_id": "RF-P", "child_rf_id": "RF-Q"},
            )
        ).data
        assert out["unlinked"] == 1
        # Idempotent: re-running drops 0
        out2 = (
            await c.call_tool(
                "unlink_rf_dependency",
                {"parent_rf_id": "RF-P", "child_rf_id": "RF-Q"},
            )
        ).data
        assert out2["unlinked"] == 0


@pytest.mark.asyncio
async def test_v0_5_aliases_still_work(workspace):
    """v0.6 P1: link_requirements / unlink_requirements / get_requirement_dependencies
    were renamed to link_rf_dependency / unlink_rf_dependency / get_rf_dependency_graph
    but the old names remain as deprecated aliases until v0.7."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "RF-OLD", "RF-NEW")
        # Old name still creates the link
        out = (
            await c.call_tool(
                "link_requirements",
                {"parent_rf_id": "RF-OLD", "child_rf_id": "RF-NEW"},
            )
        ).data
        assert out["linked"] is True
        # Old getter still walks
        out = (
            await c.call_tool(
                "get_requirement_dependencies",
                {"rf_id": "RF-OLD", "direction": "forward"},
            )
        ).data
        assert any(n["rf_id"] == "RF-NEW" for n in out["nodes"])
        # Old unlinker still drops
        out = (
            await c.call_tool(
                "unlink_requirements",
                {"parent_rf_id": "RF-OLD", "child_rf_id": "RF-NEW"},
            )
        ).data
        assert out["unlinked"] == 1


@pytest.mark.asyncio
async def test_analyze_impact_cascades_through_dependents(workspace):
    """analyze_impact(target_type='requirement') must include symbols from
    every RF that transitively depends on the target."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "auth.py").write_text(
        "def verify():\n"
        '    """@rf:RF-001"""\n'
        "    return True\n"
    )
    (pkg / "api.py").write_text(
        "from pkg.auth import verify\n"
        "\n"
        "def handle():\n"
        '    """@rf:RF-002"""\n'
        "    return verify()\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "RF-001", "RF-002")
        await c.call_tool("scan_rf_annotations", {})
        # RF-002 (api) requires RF-001 (auth)
        await c.call_tool(
            "link_rf_dependency",
            {"parent_rf_id": "RF-002", "child_rf_id": "RF-001"},
        )

        out = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "requirement", "target": "RF-001"},
            )
        ).data
        # impact of changing RF-001 must mention RF-002 as a dependent
        dep_ids = {r["rf_id"] for r in out["dependent_requirements"]}
        assert "RF-002" in dep_ids, f"RF-002 should cascade as dependent: {out}"
        # implementing_symbols must include both auth.verify and api.handle
        impl_qnames = {s["qualified_name"] for s in out["implementing_symbols"]}
        assert "pkg.auth.verify" in impl_qnames
        assert "pkg.api.handle" in impl_qnames
