"""v0.7 B1: bulk_link_spec_symbols — batch Spec↔symbol links in one round-trip.

Cuts brownfield migration friction: instead of N round-trips for N
mappings (each one a `link_spec_symbol` call), the agent sends one list
and gets per-entry results.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_bulk_link_happy_path(workspace):
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "auth.py").write_text(
        "def login():\n    return True\n"
        "\n"
        "def verify():\n    return True\n"
    )
    (pkg / "api.py").write_text(
        "def handle():\n    return None\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        for spec in ("SPEC-001", "SPEC-002"):
            await c.call_tool("create_spec", {"spec_id": spec, "title": spec})

        out = (
            await c.call_tool(
                "bulk_link_spec_symbols",
                {
                    "mappings": [
                        {"spec_id": "SPEC-001", "symbol_qname": "pkg.auth.login"},
                        {"spec_id": "SPEC-001", "symbol_qname": "pkg.auth.verify"},
                        {"spec_id": "SPEC-002", "symbol_qname": "pkg.api.handle",
                         "confidence": 0.85, "source": "embedding"},
                    ]
                },
            )
        ).data
    assert out["total"] == 3
    assert out["linked"] == 3
    assert out["skipped"] == 0
    assert out["failed"] == 0
    for r in out["results"]:
        assert r["ok"] is True


@pytest.mark.asyncio
async def test_bulk_link_idempotent(workspace):
    """Re-linking the same pair returns ok=True linked=False (skipped)."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text("def f():\n    return 1\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"spec_id": "Spec-A", "title": "A"})
        m = [{"spec_id": "Spec-A", "symbol_qname": "pkg.m.f"}]
        out1 = (await c.call_tool("bulk_link_spec_symbols", {"mappings": m})).data
        out2 = (await c.call_tool("bulk_link_spec_symbols", {"mappings": m})).data
    assert out1["linked"] == 1
    assert out2["linked"] == 0
    assert out2["skipped"] == 1
    assert all(r["ok"] for r in out2["results"])


@pytest.mark.asyncio
async def test_bulk_link_partial_failure(workspace):
    """Mixing valid + invalid mappings: returns per-entry results without
    failing the whole batch."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text("def f():\n    return 1\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"spec_id": "Spec-A", "title": "A"})

        out = (
            await c.call_tool(
                "bulk_link_spec_symbols",
                {
                    "mappings": [
                        {"spec_id": "Spec-A", "symbol_qname": "pkg.m.f"},
                        {"spec_id": "Spec-NONE", "symbol_qname": "pkg.m.f"},
                        {"spec_id": "Spec-A", "symbol_qname": "pkg.m.does_not_exist"},
                        {"spec_id": "", "symbol_qname": "pkg.m.f"},  # missing
                    ]
                },
            )
        ).data

    assert out["total"] == 4
    assert out["linked"] == 1
    assert out["failed"] == 3
    error_msgs = [r["error"] for r in out["results"] if r["error"]]
    assert any("Spec-NONE" in e for e in error_msgs)
    assert any("does_not_exist" in e for e in error_msgs)
    assert any("required" in e for e in error_msgs)


@pytest.mark.asyncio
async def test_manual_links_survive_force_reindex(workspace):
    """Data-loss regression: `index_project(force=True)` cascades through
    `symbol` → `spec_symbol`, which used to silently wipe links created by
    `bulk_link_spec_symbols` / `link_spec_symbol`. The indexer now snapshots
    non-annotation spec_symbol rows before re-extract and restores them by
    qname after symbols are re-inserted."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "auth.py").write_text("def login():\n    return True\n")
    (pkg / "api.py").write_text("def handle():\n    return None\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"spec_id": "SPEC-001", "title": "auth"})
        await c.call_tool("create_spec", {"spec_id": "SPEC-002", "title": "api"})
        bl = (await c.call_tool(
            "bulk_link_spec_symbols",
            {"mappings": [
                {"spec_id": "SPEC-001", "symbol_qname": "pkg.auth.login"},
                {"spec_id": "SPEC-002", "symbol_qname": "pkg.api.handle",
                 "confidence": 0.85, "source": "embedding"},
            ]},
        )).data
        assert bl["linked"] == 2

        # Force re-extract — pre-fix, this dropped both manual links to 0.
        idx = (await c.call_tool("index_project", {"force": True})).data
        assert idx["manual_links_restored"] == 2

        impl_001 = (await c.call_tool(
            "get_spec_implementation", {"spec_id": "SPEC-001"}
        )).data
        impl_002 = (await c.call_tool(
            "get_spec_implementation", {"spec_id": "SPEC-002"}
        )).data

    qnames_001 = {s["qualified_name"] for s in impl_001["symbols"]}
    qnames_002 = {s["qualified_name"] for s in impl_002["symbols"]}
    assert "pkg.auth.login" in qnames_001
    assert "pkg.api.handle" in qnames_002
