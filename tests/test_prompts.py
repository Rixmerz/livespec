"""MCP prompt registration and playbook content."""

from __future__ import annotations

from pathlib import Path

from livespec_mcp.prompts import _AGENT_PLAYBOOK, _load_agent_playbook


def test_agent_playbook_file_exists():
    assert _AGENT_PLAYBOOK.is_file(), f"missing {_AGENT_PLAYBOOK}"


def test_agent_playbook_loads_key_sections():
    text = _load_agent_playbook()
    assert "@spec:SPEC-" in text
    assert "index_project" in text
    assert "quick_orient" in text
    assert "bulk_link_spec_symbols" in text
    assert "anti-patterns" in text.lower() or "Anti-patterns" in text


def test_agent_playbook_under_docs():
    assert _AGENT_PLAYBOOK == Path(__file__).resolve().parents[1] / "docs" / "AGENT_PLAYBOOK.md"
