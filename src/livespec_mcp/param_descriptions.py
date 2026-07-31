"""Canonical MCP parameter descriptions for ``tools/list``.

Hosts (Cursor especially) show an empty Description column when JSON Schema
properties lack ``description``. Signatures may omit ``Field(description=…)``;
``SchemaCompatMiddleware`` fills gaps from this catalog.

Lookup order for each ``(tool_name, param_name)``:
1. ``TOOL_PARAM_DESCRIPTIONS[(tool, param)]``
2. ``PARAM_DESCRIPTIONS[param]``
"""

from __future__ import annotations

from livespec_mcp.workspace_param import WORKSPACE_DESCRIPTION

# Shared across tools (same meaning everywhere).
PARAM_DESCRIPTIONS: dict[str, str] = {
    "workspace": WORKSPACE_DESCRIPTION,
    "qname": (
        "Fully-qualified symbol name. Separators ``::``, ``.``, and ``#`` "
        "are accepted interchangeably."
    ),
    "symbol_qname": "Fully-qualified symbol name to link (or unlink).",
    "max_depth": (
        "How many call-graph / dependency hops to walk "
        "(1 = direct neighbors only)."
    ),
    "limit": "Max items to return in this page (pagination; counts stay exact).",
    "cursor": "Offset into the full result list; pass prior ``next_cursor`` to continue.",
    "summary_only": (
        "If true, return counts/meta only (no item arrays) — use on huge repos."
    ),
    "min_weight": (
        "Drop call edges below this resolver weight. Default 0.6 skips ambiguous "
        "fan-out (weight 0.5). Pass 0.0 for the unfiltered cone."
    ),
    "force": "If true, re-extract every file even when content hash is unchanged.",
    "watch": (
        "If true, start a filesystem watcher after indexing (debounce ~2s) so "
        "edits trigger automatic re-index."
    ),
    "explorer": (
        "If true, (re)generate the static Spec Explorer bundle under "
        "``.mcp-docs/explorer/`` after indexing."
    ),
    "include_infrastructure": (
        "Include infrastructure / generated / migration-style *symbols* that are "
        "normally filtered out. For HTTP routes see ``include_infra_routes``."
    ),
    "include_structural_patterns": (
        "Include structural/boilerplate symbol names normally filtered from overview."
    ),
    "include_public": (
        "Include symbols marked public/exported (``pub``, ``export``, …) that "
        "are normally kept out of dead-code candidates."
    ),
    "include_non_python": (
        "Also sweep non-Python symbols for dead-code candidates "
        "(auto-enabled on TS/JS-only repos)."
    ),
    "include_ts_framework_routes": (
        "Treat TS/JS framework route handlers as entry points when sweeping dead code."
    ),
    "include_infra_routes": (
        "Include infra/docs/UI *routes* (``/health``, ``/metrics``, swagger, …) "
        "that are filtered by default. For symbols see ``include_infrastructure``."
    ),
    "include_orphan_clients": (
        "Include client ``route_ref`` rows with no matching server hop "
        "(often missing SA / incomplete extract — not proof of dead code)."
    ),
    "include_harness": "Include test harness helpers normally excluded from orphan-test results.",
    "include_fixtures": "Include pytest/fixture symbols normally excluded from orphan-test results.",
    "include_changes": "Also export OpenSpec ``changes/`` (and archive) alongside canonical specs.",
    "framework": (
        "Optional framework filter (e.g. ``django``, ``fastapi``, ``express``, "
        "``hono``, ``spring``). Omit to sweep all known frameworks."
    ),
    "project": (
        "Optional project name within a ``group_db``; omit to include every "
        "project in the shared DB."
    ),
    "cursors": (
        "Per-list pagination cursors for ``audit_coverage`` (object of list-name → offset)."
    ),
    "base_ref": "Git base ref for the diff (e.g. ``main``, ``HEAD~1``, a SHA).",
    "head_ref": "Git head ref for the diff (e.g. ``HEAD``, a branch, a SHA).",
    "impacted_limit": "Max impacted symbols to return in this page.",
    "impacted_cursor": "Pagination offset for the impacted-symbols list.",
    "pattern": "Regex or literal search pattern over indexed file contents.",
    "path_glob": "Optional glob limiting which indexed paths are grepped.",
    "per_file_limit": "Max matches to keep per file before moving on.",
    "fts_prefilter": (
        "If true, use FTS5 to narrow candidate files before line grep (faster on large repos)."
    ),
    "module": "Optional module / package filter for Specs.",
    "priority": "Optional Spec priority filter (e.g. high / medium / low).",
    "has_implementation": (
        "If true, only Specs with at least one linked symbol; if false, only Specs with none."
    ),
    "capability": "Optional OpenSpec capability / module grouping filter.",
    "mappings": (
        "List of ``{spec_id, symbol_qname, relation?, confidence?}`` objects to link in bulk."
    ),
    "spec_id": "Spec identifier (e.g. ``SPEC-042`` or an OpenSpec requirement id).",
    "parent_spec_id": "Parent Spec id for a Spec→Spec dependency edge.",
    "child_spec_id": "Child Spec id for a Spec→Spec dependency edge.",
    "scenario_name": "OpenSpec scenario name (``#### Scenario: …``) within the Spec.",
    "relation": "Link relation (e.g. ``implements``, ``tests``, ``documents``).",
    "confidence": "Link confidence in ``[0, 1]`` (annotation / manual override).",
    "source": "Provenance of the link (e.g. ``manual``, ``annotation``, ``import``).",
    "unlink": "If true, remove the link instead of creating it.",
    "module_depth": "Directory depth used to group modules when proposing Specs.",
    "min_symbols_per_group": "Skip module groups smaller than this when proposing Specs.",
    "max_proposals": "Maximum Spec proposals to return.",
    "skip_already_covered": (
        "Skip module groups that already have any Spec-linked symbol."
    ),
    "sample_per_group": "Max annotation samples to return per verb/group.",
    "out_dir": "Output directory for the OpenSpec tree (default under the workspace).",
    "out_subdir": "Subdirectory under the workspace for exported documentation.",
    "strict": "If true, fail when any requirement lacks ≥1 scenario (OpenSpec ``--strict``).",
    "dry_run": "If true, validate/apply preview without writing Spec changes.",
    "openspec_dir": "Path to the ``openspec/`` directory (default: ``<workspace>/openspec``).",
    "scope": "Search scope: ``code``, ``spec``, or ``all`` (FTS5 over AST chunks / Specs).",
    "fmt": "Markdown dialect hint: ``auto``, ``openspec``, or ``livespec`` native.",
    "path": "Filesystem path to a markdown Spec file or tree to import.",
    "title": "Human-readable Spec title.",
    "description": "Longer Spec description / body text.",
    "status": "Lifecycle status filter or value (e.g. ``active``, ``deprecated``).",
    "only_stale": "If true, list only docs whose body_hash drifted from the current symbol.",
    "identifier": "Target id for doc generation (symbol qname, Spec id, or file path).",
    "content": "Optional pre-written markdown body; when set, skips LLM sampling.",
    "max_tokens": "Sampling budget hint when the host generates doc content.",
    "format": "Export format for documentation (e.g. ``markdown``).",
    "generated_at": "Optional ISO timestamp stamped into the Explorer bundle meta.",
    "base": "Optional git base ref for Explorer Changes tab.",
    "head": "Optional git head ref for Explorer Changes tab.",
    "direction": "Walk ``up`` (dependents), ``down`` (dependencies), or ``both``.",
    "target": "Impact target: symbol qname, file path, or Spec id (see ``target_type``).",
    "target_type": "Impact / doc target kind: ``symbol``, ``file``, or ``spec``.",
    "name": "OpenSpec change proposal name (directory under ``openspec/changes/``).",
    "query": "Search query string (tool-specific semantics — see tool description).",
    "kind": "Optional kind filter (tool-specific — symbol kind, Spec kind, or dependency kind).",
}

