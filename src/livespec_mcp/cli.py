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
from pathlib import Path
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
            "`languages_failed` / `parser_errors` fields of index_project.",
            file=sys.stderr,
        )
        if result.get("hint"):
            print(f"hint: {result['hint']}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(path: str) -> dict[str, Any]:
    from livespec_mcp.state import get_state
    from livespec_mcp.tools.indexing import compute_index_status

    return compute_index_status(get_state(path))


def _current_links(path: str) -> list[dict[str, Any]]:
    from livespec_mcp import verification
    from livespec_mcp.state import get_state

    st = get_state(path)
    return verification.links(st.conn, st.project_id)


def _cmd_verify(args: argparse.Namespace) -> int:
    """Mark specs verified on the code that implements them now (`livespec_mcp.verification`)."""
    import subprocess

    from livespec_mcp import verification

    commit = (
        args.commit
        or subprocess.run(
            ["git", "-C", args.path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )
    written = verification.record(
        Path(args.path),
        _current_links(args.path),
        args.spec,
        by=args.by,
        commit=commit,
        evidence=args.evidence or "",
    )
    print(
        json.dumps(
            {"record": str(verification.ledger_path(Path(args.path))), "verified": written},
            indent=2,
        )
    )
    missing = sorted(set(args.spec) - {w["spec"] for w in written})
    if missing:
        print(f"error: no code implements {', '.join(missing)}: nothing to verify", file=sys.stderr)
        return 1
    return 0


def _cmd_stale(args: argparse.Namespace) -> int:
    """The requirements an edit made stale since they were checked; exit 1 while any is."""
    from livespec_mcp import verification

    found = verification.stale(
        Path(args.path),
        _current_links(args.path),
        strict=args.strict,
        require_test=args.require_test,
    )
    print(json.dumps({"stale": found, "count": len(found)}, indent=2))
    return 1 if found else 0


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

    p_verify = sub.add_parser(
        "verify", help="mark specs verified on the code that implements them now"
    )
    p_verify.add_argument("path", help="the repo root (indexed)")
    p_verify.add_argument("--spec", action="append", required=True, help="a spec id (repeatable)")
    p_verify.add_argument("--by", required=True, choices=("test", "reviewer", "verifier"))
    p_verify.add_argument("--commit", help="the commit it was checked on (default: HEAD)")
    p_verify.add_argument("--evidence", help="what showed it: the test, the command, the review")

    p_stale = sub.add_parser(
        "stale", help="the spec requirements an edit made stale; exit 1 while any is"
    )
    p_stale.add_argument("path", help="the repo root (indexed)")
    p_stale.add_argument(
        "--strict", action="store_true", help="a requirement never verified is stale too"
    )
    p_stale.add_argument(
        "--require-test",
        action="append",
        default=[],
        metavar="PRIORITY",
        help="a requirement of this priority counts only when a test verified it (repeatable)",
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
    if args.cmd in ("verify", "stale"):
        try:
            return _cmd_verify(args) if args.cmd == "verify" else _cmd_stale(args)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
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
