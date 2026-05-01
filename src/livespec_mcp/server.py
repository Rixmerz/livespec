"""livespec-mcp FastMCP server entry point."""

from __future__ import annotations

from fastmcp import FastMCP

from livespec_mcp import prompts, resources
from livespec_mcp.instrumentation import AgentLogMiddleware
from livespec_mcp.tools import analysis, docs, indexing, requirements, search, watcher

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


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