# Disambiguate params whose meaning depends on the tool.
TOOL_PARAM_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("find_symbol", "query"): (
        "Substring or qualified name to search. Separators ``::``, ``.``, and ``#`` "
        "are normalized."
    ),
    ("find_symbol", "kind"): (
        "Optional symbol kind filter (``function``, ``class``, ``method``, …)."
    ),
    ("search", "query"): "FTS5 keyword query over AST-aware chunks and Specs.",
    ("grep_in_indexed_files", "kind"): (
        "Optional symbol-kind filter when restricting grep to symbol bodies."
    ),
    ("list_specs", "kind"): (
        "Spec kind filter: ``functional_requirement``, ``adr``, ``nfr``, …"
    ),
    ("create_spec", "kind"): (
        "Spec kind: ``functional_requirement``, ``adr``, ``nfr``, or other taxonomy value."
    ),
    ("update_spec", "kind"): (
        "Spec kind: ``functional_requirement``, ``adr``, ``nfr``, or other taxonomy value."
    ),
    ("link_spec_dependency", "kind"): (
        "Dependency kind: ``requires``, ``extends``, ``conflicts``, …"
    ),
    ("unlink_spec_dependency", "kind"): (
        "Dependency kind to remove: ``requires``, ``extends``, ``conflicts``, …"
    ),
    ("list_specs", "status"): "Filter by Spec lifecycle status (e.g. ``active``).",
    ("list_spec_changes", "status"): (
        "Filter OpenSpec changes by status (e.g. ``pending``, ``applied``, ``archived``)."
    ),
    ("create_spec", "status"): "Initial Spec lifecycle status (default ``active``).",
    ("update_spec", "status"): "New Spec lifecycle status.",
    ("analyze_impact", "target_type"): "What ``target`` names: ``symbol``, ``file``, or ``spec``.",
    ("generate_docs", "target_type"): "Doc target kind: ``symbol``, ``file``, or ``spec``.",
    ("list_docs", "target_type"): "Optional filter: ``symbol``, ``file``, or ``spec``.",
    ("import_specs_from_markdown", "path"): (
        "Markdown file or directory tree to import Specs from."
    ),
    ("find_legacy_flows", "project"): (
        "Optional project name inside ``group_db``; omit for all projects."
    ),
    ("get_spec_change", "name"): "OpenSpec change folder name under ``openspec/changes/``.",
    ("apply_spec_change", "name"): "OpenSpec change folder name to apply.",
    ("archive_spec_change", "name"): "OpenSpec change folder name to archive.",
}


def description_for(tool_name: str, param_name: str) -> str | None:
    """Return catalog description for a tool parameter, or None if unknown."""
    return TOOL_PARAM_DESCRIPTIONS.get((tool_name, param_name)) or PARAM_DESCRIPTIONS.get(
        param_name
    )
