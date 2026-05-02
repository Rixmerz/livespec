"""livespec-mcp FastMCP server entry point."""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp import prompts, resources
from livespec_mcp.instrumentation import AgentLogMiddleware
from livespec_mcp.state import get_state
from livespec_mcp.tools import analysis, docs, indexing, requirements, search, watcher
from livespec_mcp.tools.plugins import register_active as register_active_plugins

mcp = FastMCP(
    name="livespec-mcp",
    instructions=(
        "Local-first MCP that maintains living documentation with bidirectional "
        "Functional Requirement <-> code traceability. Index a workspace once, then "
        "query symbols, call graphs, impact analysis, and RF coverage. "
        "Start with `index_project()` then `get_project_overview()`."
    ),
)

mcp.add_middleware(AgentLogMiddleware())

indexing.register(mcp)
analysis.register(mcp)
requirements.register(mcp)
search.register(mcp)
docs.register(mcp)
watcher.register(mcp)
resources.register(mcp)
prompts.register(mcp)

# v0.8 P3.1: plugin auto-detect. Probes the resolved workspace's DB and
# loads optional plugin tool sets when their tables show signal. Safe in
# v0.8 because plugin register hooks are no-ops; future phases migrate
# tools into them.
try:
    register_active_plugins(mcp, get_state())
except Exception:
    # Workspace may be unresolvable at import time (env var pointing at a
    # path the user hasn't created yet, etc.) — never fail server boot.
    pass


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
