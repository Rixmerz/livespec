"""livespec-mcp FastMCP server entry point."""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp import prompts, resources
from livespec_mcp.instrumentation import AgentLogMiddleware
from livespec_mcp.tools import analysis, indexing, requirements, search
from livespec_mcp.tools.plugins import register_all_plugins
from livespec_mcp.workspace_param import WORKSPACE_DESCRIPTION

mcp = FastMCP(
    name="livespec-mcp",
    instructions=(
        "Local-first MCP: call graph, impact analysis, RF<->code traceability. "
        "Every tool has a required parameter `workspace` (see its per-parameter "
        "description and examples in the schema). Pass the absolute path of the "
        "repo the user is editing in this conversation — not a parent folder. "
        "No LIVESPEC_WORKSPACE env. Switch repos by changing `workspace` only. "
        "Fetch prompt `agent_playbook` for @rf: commenting. "
        "Cold-open per repo: index_project(workspace=<repo>) then "
        "get_project_overview(workspace=<same repo>)."
    ),
)

mcp.add_middleware(AgentLogMiddleware())

indexing.register(mcp)
analysis.register(mcp)
requirements.register(mcp)
search.register(mcp)
resources.register(mcp)
prompts.register(mcp)

# Multi-tenant: RF + docs plugins always registered; each tool selects DB via
# workspace= on the call (LRU cache in get_state).
register_all_plugins(mcp)


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
