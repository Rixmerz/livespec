"""livespec-rf plugin (v0.8 P3.1: framework only, no tools yet).

Loads when the active project has rf rows, or when ``LIVESPEC_PLUGINS``
includes ``rf``. Future phases (P3.4) migrate the 11 RF mutation/linking
tools out of ``tools/requirements.py`` into this module:

    create_requirement, update_requirement, delete_requirement,
    link_rf_symbol, bulk_link_rf_symbols, link_rf_dependency,
    unlink_rf_dependency, get_rf_dependency_graph, scan_rf_annotations,
    scan_docstrings_for_rf_hints, import_requirements_from_markdown.

The agentic-read tools (``audit_coverage``, ``get_requirement_implementation``,
``list_requirements``, ``propose_requirements_from_codebase``) stay in the
default surface — they answer questions an agent asks, not edits a human
makes.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register RF plugin tools. v0.8: no-op (framework only)."""
    return
