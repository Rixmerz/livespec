"""livespec: code intelligence + Spec traceability, shipped as an MCP server.

The distribution/console name stays ``livespec-mcp`` (see pyproject); the
product is ``livespec`` — a Claude Code plugin bundling this MCP server, a
subagent, and a Skill.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("livespec-mcp")
except PackageNotFoundError:  # imported from source without an installed distribution
    __version__ = "0.0.0+source"
