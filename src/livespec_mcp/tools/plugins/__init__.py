"""Plugin auto-detect framework (v0.8 P3.1).

The default surface is code-intel + RF-agentic tools that any agent on
any codebase will reach for. Mutation/management tools live in plugins
that load only when the workspace's DB shows they're relevant:

    livespec-rf    -> rf table has rows for the active project
    livespec-docs  -> doc table has rows for the active project

The DB-state detection is a soft default. Power users override it with the
``LIVESPEC_PLUGINS`` env var:

    LIVESPEC_PLUGINS=none      no plugins load
    LIVESPEC_PLUGINS=all       every plugin loads
    LIVESPEC_PLUGINS=rf        only the rf plugin loads
    LIVESPEC_PLUGINS=rf,docs   both plugins load (same as 'all' today)

v0.8 only ships the framework — the plugin modules are empty register
hooks. Tools physically migrate into them in subsequent breaking phases
(P3.4 RF mutation, P3.5 docs management). Until then, calling
``register_active`` is safe: it never adds duplicate tools.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from livespec_mcp.state import AppState

KNOWN_PLUGINS = ("rf", "docs")


def _project_table_has_rows(state: AppState, table: str) -> bool:
    try:
        row = state.conn.execute(
            f"SELECT 1 FROM {table} WHERE project_id=? LIMIT 1",
            (state.project_id,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


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
    if _project_table_has_rows(state, "rf"):
        active.add("rf")
    if _project_table_has_rows(state, "doc"):
        active.add("docs")
    return active


def register_active(mcp: FastMCP, state: AppState) -> set[str]:
    """Register every plugin selected for ``state`` on ``mcp``.

    Returns the set of plugin names that ran their ``register`` hook.
    Plugins are imported lazily so an inactive one never loads its module.
    """
    active = detect_active_plugins(state)
    if "rf" in active:
        from livespec_mcp.tools.plugins import rf as rf_plugin

        rf_plugin.register(mcp)
    if "docs" in active:
        from livespec_mcp.tools.plugins import docs as docs_plugin

        docs_plugin.register(mcp)
    return active


__all__ = ["KNOWN_PLUGINS", "detect_active_plugins", "register_active"]
