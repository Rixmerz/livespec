"""livespec FastMCP server entry point (the MCP component of the livespec plugin).

The console scripts (``livespec``, ``livespec-mcp``) point at :func:`main`
here. ``mcp`` is built on first access (``from livespec_mcp.server import
mcp``, fastmcp.json's ``server.py:mcp``) by importing
:mod:`livespec_mcp.app`, so the CLI subcommands that never serve MCP do not
pay for fastmcp and the tool modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

    mcp: FastMCP


def __getattr__(name: str) -> Any:
    if name == "mcp":
        from livespec_mcp.app import mcp

        globals()["mcp"] = mcp
        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    import sys

    from livespec_mcp.cli import main as cli_main

    sys.exit(cli_main())


if __name__ == "__main__":
    main()
