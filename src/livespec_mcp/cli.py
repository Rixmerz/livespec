"""CLI entry point: MCP server by default, plus headless subcommands.

`livespec` with no arguments runs the stdio MCP server — the form every
mcp.json out there already uses, so it must never change. Subcommands give
the same indexing pipeline a scriptable surface (cron, systemd timers,
pre-commit hooks, CI) without an MCP host in the middle:

    livespec index <path> [--force]             # index + chunks, JSON out
    livespec status <path>                      # index status, JSON out
    livespec grammars [--all] [lang ...]        # prefetch tree-sitter grammars
    livespec ui [--port N] [--open] [--add PATH]  # browse every indexed project
    livespec serve                              # explicit server form

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


def _cmd_index(path: str, *, force: bool) -> dict[str, Any]:
    from livespec_mcp.state import get_state
    from livespec_mcp.tools.indexing import run_index_pipeline

    return run_index_pipeline(get_state(path, create=True), force=force)


def _cmd_grammars(languages: list[str] | None, *, check: bool = False) -> int:
    """Prefetch (or report) the tree-sitter grammars indexing needs.

    Exits non-zero when a requested grammar is still unavailable afterwards, so
    a provisioning script or a Docker build fails loudly instead of shipping an
    image that will quietly index Python only.
    """
    from livespec_mcp.domain.languages import (
        EXTRACTOR_SUPPORTED,
        downloaded_grammars,
        grammar_cache_dir,
        prefetch_grammars,
    )

    if check:
        wanted = sorted(languages or EXTRACTOR_SUPPORTED)
        cached = downloaded_grammars()
        missing = [lang for lang in wanted if lang not in cached]
        print(
            json.dumps(
                {
                    "requested": wanted,
                    "cached": cached,
                    "missing": missing,
                    "cache_dir": grammar_cache_dir(),
                },
                indent=2,
            )
        )
        return 1 if missing else 0

    result = prefetch_grammars(languages)
    print(json.dumps(result, indent=2))
    if result["failed"]:
        print(
            f"error: {len(result['failed'])} grammar(s) could not be downloaded. "
            "Indexing will SKIP those languages and say so in the "
            "`languages_failed` field of index_project.",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_status(path: str) -> dict[str, Any]:
    from livespec_mcp.state import get_state
    from livespec_mcp.tools.indexing import compute_index_status

    return compute_index_status(get_state(path))


def _cmd_ui(args: argparse.Namespace) -> int:
    from livespec_mcp import registry
    from livespec_mcp.ui.data import register_existing

    for path in args.add:
        try:
            entry = register_existing(path)
        except (FileNotFoundError, sqlite3.Error) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"registered {entry['path']}", file=sys.stderr)
    if args.list:
        print(
            json.dumps(
                {"registry": str(registry.registry_path()), "projects": registry.list_projects()},
                indent=2,
            )
        )
        return 0

    from livespec_mcp.ui.server import make_server, server_url

    try:
        srv = make_server(args.host, args.port)
    except OSError as e:
        print(f"error: cannot listen on {args.host}:{args.port} ({e})", file=sys.stderr)
        return 1
    url = server_url(srv)
    # The first stdout line is the contract the /livespec-ui command reads.
    print(f"livespec ui: {url}", flush=True)
    print(f"registry: {registry.registry_path()} · stop with Ctrl+C", file=sys.stderr, flush=True)
    if args.open:
        import webbrowser

        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        _serve()
        return 0

    parser = argparse.ArgumentParser(
        prog="livespec",
        description="Code intelligence MCP server + headless indexing CLI.",
    )
    from livespec_mcp import __version__

    parser.add_argument("--version", action="version", version=f"livespec {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the MCP server on stdio (same as no arguments)")

    p_index = sub.add_parser("index", help="index a repo and print JSON stats")
    p_index.add_argument("path", help="absolute or relative path to the repo root")
    p_index.add_argument("--force", action="store_true", help="re-extract every file")

    p_status = sub.add_parser("status", help="print index status JSON for a repo")
    p_status.add_argument("path", help="absolute or relative path to the repo root")

    p_grammars = sub.add_parser(
        "grammars",
        help="download the tree-sitter grammars livespec needs (run once, online)",
        description=(
            "tree-sitter-language-pack fetches each grammar from the network on "
            "first use rather than bundling them, so an offline or proxied "
            "machine indexes Python and silently nothing else. Run this once "
            "with network access to cache them; after that indexing is local. "
            "With no arguments, downloads exactly the languages livespec can "
            "extract, not the pack's several hundred."
        ),
    )
    p_grammars.add_argument(
        "languages",
        nargs="*",
        help="specific language ids (default: every language livespec extracts)",
    )
    p_grammars.add_argument(
        "--check",
        action="store_true",
        help="report what is cached without downloading anything",
    )

    p_ui = sub.add_parser(
        "ui",
        help="serve one local, read-only web UI over every indexed project",
        description=(
            "Starts a local web server over the user-level registry of indexed "
            "projects (every `livespec index` / index_project records one). "
            "Each project's index is opened read-only; nothing is written into "
            "any project. Stop it with Ctrl+C."
        ),
    )
    p_ui.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p_ui.add_argument("--port", type=int, default=0, help="port (default 0: a random free port)")
    p_ui.add_argument("--open", action="store_true", help="open the page in a browser")
    p_ui.add_argument(
        "--add",
        action="append",
        default=[],
        metavar="PATH",
        help="register an already-indexed repo without re-indexing it (repeatable)",
    )
    p_ui.add_argument("--list", action="store_true", help="print the registry as JSON and exit")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        _serve()
        return 0
    if args.cmd == "ui":
        return _cmd_ui(args)
    if args.cmd == "grammars":
        return _cmd_grammars(args.languages or None, check=args.check)
    try:
        if args.cmd == "index":
            payload = _cmd_index(args.path, force=args.force)
        else:
            payload = _cmd_status(args.path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as e:
        if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
            # Busy, not broken: another livespec process holds the write lock past
            # the 30s timeout. Deleting the file would throw away a good index.
            print(
                f"error: the index is busy ({e}): another livespec process is writing it. "
                f"Run this again when it is done; the file is fine, do not delete it.",
                file=sys.stderr,
            )
            return 1
        print(
            f"error: index database unreadable ({e}). "
            f"Delete <repo>/.mcp-docs/docs.db and re-run `livespec-mcp index`.",
            file=sys.stderr,
        )
        return 1
    except PermissionError as e:
        print(
            f"error: cannot write workspace state ({e}). Check permissions on <repo>/.mcp-docs/.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(payload, indent=2))
    return 0
