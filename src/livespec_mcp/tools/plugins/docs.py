"""livespec-docs plugin (v0.8 P3.1: framework only, no tools yet).

Loads when the active project has doc rows, or when ``LIVESPEC_PLUGINS``
includes ``docs``. Future phases (P3.5) migrate the doc-management tools
out of ``tools/docs.py`` into this module:

    generate_docs, list_docs, export_documentation.

Bulk doc generation is a human-tier feature. Agents write docs as part
of their LLM output and rarely call these tools during normal work.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register docs plugin tools. v0.8: no-op (framework only)."""
    return
