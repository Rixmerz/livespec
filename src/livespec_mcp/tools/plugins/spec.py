"""livespec-spec plugin (v0.8 P3.4: Spec mutation surface; v0.20 RF->Spec rename).

Loads when the active project has spec rows, or when ``LIVESPEC_PLUGINS``
includes ``spec``. Registers the Spec mutation/linking tools that humans
run to mutate Spec state — the corresponding agentic-read tools
(``list_specs``, ``get_spec_implementation``,
``propose_specs_from_codebase``) stay in the default surface
because they answer questions an agent asks during work.

Tools registered here:

    create_spec, update_spec, delete_spec,
    link_spec_symbol, link_scenario_symbol,
    link_spec_dependency, unlink_spec_dependency, get_spec_dependency_graph,
    scan_spec_annotations, scan_docstrings_for_spec_hints,
    apply_spec_change, archive_spec_change (OpenSpec change lifecycle, v0.22).

Also registered here but NOT plugin-gated (they are in
``CORE_PLUGIN_TOOL_NAMES``, so they stay visible on every workspace):
``import_specs_from_markdown``, ``sync_openspec``.

``bulk_link_spec_symbols`` is NOT registered here — it is an
``@agentic_tool`` (``specs.py``) on the default surface.

Bootstrap on a fresh repo: set ``LIVESPEC_PLUGINS=spec`` (or ``=all``) so
the plugin loads before the spec table has rows. Once a Spec exists the
DB-state probe takes over.
"""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp.tools import specs as _specs


def register(mcp: FastMCP) -> None:
    """Register Spec mutation tools on ``mcp``."""
    _specs.register(mcp, agentic=False, mutation=True)
