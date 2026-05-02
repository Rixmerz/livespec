"""livespec-docs plugin (v0.8 P3.5: doc-generation surface).

Loads when the active project has doc rows, or when ``LIVESPEC_PLUGINS``
includes ``docs``. Registers the 3 doc-management tools:

    generate_docs, list_docs, export_documentation.

These are human-tier ceremony — agents typically write docs as part of
their LLM output rather than calling these tools mid-task. Bootstrap on
a fresh repo: set ``LIVESPEC_PLUGINS=docs`` (or ``=all``) so the plugin
loads before any doc rows exist; the DB-state probe takes over once
the first doc lands.
"""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp.tools import docs as _docs


def register(mcp: FastMCP) -> None:
    """Register doc-management tools on ``mcp``."""
    _docs.register(mcp)
