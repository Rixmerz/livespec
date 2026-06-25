"""livespec-mcp: living documentation MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("livespec-mcp")
except PackageNotFoundError:  # imported from source without an installed distribution
    __version__ = "0.0.0+source"
