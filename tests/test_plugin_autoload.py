"""v0.8 P3.1 — plugin auto-detect framework.

The framework decides which plugin modules load. v0.8 plugin modules are
empty no-ops; these tests lock the SELECTION logic so subsequent phases
that move tools into plugins inherit a stable wiring.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastmcp import FastMCP

from livespec_mcp.state import get_state
from livespec_mcp.tools.plugins import (
    KNOWN_PLUGINS,
    detect_active_plugins,
    register_active,
)


def _seed_rf(state) -> None:
    state.conn.execute(
        "INSERT INTO rf (project_id, rf_id, title) VALUES (?, ?, ?)",
        (state.project_id, "RF-001", "seed"),
    )
    state.conn.commit()


def _seed_doc(state) -> None:
    state.conn.execute(
        "INSERT INTO doc (project_id, target_type, target_key, content)"
        " VALUES (?, ?, ?, ?)",
        (state.project_id, "symbol", "pkg.x", "body"),
    )
    state.conn.commit()


def test_detect_empty_workspace_returns_empty(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state()
    assert detect_active_plugins(state) == set()


def test_detect_rf_rows_activate_rf_plugin(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state()
    _seed_rf(state)
    assert detect_active_plugins(state) == {"rf"}


def test_detect_doc_rows_activate_docs_plugin(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state()
    _seed_doc(state)
    assert detect_active_plugins(state) == {"docs"}


def test_detect_both_rows_activate_both_plugins(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state()
    _seed_rf(state)
    _seed_doc(state)
    assert detect_active_plugins(state) == {"rf", "docs"}


def test_env_none_overrides_db_signal(workspace, monkeypatch):
    state = get_state()
    _seed_rf(state)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "none")
    assert detect_active_plugins(state) == set()


def test_env_all_loads_every_known_plugin_even_on_empty_db(
    workspace, monkeypatch
):
    state = get_state()
    monkeypatch.setenv("LIVESPEC_PLUGINS", "all")
    assert detect_active_plugins(state) == set(KNOWN_PLUGINS)


def test_env_subset_filters_to_named_plugins(workspace, monkeypatch):
    state = get_state()
    _seed_rf(state)
    _seed_doc(state)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "rf")
    assert detect_active_plugins(state) == {"rf"}


def test_env_unknown_plugin_name_is_ignored(workspace, monkeypatch):
    state = get_state()
    monkeypatch.setenv("LIVESPEC_PLUGINS", "rf,bogus,docs")
    assert detect_active_plugins(state) == {"rf", "docs"}


def test_register_active_returns_active_set_and_is_idempotent(
    workspace, monkeypatch
):
    state = get_state()
    _seed_rf(state)
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    mcp = FastMCP(name="test")
    active = register_active(mcp, state)
    assert active == {"rf"}
    # v0.8 plugins are no-ops; calling twice must not raise
    again = register_active(mcp, state)
    assert again == {"rf"}


@pytest.mark.asyncio
async def test_docs_plugin_registers_doc_tools(workspace, monkeypatch):
    """v0.8 P3.5: docs plugin owns generate_docs, list_docs, export_documentation."""
    from fastmcp import Client

    state = get_state()
    monkeypatch.setenv("LIVESPEC_PLUGINS", "docs")
    test_mcp = FastMCP(name="docs-plugin-test")
    register_active(test_mcp, state)

    async with Client(test_mcp) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
    expected_docs = {"generate_docs", "list_docs", "export_documentation"}
    assert expected_docs <= names, f"missing: {expected_docs - names}"


@pytest.mark.asyncio
async def test_rf_plugin_registers_mutation_tools(workspace, monkeypatch):
    """v0.8 P3.4: when the rf plugin loads, mutation tools become callable.

    Verifies the plugin registration plumbing actually wires
    `requirements.register(mutation=True)` into the mcp instance.
    """
    from fastmcp import Client

    state = get_state()
    monkeypatch.setenv("LIVESPEC_PLUGINS", "rf")
    test_mcp = FastMCP(name="rf-plugin-test")
    register_active(test_mcp, state)

    async with Client(test_mcp) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
    # The 11 mutation tools must all be present
    expected_mutation = {
        "create_requirement", "update_requirement", "delete_requirement",
        "link_rf_symbol", "bulk_link_rf_symbols",
        "link_rf_dependency", "unlink_rf_dependency",
        "get_rf_dependency_graph",
        "scan_rf_annotations", "scan_docstrings_for_rf_hints",
        "import_requirements_from_markdown",
    }
    missing = expected_mutation - names
    assert not missing, f"plugin failed to register: {missing}"
    # Agentic tools must NOT be re-registered by the plugin
    assert "list_requirements" not in names
    assert "get_requirement_implementation" not in names


def test_detect_survives_missing_table(workspace, monkeypatch):
    """If a plugin's table doesn't exist (older schema), probe returns False."""
    state = get_state()
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state.conn.execute("DROP TABLE rf_symbol")
    state.conn.execute("DROP TABLE rf_dependency")
    state.conn.execute("DROP TABLE rf")
    state.conn.commit()
    assert "rf" not in detect_active_plugins(state)
