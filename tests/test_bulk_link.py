"""v0.7 B1: bulk_link_rf_symbols — batch RF↔symbol links in one round-trip.

Cuts brownfield migration friction: instead of N round-trips for N
mappings (each one a `link_rf_symbol` call), the agent sends one list
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
        for rf in ("RF-001", "RF-002"):
            await c.call_tool("create_requirement", {"rf_id": rf, "title": rf})

        out = (
            await c.call_tool(
                "bulk_link_rf_symbols",
                {
                    "mappings": [
                        {"rf_id": "RF-001", "symbol_qname": "pkg.auth.login"},
                        {"rf_id": "RF-001", "symbol_qname": "pkg.auth.verify"},
                        {"rf_id": "RF-002", "symbol_qname": "pkg.api.handle",
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
        await c.call_tool("create_requirement", {"rf_id": "RF-A", "title": "A"})
        m = [{"rf_id": "RF-A", "symbol_qname": "pkg.m.f"}]
        out1 = (await c.call_tool("bulk_link_rf_symbols", {"mappings": m})).data
        out2 = (await c.call_tool("bulk_link_rf_symbols", {"mappings": m})).data
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
        await c.call_tool("create_requirement", {"rf_id": "RF-A", "title": "A"})

        out = (
            await c.call_tool(
                "bulk_link_rf_symbols",
                {
                    "mappings": [
                        {"rf_id": "RF-A", "symbol_qname": "pkg.m.f"},
                        {"rf_id": "RF-NONE", "symbol_qname": "pkg.m.f"},
                        {"rf_id": "RF-A", "symbol_qname": "pkg.m.does_not_exist"},
                        {"rf_id": "", "symbol_qname": "pkg.m.f"},  # missing
                    ]
                },
            )
        ).data

    assert out["total"] == 4
    assert out["linked"] == 1
    assert out["failed"] == 3
    error_msgs = [r["error"] for r in out["results"] if r["error"]]
    assert any("RF-NONE" in e for e in error_msgs)
    assert any("does_not_exist" in e for e in error_msgs)
    assert any("required" in e for e in error_msgs)
