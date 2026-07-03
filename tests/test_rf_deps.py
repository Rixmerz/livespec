"""v0.5 P2: Spec dependency graph — link/unlink, cycle prevention, traversal,
and analyze_impact cascade through dependents."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


async def _create_rfs(client, *spec_ids: str) -> None:
    for spec_id in spec_ids:
        await client.call_tool("create_spec", {"spec_id": spec_id, "title": spec_id})


@pytest.mark.asyncio
async def test_link_and_walk_dependencies(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "SPEC-001", "SPEC-002", "SPEC-003")

        # SPEC-002 requires SPEC-001; SPEC-003 extends SPEC-002
        out = (
            await c.call_tool(
                "link_spec_dependency",
                {"parent_spec_id": "SPEC-002", "child_spec_id": "SPEC-001"},
            )
        ).data
        assert out["linked"] is True
        assert out["kind"] == "requires"
        out = (
            await c.call_tool(
                "link_spec_dependency",
                {"parent_spec_id": "SPEC-003", "child_spec_id": "SPEC-002", "kind": "extends"},
            )
        ).data
        assert out["linked"] is True

        # Forward from SPEC-003: should reach SPEC-002 and SPEC-001
        fwd = (
            await c.call_tool(
                "get_spec_dependency_graph",
                {"spec_id": "SPEC-003", "direction": "forward"},
            )
        ).data
        node_ids = {n["spec_id"] for n in fwd["nodes"]}
        assert {"SPEC-001", "SPEC-002", "SPEC-003"} <= node_ids
        edge_pairs = {(e["parent"], e["child"]) for e in fwd["edges"]}
        assert ("SPEC-003", "SPEC-002") in edge_pairs
        assert ("SPEC-002", "SPEC-001") in edge_pairs

        # Backward from SPEC-001: who depends on me?
        back = (
            await c.call_tool(
                "get_spec_dependency_graph",
                {"spec_id": "SPEC-001", "direction": "backward"},
            )
        ).data
        back_ids = {n["spec_id"] for n in back["nodes"]}
        assert {"SPEC-001", "SPEC-002", "SPEC-003"} <= back_ids


@pytest.mark.asyncio
async def test_cycle_is_rejected(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "Spec-A", "Spec-B", "Spec-C")
        await c.call_tool("link_spec_dependency", {"parent_spec_id": "Spec-A", "child_spec_id": "Spec-B"})
        await c.call_tool("link_spec_dependency", {"parent_spec_id": "Spec-B", "child_spec_id": "Spec-C"})
        # Now Spec-A -> Spec-B -> Spec-C; adding Spec-C -> Spec-A would create a cycle
        out = (
            await c.call_tool(
                "link_spec_dependency",
                {"parent_spec_id": "Spec-C", "child_spec_id": "Spec-A"},
            )
        ).data
        assert out.get("isError") is True
        assert "cycle" in out["error"].lower()


@pytest.mark.asyncio
async def test_self_link_rejected(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "Spec-X")
        out = (
            await c.call_tool(
                "link_spec_dependency",
                {"parent_spec_id": "Spec-X", "child_spec_id": "Spec-X"},
            )
        ).data
        assert out.get("isError") is True


@pytest.mark.asyncio
async def test_unlink(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "Spec-P", "Spec-Q")
        await c.call_tool(
            "link_spec_dependency",
            {"parent_spec_id": "Spec-P", "child_spec_id": "Spec-Q"},
        )
        out = (
            await c.call_tool(
                "unlink_spec_dependency",
                {"parent_spec_id": "Spec-P", "child_spec_id": "Spec-Q"},
            )
        ).data
        assert out["unlinked"] == 1
        # Idempotent: re-running drops 0
        out2 = (
            await c.call_tool(
                "unlink_spec_dependency",
                {"parent_spec_id": "Spec-P", "child_spec_id": "Spec-Q"},
            )
        ).data
        assert out2["unlinked"] == 0


@pytest.mark.asyncio
async def test_analyze_impact_cascades_through_dependents(workspace):
    """analyze_impact(target_type='spec') must include symbols from
    every Spec that transitively depends on the target."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "auth.py").write_text(
        "def verify():\n"
        '    """@spec:SPEC-001"""\n'
        "    return True\n"
    )
    (pkg / "api.py").write_text(
        "from pkg.auth import verify\n"
        "\n"
        "def handle():\n"
        '    """@spec:SPEC-002"""\n'
        "    return verify()\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await _create_rfs(c, "SPEC-001", "SPEC-002")
        await c.call_tool("scan_spec_annotations", {})
        # SPEC-002 (api) requires SPEC-001 (auth)
        await c.call_tool(
            "link_spec_dependency",
            {"parent_spec_id": "SPEC-002", "child_spec_id": "SPEC-001"},
        )

        out = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "spec", "target": "SPEC-001"},
            )
        ).data
        # impact of changing SPEC-001 must mention SPEC-002 as a dependent
        dep_ids = {r["spec_id"] for r in out["dependent_specs"]}
        assert "SPEC-002" in dep_ids, f"SPEC-002 should cascade as dependent: {out}"
        # implementing_symbols must include both auth.verify and api.handle
        impl_qnames = {s["qualified_name"] for s in out["implementing_symbols"]}
        assert "pkg.auth.verify" in impl_qnames
        assert "pkg.api.handle" in impl_qnames
