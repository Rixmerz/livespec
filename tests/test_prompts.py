"""MCP prompt registration and playbook content."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.prompts import _AGENT_PLAYBOOK, _load_agent_playbook
from livespec_mcp.server import mcp


def test_agent_playbook_file_exists():
    assert _AGENT_PLAYBOOK.is_file(), f"missing {_AGENT_PLAYBOOK}"


def test_agent_playbook_loads_key_sections():
    text = _load_agent_playbook()
    # The annotation examples teach OpenSpec slugs: an agent copies what it
    # reads here, and a SPEC-NNN example seeds the dialect we migrated off.
    assert "@spec:auth-user-login" in text or "@spec:booking-" in text
    assert "@spec:SPEC-" not in text
    assert "index_project" in text
    assert "quick_orient" in text
    assert "bulk_link_spec_symbols" in text
    assert "anti-patterns" in text.lower() or "Anti-patterns" in text


def test_agent_playbook_under_docs():
    assert _AGENT_PLAYBOOK == Path(__file__).resolve().parents[1] / "docs" / "AGENT_PLAYBOOK.md"


def test_agent_playbook_advertises_openspec_interop():
    """Agents must be able to DISCOVER the OpenSpec compatibility from the guide."""
    text = _load_agent_playbook()
    assert "OpenSpec" in text
    assert "sync_openspec" in text
    assert "link_scenario_symbol" in text


@pytest.mark.asyncio
async def test_openspec_workflow_prompt_registered():
    async with Client(mcp) as c:
        names = {p.name for p in await c.list_prompts()}
        assert "openspec_workflow" in names
        result = await c.get_prompt("openspec_workflow")
        text = result.messages[0].content.text
        for tool in (
            "sync_openspec",
            "export_openspec",
            "validate_openspec",
            "link_scenario_symbol",
            "apply_spec_change",
        ):
            assert tool in text, f"{tool} missing from openspec_workflow prompt"
