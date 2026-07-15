"""CLI entry point: MCP server by default, plus headless subcommands.

`livespec-mcp` with no arguments runs the stdio MCP server — the form every
mcp.json out there already uses, so it must never change. Subcommands give
the same indexing pipeline a scriptable surface (cron, systemd timers,
pre-commit hooks, CI) without an MCP host in the middle:

    livespec-mcp index <path> [--force] [--embed]   # index + chunks, JSON out
    livespec-mcp status <path>                      # index status, JSON out
    livespec-mcp explorer serve [path] [--port 8765]  # Spec Explorer at /explorer/
    livespec-mcp fastapi init [path]                  # index + Explorer + Cursor assets
    livespec-mcp serve                              # explicit server form

JSON goes to stdout, errors to stderr with exit code 1.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any


def _serve() -> None:
    from livespec_mcp.server import mcp

    mcp.run()  # stdio transport by default


def _cmd_index(path: str, *, force: bool, embed: bool) -> dict[str, Any]:
    from livespec_mcp.state import get_state
    from livespec_mcp.tools.indexing import run_index_pipeline

    return run_index_pipeline(get_state(path), force=force, embed=embed)


def _cmd_status(path: str) -> dict[str, Any]:
    from livespec_mcp.state import get_state
    from livespec_mcp.tools.indexing import compute_index_status

    return compute_index_status(get_state(path))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        _serve()
        return 0

    parser = argparse.ArgumentParser(
        prog="livespec-mcp",
        description="Code intelligence MCP server + headless indexing CLI.",
    )
    from livespec_mcp import __version__

    parser.add_argument(
        "--version", action="version", version=f"livespec-mcp {__version__}"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the MCP server on stdio (same as no arguments)")

    p_index = sub.add_parser("index", help="index a repo and print JSON stats")
    p_index.add_argument("path", help="absolute or relative path to the repo root")
    p_index.add_argument("--force", action="store_true", help="re-extract every file")
    p_index.add_argument(
        "--embed", action="store_true", help="populate vector embeddings ([embeddings] extra)"
    )

    p_status = sub.add_parser("status", help="print index status JSON for a repo")
    p_status.add_argument("path", help="absolute or relative path to the repo root")

    p_explorer = sub.add_parser("explorer", help="Spec Explorer local preview")
    p_explorer_sub = p_explorer.add_subparsers(dest="explorer_cmd", required=True)
    p_explorer_serve = p_explorer_sub.add_parser(
        "serve", help="HTTP server at /explorer (default port 8765)"
    )
    p_explorer_serve.add_argument(
        "path",
        nargs="?",
        default=".",
        help="repo root (default: current directory)",
    )
    p_explorer_serve.add_argument("--host", default="127.0.0.1")
    p_explorer_serve.add_argument("--port", type=int, default=8765)
    p_explorer_serve.add_argument(
        "--mount-path",
        default="/explorer",
        help="URL prefix (default: /explorer)",
    )

    p_fastapi = sub.add_parser(
        "fastapi",
        help="FastAPI + Spec Explorer onboarding",
    )
    p_fastapi_sub = p_fastapi.add_subparsers(dest="fastapi_cmd", required=True)
    p_fastapi_init = p_fastapi_sub.add_parser(
        "init",
        help="index, build Explorer, autowire, install Cursor rule/skill",
    )
    p_fastapi_init.add_argument(
        "path",
        nargs="?",
        default=".",
        help="repo root (default: current directory)",
    )
    p_fastapi_init.add_argument(
        "--no-index",
        action="store_true",
        help="skip index_project + explorer bundle",
    )
    p_fastapi_init.add_argument(
        "--no-wire",
        action="store_true",
        help="skip autowire mount_explorer into main.py/app.py",
    )
    p_fastapi_init.add_argument(
        "--no-cursor",
        action="store_true",
        help="skip .cursor/rules + .cursor/skills install",
    )

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        _serve()
        return 0
    if args.cmd == "explorer":
        if args.explorer_cmd == "serve":
            from livespec_mcp.explorer.asgi import serve_explorer

            try:
                serve_explorer(
                    args.path,
                    host=args.host,
                    port=args.port,
                    prefix=args.mount_path,
                )
            except FileNotFoundError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        return 0
    if args.cmd == "fastapi":
        if args.fastapi_cmd == "init":
            from livespec_mcp.explorer.install import init_fastapi_project

            try:
                result = init_fastapi_project(
                    args.path,
                    index=not args.no_index,
                    wire_app=not args.no_wire,
                    install_cursor=not args.no_cursor,
                )
            except FileNotFoundError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            print(json.dumps(result.__dict__, indent=2))
            return 1 if result.errors else 0
        return 0
    try:
        if args.cmd == "index":
            payload = _cmd_index(args.path, force=args.force, embed=args.embed)
        else:
            payload = _cmd_status(args.path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as e:
        print(
            f"error: index database unreadable ({e}). "
            f"Delete <repo>/.mcp-docs/docs.db and re-run `livespec-mcp index`.",
            file=sys.stderr,
        )
        return 1
    except PermissionError as e:
        print(
            f"error: cannot write workspace state ({e}). "
            f"Check permissions on <repo>/.mcp-docs/.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, indent=2))
    return 0
