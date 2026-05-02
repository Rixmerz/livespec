"""livespec-rf plugin (v0.8 P3.4: RF mutation surface).

Loads when the active project has rf rows, or when ``LIVESPEC_PLUGINS``
includes ``rf``. Registers the 11 RF mutation/linking tools that humans
run to mutate RF state — the corresponding agentic-read tools
(``list_requirements``, ``get_requirement_implementation``,
``propose_requirements_from_codebase``) stay in the default surface
because they answer questions an agent asks during work.

Tools registered here:

    create_requirement, update_requirement, delete_requirement,
    link_rf_symbol, bulk_link_rf_symbols,
    link_rf_dependency, unlink_rf_dependency, get_rf_dependency_graph,
    scan_rf_annotations, scan_docstrings_for_rf_hints,
    import_requirements_from_markdown.

Bootstrap on a fresh repo: set ``LIVESPEC_PLUGINS=rf`` (or ``=all``) so
the plugin loads before the rf table has rows. Once an RF exists the
DB-state probe takes over.
"""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp.tools import requirements as _requirements


def register(mcp: FastMCP) -> None:
    """Register RF mutation tools on ``mcp``."""
    _requirements.register(mcp, agentic=False, mutation=True)
