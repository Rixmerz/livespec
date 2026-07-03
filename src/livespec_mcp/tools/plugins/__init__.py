"""Plugin auto-detect framework (v0.8 P3.1).

The default surface is code-intel + Spec-agentic tools that any agent on
any codebase will reach for. Mutation/management tools live in plugins
that load only when the workspace's DB shows they're relevant:

    livespec-spec  -> spec table has rows for the active project
    livespec-docs  -> doc table has rows for the active project

The DB-state detection is a soft default. Power users override it with the
``LIVESPEC_PLUGINS`` env var:

    LIVESPEC_PLUGINS=none        no plugins load
    LIVESPEC_PLUGINS=all         every plugin loads
    LIVESPEC_PLUGINS=spec        only the spec plugin loads
    LIVESPEC_PLUGINS=spec,docs   both plugins load (same as 'all' today)

v0.8 only ships the framework — the plugin modules are empty register
hooks. Tools physically migrate into them in subsequent breaking phases
(P3.4 Spec mutation, P3.5 docs management). Until then, calling
``register_active`` is safe: it never adds duplicate tools.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from livespec_mcp.state import AppState

KNOWN_PLUGINS = ("spec", "docs")

# Registered by plugins but hidden unless detect_active_plugins says so.
SPEC_MUTATION_TOOL_NAMES = frozenset(
    {
        "create_spec",
        "update_spec",
        "delete_spec",
        "link_spec_symbol",
        "link_spec_dependency",
        "unlink_spec_dependency",
        "get_spec_dependency_graph",
        "scan_spec_annotations",
        "scan_docstrings_for_spec_hints",
    }
)

DOCS_PLUGIN_TOOL_NAMES = frozenset(
    {
        "generate_docs",
        "list_docs",
        "export_documentation",
    }
)

# Core surface tools registered by plugins but always visible (not gated).
CORE_PLUGIN_TOOL_NAMES = frozenset(
    {
        "export_explorer",
        "import_specs_from_markdown",
    }
)

PLUGIN_TOOL_NAMES = SPEC_MUTATION_TOOL_NAMES | DOCS_PLUGIN_TOOL_NAMES | CORE_PLUGIN_TOOL_NAMES


def plugin_name_for_tool(tool_name: str) -> str | None:
    if tool_name in CORE_PLUGIN_TOOL_NAMES:
        return None
    if tool_name in SPEC_MUTATION_TOOL_NAMES:
        return "spec"
    if tool_name in DOCS_PLUGIN_TOOL_NAMES:
        return "docs"
    return None


def _project_table_has_rows(state: AppState, table: str) -> bool:
    try:
        row = state.conn.execute(
            f"SELECT 1 FROM {table} WHERE project_id=? LIMIT 1",
            (state.project_id,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _project_has_explorer_bundle(state: AppState) -> bool:
    """Explorer bundle on disk → docs plugin tools (export_explorer) are relevant."""
    return (
        state.settings.workspace / ".mcp-docs" / "explorer" / "index.html"
    ).is_file()


def _parse_override(raw: str) -> set[str] | None:
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    if not parts:
        return None
    if "none" in parts:
        return set()
    if "all" in parts:
        return set(KNOWN_PLUGINS)
    return parts & set(KNOWN_PLUGINS)


def detect_active_plugins(state: AppState) -> set[str]:
    """Return the set of plugin names that should load for ``state``.

    Honors ``LIVESPEC_PLUGINS`` first, then falls back to DB-state probing.
    Unknown plugin names in the env var are ignored.
    """
    raw = os.environ.get("LIVESPEC_PLUGINS")
    if raw is not None:
        override = _parse_override(raw)
        if override is not None:
            return override

    active: set[str] = set()
    if _project_table_has_rows(state, "spec"):
        active.add("spec")
    if _project_table_has_rows(state, "doc") or _project_has_explorer_bundle(state):
        active.add("docs")
    return active


def register_active(mcp: FastMCP, state: AppState) -> set[str]:
    """Register every plugin selected for ``state`` on ``mcp``.

    Returns the set of plugin names that ran their ``register`` hook.
    Plugins are imported lazily so an inactive one never loads its module.
    """
    active = detect_active_plugins(state)
    if "spec" in active:
        from livespec_mcp.tools.plugins import spec as spec_plugin

        spec_plugin.register(mcp)
    if "docs" in active:
        from livespec_mcp.tools.plugins import docs as docs_plugin

        docs_plugin.register(mcp)
    return active


def register_all_plugins(mcp: FastMCP) -> set[str]:
    """Register spec + docs plugins at server boot (multi-tenant MCP servers).

    Tools are always registered so they can run when a workspace opts in.
    ``PluginVisibilityMiddleware`` hides them from ``tools/list`` and blocks
    ``tools/call`` until the workspace has spec/doc rows or
    ``LIVESPEC_PLUGINS`` includes the plugin.
    """
    from livespec_mcp.tools.plugins import docs as docs_plugin
    from livespec_mcp.tools.plugins import spec as spec_plugin

    spec_plugin.register(mcp)
    docs_plugin.register(mcp)
    return set(KNOWN_PLUGINS)


__all__ = [
    "CORE_PLUGIN_TOOL_NAMES",
    "DOCS_PLUGIN_TOOL_NAMES",
    "KNOWN_PLUGINS",
    "PLUGIN_TOOL_NAMES",
    "SPEC_MUTATION_TOOL_NAMES",
    "detect_active_plugins",
    "plugin_name_for_tool",
    "register_active",
    "register_all_plugins",
]
