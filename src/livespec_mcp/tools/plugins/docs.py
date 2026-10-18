"""livespec-docs plugin (v0.8 P3.5: doc-generation surface).

Loads when the active project has doc rows, or when ``LIVESPEC_PLUGINS``
includes ``docs``. Registers:

    generate_docs, list_docs, export_documentation

Human-facing ceremony (Markdown docs). Agents prefer graph tools mid-task.
Bootstrap on a fresh repo: ``LIVESPEC_PLUGINS=docs`` (or ``=all``).

The Spec/Flow Explorer exports that used to live here were removed: they wrote
HTML/JS bundles into the repository. The browsable view of every indexed
project is now ``livespec ui``, which writes nothing into any project.
"""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp.tools import docs as _docs


def register(mcp: FastMCP) -> None:
    """Register doc-management tools on ``mcp``."""
    _docs.register(mcp)
