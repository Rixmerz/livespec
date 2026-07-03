"""Tests for the markdown Spec importer (P2.1)."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.md_specs import parse_specs_markdown
from livespec_mcp.server import mcp

SAMPLE = """\
# Specs

## SPEC-001: Login flow
**Prioridad:** alta · **Módulo:** auth
El usuario se autentica con email + password.

Criterios:
- Token expira en 24h
- Refresh token via /refresh

## SPEC-002: Bulk export
**Priority:** medium
**Module:** export
**Status:** draft

Exportar todos los registros a CSV en background.

## SPEC-3: Cleanup job
**Prioridad:** baja
Job nocturno que purga registros viejos.
"""


def test_parse_basic():
    specs = parse_specs_markdown(SAMPLE)
    assert len(specs) == 3

    rf1 = specs[0]
    assert rf1.spec_id == "SPEC-001"
    assert rf1.title == "Login flow"
    assert rf1.priority == "high"
    assert rf1.module == "auth"
    assert rf1.status == "active"
    assert "Token expira" in rf1.description

    rf2 = specs[1]
    assert rf2.spec_id == "SPEC-002"
    assert rf2.priority == "medium"
    assert rf2.module == "export"
    assert rf2.status == "draft"

    # SPEC-3 normalises to SPEC-003
    rf3 = specs[2]
    assert rf3.spec_id == "SPEC-003"
    assert rf3.priority == "low"


@pytest.mark.asyncio
async def test_import_creates_rfs(sample_repo, tmp_path):
    md = sample_repo / "specs.md"
    md.write_text(SAMPLE)
    async with Client(mcp) as c:
        result = (
            await c.call_tool(
                "import_specs_from_markdown",
                {"path": "specs.md"},
            )
        ).data
        assert result["parsed"] == 3
        assert result["created"] == 3
        assert result["updated"] == 0

        listed = (await c.call_tool("list_specs", {})).data
        spec_ids = {r["spec_id"] for r in listed["specs"]}
        assert {"SPEC-001", "SPEC-002", "SPEC-003"}.issubset(spec_ids)


@pytest.mark.asyncio
async def test_import_is_idempotent(sample_repo):
    md = sample_repo / "specs.md"
    md.write_text(SAMPLE)
    async with Client(mcp) as c:
        first = (
            await c.call_tool(
                "import_specs_from_markdown",
                {"path": "specs.md"},
            )
        ).data
        second = (
            await c.call_tool(
                "import_specs_from_markdown",
                {"path": "specs.md"},
            )
        ).data
        assert first["created"] == 3
        assert second["created"] == 0
        assert second["updated"] == 3
