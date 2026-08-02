"""v0.23: validate_openspec output is aggregated (grouped findings, bounded
sample), not one flat entry per spec — a real 185-spec store produced a
53KB+ response under the old shape. Also covers the store-hygiene report.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from livespec_mcp.domain.openspec_validate import validate_openspec
from livespec_mcp.server import mcp
from livespec_mcp.state import get_state


@pytest.mark.asyncio
async def test_findings_grouped_and_project_level_flag_on_large_corpus(workspace):
    """25 specs, none with scenarios -> ONE grouped finding, not 25 flat
    entries, and it's flagged project_level (>=20 checked, >=95% affected)."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        for i in range(25):
            await c.call_tool(
                "create_spec",
                {
                    "spec_id": f"validate-spec-{i:03d}",
                    "title": f"Spec {i}",
                    "description": "The system SHALL do the thing.",
                },
            )
        result = (await c.call_tool("validate_openspec", {})).data

    assert result["checked"] == 25
    no_scenario = [
        f for f in result["findings"] if f["issue"].startswith("requirement has no scenario")
    ]
    assert len(no_scenario) == 1  # grouped, not 25 separate entries
    finding = no_scenario[0]
    assert finding["count"] == 25
    assert finding["project_level"] is True
    assert len(finding["sample"]) <= 10  # bounded, not all 25
    assert finding["sample_truncated"] is True

    # Response stays small regardless of corpus size.
    assert len(json.dumps(result)) < 5000


@pytest.mark.asyncio
async def test_valid_reflects_only_errors_not_warnings(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec",
            {"spec_id": "handmade-rule", "title": "No scenario", "description": "The app SHALL do X."},
        )
        loose = (await c.call_tool("validate_openspec", {})).data
        strict = (await c.call_tool("validate_openspec", {"strict": True})).data

    assert loose["valid"] is True
    assert loose["warning_count"] >= 1
    assert loose["error_count"] == 0
    assert "handmade-rule" in loose["specs_without_scenarios"]

    # Strict: the same finding is promoted to an error -> valid flips.
    assert strict["valid"] is False
    assert strict["error_count"] >= 1


def test_hygiene_flags_obsolete_marker_and_prefix_mismatch(workspace):
    st = get_state(str(workspace), create=True)
    pid = st.project_id
    st.conn.execute(
        """INSERT INTO spec(project_id, spec_id, kind, title, description, status)
           VALUES (?,?,?,?,?,?)""",
        (pid, "FE-RF-001", "functional_requirement", "Normal", "The app SHALL work.", "active"),
    )
    st.conn.execute(
        """INSERT INTO spec(project_id, spec_id, kind, title, description, status)
           VALUES (?,?,?,?,?,?)""",
        (pid, "FE-RF-002", "functional_requirement", "Also normal", "The app SHALL work too.", "active"),
    )
    st.conn.execute(
        """INSERT INTO spec(project_id, spec_id, kind, title, description, status)
           VALUES (?,?,?,?,?,?)""",
        (
            pid,
            "MCP-RF-001",
            "functional_requirement",
            "Stray mis-prefixed entry",
            "OBSOLETE — parser probe, delete from store",
            "active",
        ),
    )
    st.conn.commit()

    result = validate_openspec(st.conn, pid)
    hygiene = result["hygiene"]
    assert hygiene["dominant_prefix"] == "FE-RF"
    assert "MCP-RF-001" in hygiene["mismatched_prefix_specs"]
    assert hygiene["mismatched_prefix_count"] == 1
    assert "MCP-RF-001" in hygiene["obsolete_marked_specs"]
    assert hygiene["obsolete_marked_count"] == 1
