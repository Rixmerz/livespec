"""OpenSpec (Fission-AI) round-trip + change-lifecycle interop (v0.22)."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.md_specs import extract_scenarios, parse_openspec_markdown
from livespec_mcp.server import mcp
from livespec_mcp.storage.db import connect

CANONICAL = """\
# Theming Specification

## Purpose
Let users control the app's appearance.

## Requirements

### Requirement: Theme selection
The app SHALL let users switch between light and dark themes.

#### Scenario: User toggles dark mode
- **WHEN** the user clicks the theme toggle
- **THEN** the app switches to dark mode and persists the choice
"""

CHANGE_PROPOSAL = "# Add high contrast\n\nWhy: accessibility.\n"
CHANGE_DELTA = """\
## ADDED Requirements

### Requirement: High contrast mode
The app SHALL offer a high-contrast palette.

#### Scenario: Enable high contrast
- **WHEN** the user turns on high contrast
- **THEN** the palette switches to high-contrast tokens

## REMOVED Requirements

### Requirement: Theme selection
Superseded by the new theming engine.
"""


# ---------- migration / schema ----------


def test_fresh_db_has_openspec_tables(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"spec_scenario", "spec_change", "spec_change_delta"} <= tables
    conn.close()


# ---------- scenario parsing ----------


def test_extract_scenarios():
    scen = extract_scenarios(
        "prose\n\n#### Scenario: A\n- **WHEN** x\n- **THEN** y\n"
        "#### Scenario: B\n- **WHEN** p\n- **THEN** q\n"
    )
    assert [s[0] for s in scen] == ["A", "B"]
    assert "**WHEN** x" in scen[0][1] and "**THEN** y" in scen[0][1]


def test_parse_openspec_populates_scenarios_and_operation():
    specs = parse_openspec_markdown(CANONICAL, capability="theming")
    theme = specs[0]
    assert theme.spec_id == "theming-theme-selection"
    assert len(theme.scenarios) == 1
    assert theme.scenarios[0][0] == "User toggles dark mode"
    assert theme.operation is None  # canonical, no delta header

    deltas = parse_openspec_markdown(CHANGE_DELTA, capability="theming")
    by_id = {s.spec_id: s for s in deltas}
    assert by_id["theming-high-contrast-mode"].operation == "added"
    assert by_id["theming-theme-selection"].operation == "removed"


# ---------- import persists scenarios ----------


@pytest.mark.asyncio
async def test_import_persists_scenarios(sample_repo):
    (sample_repo / "spec.md").write_text(CANONICAL)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("import_specs_from_markdown", {"path": "spec.md"})
        listed = (await c.call_tool("list_specs", {})).data
        by_id = {r["spec_id"]: r for r in listed["specs"]}
        assert by_id["theme-selection"]["scenario_count"] == 1

        impl = (
            await c.call_tool(
                "get_spec_implementation", {"spec_id": "theme-selection"}
            )
        ).data
        assert impl["coverage"]["scenario_count"] == 1
        assert impl["scenarios"][0]["name"] == "User toggles dark mode"
        assert "WHEN" in impl["scenarios"][0]["body"]


@pytest.mark.asyncio
async def test_native_reimport_keeps_scenarios(sample_repo):
    """A native ## SPEC-NNN re-import must not wipe scenarios another import set."""
    (sample_repo / "os.md").write_text(CANONICAL)
    (sample_repo / "native.md").write_text("## SPEC-010: Something\ndesc\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("import_specs_from_markdown", {"path": "os.md"})
        await c.call_tool("import_specs_from_markdown", {"path": "native.md"})
        impl = (
            await c.call_tool(
                "get_spec_implementation", {"spec_id": "theme-selection"}
            )
        ).data
        assert impl["coverage"]["scenario_count"] == 1


# ---------- validate ----------


@pytest.mark.asyncio
async def test_validate_openspec_flags_missing_scenario(sample_repo):
    (sample_repo / "spec.md").write_text(CANONICAL)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("import_specs_from_markdown", {"path": "spec.md"})
        # Add a spec with no scenario via create_spec.
        await c.call_tool(
            "create_spec",
            {"title": "No scenario", "description": "The app SHALL do X.", "spec_id": "SPEC-900"},
        )
        loose = (await c.call_tool("validate_openspec", {})).data
        assert "SPEC-900" in loose["specs_without_scenarios"]
        assert loose["valid"] is True  # non-strict: scenario gap is a warning

        strict = (await c.call_tool("validate_openspec", {"strict": True})).data
        assert strict["valid"] is False
        assert strict["error_count"] >= 1


# ---------- export round-trip ----------


@pytest.mark.asyncio
async def test_export_roundtrip(sample_repo):
    tree = sample_repo / "openspec" / "specs" / "theming"
    tree.mkdir(parents=True)
    (tree / "spec.md").write_text(CANONICAL)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        result = (await c.call_tool("export_openspec", {"out_dir": "out"})).data
        assert result["specs_written"] == 1
        assert result["scenarios_written"] == 1

    exported = (sample_repo / "out" / "specs" / "theming" / "spec.md").read_text()
    assert "### Requirement: Theme selection" in exported
    assert "#### Scenario: User toggles dark mode" in exported
    assert "<!-- livespec:id=theming-theme-selection -->" in exported
    # Re-parse the exported file: id + scenario survive the round-trip.
    reparsed = parse_openspec_markdown(exported, capability="theming")
    assert reparsed[0].spec_id == "theming-theme-selection"
    assert len(reparsed[0].scenarios) == 1


@pytest.mark.asyncio
async def test_export_preserves_numeric_spec_id(sample_repo):
    """create_spec(SPEC-001) → export → sync must not mint a duplicate slug id."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec",
            {
                "spec_id": "SPEC-001",
                "title": "Indexing & workspace walk",
                "module": "indexing",
                "description": "Walk the workspace.",
                "status": "active",
            },
        )
        result = (await c.call_tool("export_openspec", {"out_dir": "out"})).data
        assert result["specs_written"] == 1

    exported = (sample_repo / "out" / "specs" / "indexing" / "spec.md").read_text()
    assert "<!-- livespec:id=SPEC-001 -->" in exported
    reparsed = parse_openspec_markdown(exported, capability="indexing")
    assert reparsed[0].spec_id == "SPEC-001"

    # Import into a second pass: still SPEC-001, not indexing-indexing-...
    async with Client(mcp) as c:
        synced = (
            await c.call_tool(
                "import_specs_from_markdown",
                {"path": "out/specs/indexing/spec.md", "fmt": "openspec"},
            )
        ).data
        assert synced.get("updated", 0) + synced.get("created", 0) >= 1
        listed = (await c.call_tool("list_specs", {})).data
        ids = {s["spec_id"] for s in listed["specs"]}
        assert "SPEC-001" in ids
        assert not any(
            s.startswith("indexing-") and s != "SPEC-001" for s in ids
        )


# ---------- change lifecycle ----------


@pytest.mark.asyncio
async def test_change_lifecycle(sample_repo):
    root = sample_repo / "openspec"
    (root / "specs" / "theming").mkdir(parents=True)
    (root / "specs" / "theming" / "spec.md").write_text(CANONICAL)
    change = root / "changes" / "add-high-contrast"
    (change / "specs" / "theming").mkdir(parents=True)
    (change / "proposal.md").write_text(CHANGE_PROPOSAL)
    (change / "specs" / "theming" / "spec.md").write_text(CHANGE_DELTA)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        synced = (await c.call_tool("sync_openspec", {})).data
        assert synced["changes"]["count"] == 1

        changes = (await c.call_tool("list_spec_changes", {})).data["changes"]
        assert changes[0]["name"] == "add-high-contrast"
        assert changes[0]["status"] == "proposed"
        assert changes[0]["delta_count"] == 2

        detail = (
            await c.call_tool("get_spec_change", {"name": "add-high-contrast"})
        ).data
        assert "accessibility" in detail["proposal"]
        ops = {d["operation"] for d in detail["deltas"]}
        assert ops == {"added", "removed"}

        # Apply: adds high-contrast, deprecates theme-selection.
        applied = (
            await c.call_tool("apply_spec_change", {"name": "add-high-contrast"})
        ).data
        assert applied["applied"]["added"] == 1
        assert applied["applied"]["removed"] == 1

        active = (await c.call_tool("list_specs", {"status": "active"})).data["specs"]
        active_ids = {s["spec_id"] for s in active}
        assert "theming-high-contrast-mode" in active_ids
        assert "theming-theme-selection" not in active_ids

        deprecated = (
            await c.call_tool("list_specs", {"status": "deprecated"})
        ).data["specs"]
        assert any(s["spec_id"] == "theming-theme-selection" for s in deprecated)

        # Archive.
        arch = (
            await c.call_tool("archive_spec_change", {"name": "add-high-contrast"})
        ).data
        assert arch["status"] == "archived"
        listed = (await c.call_tool("list_spec_changes", {"status": "archived"})).data
        assert len(listed["changes"]) == 1


# ---------- scenario-level traceability ----------


@pytest.mark.asyncio
async def test_scenario_level_traceability(sample_repo):
    (sample_repo / "spec.md").write_text(CANONICAL)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("import_specs_from_markdown", {"path": "spec.md"})
        linked = (
            await c.call_tool(
                "link_scenario_symbol",
                {
                    "spec_id": "theme-selection",
                    "scenario_name": "User toggles dark mode",
                    "symbol_qname": "pkg.auth.login",
                },
            )
        ).data
        assert linked["linked"] is True

        impl = (
            await c.call_tool(
                "get_spec_implementation", {"spec_id": "theme-selection"}
            )
        ).data
        scen = impl["scenarios"][0]
        assert scen["verified"] is True
        assert scen["symbols"][0]["qualified_name"] == "pkg.auth.login"
        assert impl["coverage"]["scenarios_verified"] == 1

        # Re-import must PRESERVE the scenario link (upsert, not delete+insert).
        await c.call_tool("import_specs_from_markdown", {"path": "spec.md"})
        impl2 = (
            await c.call_tool(
                "get_spec_implementation", {"spec_id": "theme-selection"}
            )
        ).data
        assert impl2["scenarios"][0]["verified"] is True

        # Unlink.
        await c.call_tool(
            "link_scenario_symbol",
            {
                "spec_id": "theme-selection",
                "scenario_name": "User toggles dark mode",
                "symbol_qname": "pkg.auth.login",
                "unlink": True,
            },
        )
        impl3 = (
            await c.call_tool(
                "get_spec_implementation", {"spec_id": "theme-selection"}
            )
        ).data
        assert impl3["scenarios"][0]["verified"] is False


@pytest.mark.asyncio
async def test_link_scenario_bad_name_errors(sample_repo):
    (sample_repo / "spec.md").write_text(CANONICAL)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("import_specs_from_markdown", {"path": "spec.md"})
        res = (
            await c.call_tool(
                "link_scenario_symbol",
                {
                    "spec_id": "theme-selection",
                    "scenario_name": "does not exist",
                    "symbol_qname": "pkg.auth.login",
                },
            )
        ).data
        assert res.get("isError") is True


@pytest.mark.asyncio
async def test_export_includes_archived_change(sample_repo):
    root = sample_repo / "openspec"
    (root / "specs" / "theming").mkdir(parents=True)
    (root / "specs" / "theming" / "spec.md").write_text(CANONICAL)
    change = root / "changes" / "add-high-contrast"
    (change / "specs" / "theming").mkdir(parents=True)
    (change / "proposal.md").write_text(CHANGE_PROPOSAL)
    (change / "specs" / "theming" / "spec.md").write_text(CHANGE_DELTA)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        await c.call_tool("archive_spec_change", {"name": "add-high-contrast"})
        result = (await c.call_tool("export_openspec", {"out_dir": "out"})).data
        assert result["changes_written"] == 1

    arch = sample_repo / "out" / "archive" / "add-high-contrast"
    assert (arch / "proposal.md").is_file()
    delta = (arch / "specs" / "theming" / "spec.md").read_text()
    assert "## ADDED Requirements" in delta
    assert "### Requirement: High contrast mode" in delta


@pytest.mark.asyncio
async def test_sync_openspec_missing_dir_errors(sample_repo):
    async with Client(mcp) as c:
        result = (await c.call_tool("sync_openspec", {})).data
        assert result.get("isError") is True


@pytest.mark.asyncio
async def test_apply_unknown_change_errors(sample_repo):
    async with Client(mcp) as c:
        result = (await c.call_tool("apply_spec_change", {"name": "nope"})).data
        assert result.get("isError") is True


# ---------- Tier 2: RENAMED, Purpose round-trip, apply validation ----------

RENAME_DELTA = """\
## RENAMED Requirements

- FROM: `### Requirement: Theme selection`
- TO: `### Requirement: Theme picker`
"""

MODIFY_MISSING_DELTA = """\
## MODIFIED Requirements

### Requirement: Nonexistent thing
The app SHALL do something that was never specified.

#### Scenario: S
- **WHEN** x
- **THEN** y
"""


def test_parse_renamed_delta():
    specs = parse_openspec_markdown(RENAME_DELTA, capability="theming")
    assert len(specs) == 1
    r = specs[0]
    assert r.operation == "renamed"
    assert r.title == "Theme picker"
    assert r.spec_id == "theming-theme-picker"
    assert r.rename_from == "Theme selection"


@pytest.mark.asyncio
async def test_rename_migrates_links(sample_repo):
    root = sample_repo / "openspec"
    (root / "specs" / "theming").mkdir(parents=True)
    (root / "specs" / "theming" / "spec.md").write_text(CANONICAL)
    change = root / "changes" / "rename-theme"
    (change / "specs" / "theming").mkdir(parents=True)
    (change / "proposal.md").write_text("# Rename theme selection\n")
    (change / "specs" / "theming" / "spec.md").write_text(RENAME_DELTA)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "theming-theme-selection", "symbol_qname": "pkg.auth.login"},
        )
        res = (await c.call_tool("apply_spec_change", {"name": "rename-theme"})).data
        assert res["applied"]["renamed"] == 1

        ids = {s["spec_id"] for s in (await c.call_tool("list_specs", {})).data["specs"]}
        assert "theming-theme-picker" in ids
        assert "theming-theme-selection" not in ids  # old spec is gone

        impl = (
            await c.call_tool(
                "get_spec_implementation", {"spec_id": "theming-theme-picker"}
            )
        ).data
        # The code link AND the scenario migrated from the old spec.
        assert any(s["qualified_name"] == "pkg.auth.login" for s in impl["symbols"])
        assert any(s["name"] == "User toggles dark mode" for s in impl["scenarios"])


@pytest.mark.asyncio
async def test_purpose_roundtrip(sample_repo):
    tree = sample_repo / "openspec" / "specs" / "theming"
    tree.mkdir(parents=True)
    (tree / "spec.md").write_text(CANONICAL)  # has a `## Purpose` section
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        await c.call_tool("export_openspec", {"out_dir": "out"})
    exported = (sample_repo / "out" / "specs" / "theming" / "spec.md").read_text()
    # The stored Purpose is re-emitted verbatim, not the synthesized placeholder.
    assert "Let users control the app's appearance." in exported
    assert "Exported by livespec." not in exported


ADDED_ONLY_DELTA = """\
## ADDED Requirements

### Requirement: Brand new thing
The app SHALL do a brand new thing.

#### Scenario: S
- **WHEN** x
- **THEN** y
"""


@pytest.mark.asyncio
async def test_sync_nested_changes_archive_layout(sample_repo):
    """Real OpenSpec repos nest archives at openspec/changes/archive/ — the
    `archive` subdir must not become a phantom change, and its contents must be
    ingested as archived. (Battle-test finding vs Fission-AI/OpenSpec.)"""
    root = sample_repo / "openspec"
    act = root / "changes" / "add-thing"
    (act / "specs" / "cap").mkdir(parents=True)
    (act / "proposal.md").write_text("# add a thing\n")
    (act / "specs" / "cap" / "spec.md").write_text(ADDED_ONLY_DELTA)
    arch = root / "changes" / "archive" / "2025-01-01-old-thing"
    (arch / "specs" / "cap").mkdir(parents=True)
    (arch / "proposal.md").write_text("# old thing\n")
    (arch / "specs" / "cap" / "spec.md").write_text(ADDED_ONLY_DELTA)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        changes = (await c.call_tool("list_spec_changes", {})).data["changes"]
        by_name = {ch["name"]: ch["status"] for ch in changes}
        assert "archive" not in by_name  # no phantom change
        assert by_name.get("add-thing") == "proposed"
        assert by_name.get("2025-01-01-old-thing") == "archived"


@pytest.mark.asyncio
async def test_sync_no_specs_dir_does_not_slurp_deltas(sample_repo):
    """A change-only tree (no openspec/specs/) must NOT import in-flight change
    deltas as canonical source-of-truth specs. (Battle-test finding.)"""
    root = sample_repo / "openspec"
    ch = root / "changes" / "add-thing"
    (ch / "specs" / "cap").mkdir(parents=True)
    (ch / "specs" / "cap" / "spec.md").write_text(ADDED_ONLY_DELTA)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (await c.call_tool("sync_openspec", {})).data
        assert result["specs"].get("created", 0) == 0
        assert result["specs"].get("note")  # explains why canonical is empty
        assert (await c.call_tool("list_specs", {})).data["specs"] == []
        # But the change WAS ingested.
        assert (await c.call_tool("list_spec_changes", {})).data["changes"]


@pytest.mark.asyncio
async def test_apply_dry_run_and_warnings(sample_repo):
    root = sample_repo / "openspec"
    (root / "specs" / "theming").mkdir(parents=True)
    (root / "specs" / "theming" / "spec.md").write_text(CANONICAL)
    change = root / "changes" / "mod-missing"
    (change / "specs" / "theming").mkdir(parents=True)
    (change / "proposal.md").write_text("# Modify a spec that doesn't exist\n")
    (change / "specs" / "theming" / "spec.md").write_text(MODIFY_MISSING_DELTA)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})

        dry = (
            await c.call_tool(
                "apply_spec_change", {"name": "mod-missing", "dry_run": True}
            )
        ).data
        assert dry["dry_run"] is True
        assert dry["plan"]["modified"] == 1
        assert any("does not exist" in w["issue"] for w in dry["warnings"])

        # Dry run mutated nothing: spec absent, change still proposed.
        ids = {s["spec_id"] for s in (await c.call_tool("list_specs", {})).data["specs"]}
        assert "theming-nonexistent-thing" not in ids
        changes = (await c.call_tool("list_spec_changes", {})).data["changes"]
        assert changes[0]["status"] == "proposed"

        # Real apply creates it and still surfaces the warning.
        applied = (
            await c.call_tool("apply_spec_change", {"name": "mod-missing"})
        ).data
        assert applied["applied"]["modified"] == 1
        assert applied["warnings"]
