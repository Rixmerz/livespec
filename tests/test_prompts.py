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
async def test_cross_repo_workflow_prompt_registered():
    async with Client(mcp) as c:
        names = {p.name for p in await c.list_prompts()}
        assert "cross_repo_workflow" in names
        result = await c.get_prompt("cross_repo_workflow")
        text = result.messages[0].content.text
        for needle in (
            "group_db",
            "xrepo-",
            "export_flow_explorer",
            "get_cross_repo_guide",
            "project://cross-repo",
        ):
            assert needle in text, f"{needle} missing from cross_repo_workflow"


def test_cross_repo_guide_file_loads():
    from livespec_mcp.prompts import _CROSS_REPO, _load_cross_repo_guide

    assert _CROSS_REPO.is_file()
    text = _load_cross_repo_guide()
    assert "xrepo-" in text
    assert "group_db" in text

