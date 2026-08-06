"""livespec FastMCP server entry point (the MCP component of the livespec plugin)."""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp import prompts, resources
from livespec_mcp.error_middleware import WorkspaceErrorMiddleware
from livespec_mcp.instrumentation import AgentLogMiddleware
from livespec_mcp.plugin_visibility import PluginVisibilityMiddleware
from livespec_mcp.schema_compat import SchemaCompatMiddleware
from livespec_mcp.tools import analysis, indexing, search, specs
from livespec_mcp.tools.plugins import register_all_plugins

mcp = FastMCP(
    name="livespec",
    instructions=(
        "Local-first MCP: call graph, impact analysis, Spec<->code traceability. "
        "Every tool has a required parameter `workspace` (see its per-parameter "
        "description and examples in the schema). Pass the absolute path of the "
        "repo the user is editing in this conversation — not a parent folder. "
        "No LIVESPEC_WORKSPACE env. Switch repos by changing `workspace` only. "
        "Fetch prompt `agent_playbook` for @spec: commenting. "
        "Cold-open per repo: index_project(workspace=<repo>) then "
        "get_project_overview(workspace=<same repo>). "
        "Speaks OpenSpec (Fission-AI): if the repo has an `openspec/` dir, call "
        "sync_openspec to ingest specs+changes, export_openspec to write them "
        "back, validate_openspec to check; fetch prompt `openspec_workflow`. "
        "Cross-repo (polyrepo): set [workspace] group_db in .livespec.toml; "
        "mirror Spec ids as xrepo-* in each repo; fetch prompt "
        "`cross_repo_workflow` or read resources guide://cross-repo and "
        "project://group."
    ),
)

# Added first → outermost. SchemaCompat rewrites tools/list JSON Schema so
# Cursor does not render every param as ``any`` (nested anyOf / null unions).
mcp.add_middleware(SchemaCompatMiddleware())
mcp.add_middleware(WorkspaceErrorMiddleware())
mcp.add_middleware(AgentLogMiddleware())
mcp.add_middleware(PluginVisibilityMiddleware())

indexing.register(mcp)
analysis.register(mcp)
specs.register(mcp)
search.register(mcp)
resources.register(mcp)
prompts.register(mcp)

# Multi-tenant: plugins registered at boot; PluginVisibilityMiddleware gates
# list/call per workspace (LRU session cache + LIVESPEC_PLUGINS override).
register_all_plugins(mcp)


def main() -> None:
    import sys

    from livespec_mcp.cli import main as cli_main

    sys.exit(cli_main())


if __name__ == "__main__":
    main()
