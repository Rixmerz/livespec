"""v0.8 P3.1 — plugin auto-detect framework.

The framework decides which plugin modules load. v0.8 plugin modules are
empty no-ops; these tests lock the SELECTION logic so subsequent phases
that move tools into plugins inherit a stable wiring.
"""

from __future__ import annotations

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
        "INSERT INTO spec (project_id, spec_id, title) VALUES (?, ?, ?)",
        (state.project_id, "SPEC-001", "seed"),
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
    state = get_state(create=True)
    assert detect_active_plugins(state) == set()


def test_detect_rf_rows_activate_rf_plugin(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state(create=True)
    _seed_rf(state)
    assert detect_active_plugins(state) == {"spec"}


def test_detect_doc_rows_activate_docs_plugin(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state(create=True)
    _seed_doc(state)
    assert detect_active_plugins(state) == {"docs"}


def test_explorer_bundle_on_disk_activates_docs_plugin(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state(create=True)
    out = workspace / ".mcp-docs" / "explorer"
    out.mkdir(parents=True)
    (out / "index.html").write_text("<html></html>", encoding="utf-8")
    assert detect_active_plugins(state) == {"docs"}


def test_detect_both_rows_activate_both_plugins(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state = get_state(create=True)
    _seed_rf(state)
    _seed_doc(state)
    assert detect_active_plugins(state) == {"spec", "docs"}


def test_env_none_overrides_db_signal(workspace, monkeypatch):
    state = get_state(create=True)
    _seed_rf(state)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "none")
    assert detect_active_plugins(state) == set()


def test_env_all_loads_every_known_plugin_even_on_empty_db(
    workspace, monkeypatch
):
    state = get_state(create=True)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "all")
    assert detect_active_plugins(state) == set(KNOWN_PLUGINS)


def test_env_subset_filters_to_named_plugins(workspace, monkeypatch):
    state = get_state(create=True)
    _seed_rf(state)
    _seed_doc(state)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "spec")
    assert detect_active_plugins(state) == {"spec"}


def test_env_unknown_plugin_name_is_ignored(workspace, monkeypatch):
    state = get_state(create=True)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "spec,bogus,docs")
    assert detect_active_plugins(state) == {"spec", "docs"}


def test_register_active_returns_active_set_and_is_idempotent(
    workspace, monkeypatch
):
    state = get_state(create=True)
    _seed_rf(state)
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    mcp = FastMCP(name="test")
    active = register_active(mcp, state)
    assert active == {"spec"}
    # v0.8 plugins are no-ops; calling twice must not raise
    again = register_active(mcp, state)
    assert again == {"spec"}


@pytest.mark.asyncio
async def test_docs_plugin_registers_doc_tools(workspace, monkeypatch):
    """v0.8 P3.5: docs plugin owns generate_docs, list_docs, export_documentation."""
    from fastmcp import Client

    state = get_state(create=True)
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
    """v0.8 P3.4: when the spec plugin loads, mutation tools become callable.

    Verifies the plugin registration plumbing actually wires
    `specs.register(mutation=True)` into the mcp instance.
    """
    from fastmcp import Client

    state = get_state(create=True)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "spec")
    test_mcp = FastMCP(name="spec-plugin-test")
    register_active(test_mcp, state)

    async with Client(test_mcp) as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
    # Mutation tools that must be present in the plugin surface.
    # `bulk_link_spec_symbols` was promoted to the agentic surface (default
    # tier) so agents can wire annotations on languages where the
    # in-source extractor doesn't yet read tags — it is therefore NOT
    # re-registered by the plugin.
    expected_mutation = {
        "create_spec", "update_spec", "delete_spec",
        "link_spec_symbol",
        "link_spec_dependency", "unlink_spec_dependency",
        "get_spec_dependency_graph",
        "scan_spec_annotations", "scan_docstrings_for_spec_hints",
        "import_specs_from_markdown",
    }
    missing = expected_mutation - names
    assert not missing, f"plugin failed to register: {missing}"
    # Agentic tools must NOT be re-registered by the plugin
    assert "list_specs" not in names
    assert "get_spec_implementation" not in names
    assert "bulk_link_spec_symbols" not in names


def test_detect_survives_missing_table(workspace, monkeypatch):
    """If a plugin's table doesn't exist (older schema), probe returns False."""
    state = get_state(create=True)
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    state.conn.execute("DROP TABLE spec_symbol")
    state.conn.execute("DROP TABLE spec_dependency")
    state.conn.execute("DROP TABLE spec")
    state.conn.commit()
    assert "spec" not in detect_active_plugins(state)


def test_env_unknown_only_value_falls_back_to_detection(workspace, monkeypatch):
    """A typo like LIVESPEC_PLUGINS=specs must not silently hide every plugin:
    with no valid names in the override, DB detection wins (v0.20)."""
    state = get_state(create=True)
    monkeypatch.setenv("LIVESPEC_PLUGINS", "specs")
    assert detect_active_plugins(state) == set()  # no rows yet
    _seed_rf(state)
    assert detect_active_plugins(state) == {"spec"}
