"""Tests for the OpenSpec markdown Spec importer."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.md_specs import (
    UnsupportedSpecCatalogError,
    parse_openspec_markdown,
    reject_legacy_spec_catalog,
)
from livespec_mcp.server import mcp

LEGACY_SAMPLE = """\
# Specs

## SPEC-001: Login flow
**Prioridad:** alta · **Módulo:** auth
El usuario se autentica con email + password.
"""

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


def test_reject_legacy_catalog():
    with pytest.raises(UnsupportedSpecCatalogError, match="SPEC-NNN"):
        reject_legacy_spec_catalog(LEGACY_SAMPLE)
    with pytest.raises(UnsupportedSpecCatalogError):
        parse_openspec_markdown(LEGACY_SAMPLE)


def test_legacy_header_inside_fence_is_not_rejected():
    """Examples in ``` blocks must not trip the hard-cut detector."""
    text = (
        "### Requirement: Real\n"
        "The system SHALL work.\n\n"
        "```\n"
        "## SPEC-099: Example in a code block\n"
        "```\n"
        "\n"
        "#### Scenario: Ok\n"
        "- **WHEN** x\n"
        "- **THEN** y\n"
    )
    reject_legacy_spec_catalog(text)  # no raise
    specs = parse_openspec_markdown(text)
    assert len(specs) == 1
    assert specs[0].spec_id == "real"


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
    assert "WHEN" in theme.description and "THEN" in theme.description
    assert "control the app" not in theme.description

    assert by_id["theming-legacy-theme-cookie"].status == "deprecated"


@pytest.mark.asyncio
async def test_import_rejects_legacy_catalog(sample_repo):
    (sample_repo / "specs.md").write_text(LEGACY_SAMPLE)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "import_specs_from_markdown",
                {"path": "specs.md"},
            )
        ).data
        assert result.get("isError") is True
        assert "SPEC-NNN" in result["error"]


@pytest.mark.asyncio
async def test_import_openspec_file(sample_repo):
    md = sample_repo / "spec.md"
    md.write_text(OPENSPEC_SAMPLE)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
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
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "import_specs_from_markdown", {"path": "openspec"}
            )
        ).data
        assert result["created"] == 3
        listed = (await c.call_tool("list_specs", {})).data
        by_id = {r["spec_id"]: r for r in listed["specs"]}
        assert "theming-theme-selection" in by_id


@pytest.mark.asyncio
async def test_import_is_idempotent(sample_repo):
    md = sample_repo / "spec.md"
    md.write_text(OPENSPEC_SAMPLE)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        first = (
            await c.call_tool(
                "import_specs_from_markdown",
                {"path": "spec.md"},
            )
        ).data
        second = (
            await c.call_tool(
                "import_specs_from_markdown",
                {"path": "spec.md"},
            )
        ).data
        assert first["created"] == 3
        assert second["created"] == 0
        assert second["updated"] == 3


@pytest.mark.asyncio
async def test_import_rejects_bad_fmt(sample_repo):
    (sample_repo / "spec.md").write_text(OPENSPEC_SAMPLE)
    async with Client(mcp) as c:
        result = (
            await c.call_tool(
                "import_specs_from_markdown", {"path": "spec.md", "fmt": "livespec"}
            )
        ).data
        assert result.get("isError") is True


def test_openspec_livespec_id_comment_overrides_slug():
    """export_openspec markers keep the store id on re-import (slug preferred)."""
    text = (
        "### Requirement: Indexing & workspace walk\n"
        "<!-- livespec:id=indexing-indexing-workspace-walk -->\n"
        "\n"
        "Walk the workspace and persist symbols.\n"
        "\n"
        "#### Scenario: Fresh clone\n"
        "- **WHEN** index_project runs\n"
        "- **THEN** symbols are stored\n"
    )
    specs = parse_openspec_markdown(text, capability="indexing")
    assert len(specs) == 1
    assert specs[0].spec_id == "indexing-indexing-workspace-walk"
    assert specs[0].title == "Indexing & workspace walk"
    assert len(specs[0].scenarios) == 1
