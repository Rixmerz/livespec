"""Tests for the markdown Spec importer (P2.1)."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.md_specs import (
    detect_spec_format,
    parse_openspec_markdown,
    parse_specs_markdown,
)
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

## SPEC-004: Use event sourcing for orders
**Kind:** adr
Decisión arquitectónica: usar event sourcing en el módulo de órdenes.
"""


def test_parse_basic():
    specs = parse_specs_markdown(SAMPLE)
    assert len(specs) == 4

    rf1 = specs[0]
    assert rf1.spec_id == "SPEC-001"
    assert rf1.title == "Login flow"
    assert rf1.priority == "high"
    assert rf1.module == "auth"
    assert rf1.status == "active"
    assert rf1.kind == "functional_requirement"  # default when unspecified
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

    # SPEC-004 declares a non-default kind
    rf4 = specs[3]
    assert rf4.spec_id == "SPEC-004"
    assert rf4.kind == "adr"


def test_parse_kind_synonyms():
    text = (
        "## SPEC-001: A\n**Tipo:** nfr\ndesc\n\n"
        "## SPEC-002: B\n**Kind:** design\ndesc\n"
    )
    specs = parse_specs_markdown(text)
    assert specs[0].kind == "non_functional_requirement"
    assert specs[1].kind == "design"


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
        assert result["parsed"] == 4
        assert result["created"] == 4
        assert result["updated"] == 0

        listed = (await c.call_tool("list_specs", {})).data
        by_id = {r["spec_id"]: r for r in listed["specs"]}
        assert {"SPEC-001", "SPEC-002", "SPEC-003", "SPEC-004"}.issubset(by_id)
        assert by_id["SPEC-004"]["kind"] == "adr"


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
        assert first["created"] == 4
        assert second["created"] == 0
        assert second["updated"] == 4


# ---------- OpenSpec (Fission-AI) interop ----------

OPENSPEC_SAMPLE = """\
# Theming Specification

## Purpose
Let users control the app's appearance.

## ADDED Requirements

### Requirement: Theme selection
The app SHALL let users switch between light and dark themes,
defaulting to the system preference.

#### Scenario: User toggles dark mode
- **WHEN** the user clicks the theme toggle
- **THEN** the app switches to dark mode and persists the choice

### Requirement: High contrast mode
The app SHALL offer a high-contrast palette for accessibility.

## REMOVED Requirements

### Requirement: Legacy theme cookie
The app used a legacy cookie that is no longer supported.
"""


def test_detect_format():
    assert detect_spec_format(OPENSPEC_SAMPLE) == "openspec"
    assert detect_spec_format(SAMPLE) == "livespec"
    # Mixed → native SPEC-NNN wins so existing imports never change behaviour.
    assert detect_spec_format(SAMPLE + "\n### Requirement: X\ndesc\n") == "livespec"


def test_parse_openspec_basic():
    specs = parse_openspec_markdown(OPENSPEC_SAMPLE, capability="theming")
    by_id = {s.spec_id: s for s in specs}
    assert len(specs) == 3

    theme = by_id["theming-theme-selection"]
    assert theme.title == "Theme selection"
    assert theme.status == "active"
    assert theme.kind == "functional_requirement"
    assert theme.module == "theming"
    assert "SHALL let users switch" in theme.description
    # Scenario block is preserved verbatim in the description.
    assert "WHEN" in theme.description and "THEN" in theme.description
    # The ## Purpose section must not leak into any requirement.
    assert "control the app" not in theme.description

    # A requirement under `## REMOVED Requirements` imports as deprecated.
    assert by_id["theming-legacy-theme-cookie"].status == "deprecated"


@pytest.mark.asyncio
async def test_import_openspec_file(sample_repo):
    md = sample_repo / "spec.md"
    md.write_text(OPENSPEC_SAMPLE)
    async with Client(mcp) as c:
        result = (
            await c.call_tool("import_specs_from_markdown", {"path": "spec.md"})
        ).data
        assert result["parsed"] == 3
        assert result["created"] == 3

        listed = (await c.call_tool("list_specs", {})).data
        by_id = {r["spec_id"]: r for r in listed["specs"]}
        assert "theme-selection" in by_id  # no capability prefix for a bare file
        assert by_id["legacy-theme-cookie"]["status"] == "deprecated"


@pytest.mark.asyncio
async def test_import_openspec_tree(sample_repo):
    tree = sample_repo / "openspec" / "specs" / "theming"
    tree.mkdir(parents=True)
    (tree / "spec.md").write_text(OPENSPEC_SAMPLE)
    async with Client(mcp) as c:
        result = (
            await c.call_tool(
                "import_specs_from_markdown", {"path": "openspec"}
            )
        ).data
        assert result["created"] == 3
        listed = (await c.call_tool("list_specs", {})).data
        by_id = {r["spec_id"]: r for r in listed["specs"]}
        # Capability prefix comes from the `theming/` folder name.
        assert "theming-theme-selection" in by_id


@pytest.mark.asyncio
async def test_import_rejects_bad_fmt(sample_repo):
    (sample_repo / "spec.md").write_text(OPENSPEC_SAMPLE)
    async with Client(mcp) as c:
        result = (
            await c.call_tool(
                "import_specs_from_markdown", {"path": "spec.md", "fmt": "nope"}
            )
        ).data
        assert result.get("isError") is True
