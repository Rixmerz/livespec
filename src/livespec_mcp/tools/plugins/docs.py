"""livespec-docs plugin (v0.8 P3.5: doc-generation + Explorer surface).

Loads when the active project has doc rows, an Explorer bundle on disk, or
when ``LIVESPEC_PLUGINS`` includes ``docs``. Registers:

    generate_docs, list_docs, export_documentation,
    export_explorer, export_flow_explorer

Human-facing ceremony (static docs + Spec/Flow Explorer HTML). Agents
prefer grafo tools mid-task; humans use export for durable artifacts.
Bootstrap on a fresh repo: ``LIVESPEC_PLUGINS=docs`` (or ``=all``), or
``index_project(explorer=True)`` to drop a bundle and unlock the plugin.
"""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp.tools import docs as _docs


def register(mcp: FastMCP) -> None:
    """Register doc-management tools on ``mcp``."""
    _docs.register(mcp)
