"""Tests for the markdown RF importer (P2.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.md_rfs import parse_rfs_markdown
from livespec_mcp.server import mcp

SAMPLE = """\
# Requirements

## RF-001: Login flow
**Prioridad:** alta · **Módulo:** auth
El usuario se autentica con email + password.

Criterios:
- Token expira en 24h
- Refresh token via /refresh

## RF-002: Bulk export
**Priority:** medium
**Module:** export
**Status:** draft

Exportar todos los registros a CSV en background.

## RF-3: Cleanup job
**Prioridad:** baja
Job nocturno que purga registros viejos.
"""


def test_parse_basic():
    rfs = parse_rfs_markdown(SAMPLE)
    assert len(rfs) == 3

    rf1 = rfs[0]
    assert rf1.rf_id == "RF-001"
    assert rf1.title == "Login flow"
    assert rf1.priority == "high"
    assert rf1.module == "auth"
    assert rf1.status == "active"
    assert "Token expira" in rf1.description

    rf2 = rfs[1]
    assert rf2.rf_id == "RF-002"
    assert rf2.priority == "medium"
    assert rf2.module == "export"
    assert rf2.status == "draft"

    # RF-3 normalises to RF-003
    rf3 = rfs[2]
    assert rf3.rf_id == "RF-003"
    assert rf3.priority == "low"


@pytest.mark.asyncio
async def test_import_creates_rfs(sample_repo, tmp_path):
    md = sample_repo / "requirements.md"
    md.write_text(SAMPLE)
    async with Client(mcp) as c:
        result = (
            await c.call_tool(
                "import_requirements_from_markdown",
                {"path": "requirements.md"},
            )
        ).data
        assert result["parsed"] == 3
        assert result["created"] == 3
        assert result["updated"] == 0

        listed = (await c.call_tool("list_requirements", {})).data
        rf_ids = {r["rf_id"] for r in listed["requirements"]}
        assert {"RF-001", "RF-002", "RF-003"}.issubset(rf_ids)


@pytest.mark.asyncio
async def test_import_is_idempotent(sample_repo):
    md = sample_repo / "requirements.md"
    md.write_text(SAMPLE)
    async with Client(mcp) as c:
        first = (
            await c.call_tool(
                "import_requirements_from_markdown",
                {"path": "requirements.md"},
            )
        ).data
        second = (
            await c.call_tool(
                "import_requirements_from_markdown",
                {"path": "requirements.md"},
            )
        ).data
        assert first["created"] == 3
        assert second["created"] == 0
        assert second["updated"] == 3
