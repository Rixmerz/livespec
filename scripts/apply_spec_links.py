"""Reproduce livespec's own Spec->symbol links from a committed seed.

The Spec *definitions* in ``docs/requirements/livespec-specs.md`` regenerate
via ``import_specs_from_markdown``. The ``implements`` / ``tests`` *links*
between each Spec and the symbols that satisfy it live only in the local
(gitignored) ``.mcp-docs/docs.db``. This script makes those links
reproducible from a fresh clone by replaying the committed seed
(``docs/requirements/livespec-spec-links.json``) through the in-process
``bulk_link_spec_symbols`` MCP tool.

Deterministic regeneration flow (run against a freshly cloned workspace):

    1. index_project                        # builds the symbol index
    2. import_specs_from_markdown \\
         docs/requirements/livespec-specs.md   # recreates the 12 Spec defs
    3. python scripts/apply_spec_links.py   # recreates the implements/tests links

The seed is a sorted list of ``{"spec_id", "qname", "relation"}`` objects.
``bulk_link_spec_symbols`` uses ``INSERT OR IGNORE``, so re-running this
script is idempotent: existing links are skipped, only missing ones are
created.

Usage:
    python scripts/apply_spec_links.py [--workspace PATH] [--links PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client

from livespec_mcp.server import mcp

DEFAULT_LINKS = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "requirements"
    / "livespec-spec-links.json"
)


def _load_links(links_path: Path) -> list[dict[str, str]]:
    """Load and validate the seed file into a list of link dicts."""
    raw = json.loads(links_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{links_path}: expected a JSON list of link objects")
    links: list[dict[str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{links_path}[{i}]: expected an object")
        spec_id = entry.get("spec_id")
        qname = entry.get("qname")
        relation = entry.get("relation", "implements")
        if not spec_id or not qname:
            raise ValueError(f"{links_path}[{i}]: 'spec_id' and 'qname' are required")
        links.append({"spec_id": spec_id, "qname": qname, "relation": relation})
    return links


def _to_mappings(links: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Translate seed entries to ``bulk_link_spec_symbols`` mapping objects.

    Grouped by (spec_id, relation) ordering for a diff-friendly, deterministic
    payload. The tool itself accepts a single flat list and links them all in
    one transaction.
    """
    ordered = sorted(links, key=lambda d: (d["spec_id"], d["relation"], d["qname"]))
    return [
        {
            "spec_id": link["spec_id"],
            "symbol_qname": link["qname"],
            "relation": link["relation"],
            "source": "manual",
        }
        for link in ordered
    ]


async def _apply(workspace: Path, mappings: list[dict[str, Any]]) -> dict[str, Any]:
    """Call ``bulk_link_spec_symbols`` in-process and return its result."""
    args: dict[str, Any] = {"mappings": mappings, "workspace": str(workspace)}
    async with Client(mcp) as client:
        result = await client.call_tool("bulk_link_spec_symbols", args)
    return result.data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce livespec Spec->symbol links from the committed seed."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Target workspace to apply links to (default: cwd).",
    )
    parser.add_argument(
        "--links",
        type=Path,
        default=DEFAULT_LINKS,
        help=f"Path to the link seed JSON (default: {DEFAULT_LINKS}).",
    )
    ns = parser.parse_args(argv)

    workspace: Path = ns.workspace.resolve()
    links_path: Path = ns.links.resolve()

    if not links_path.is_file():
        print(f"error: seed file not found: {links_path}", file=sys.stderr)
        return 2

    links = _load_links(links_path)
    mappings = _to_mappings(links)
    result = asyncio.run(_apply(workspace, mappings))

    linked = result.get("linked", 0)
    skipped = result.get("skipped", 0)
    failed = result.get("failed", 0)
    total = result.get("total", len(mappings))
    print(
        f"applied {total} mappings to {workspace}: "
        f"linked={linked} skipped={skipped} failed={failed}"
    )
    if failed:
        for entry in result.get("results", []):
            if not entry.get("ok"):
                print(
                    f"  FAILED {entry.get('spec_id')} -> "
                    f"{entry.get('symbol_qname')}: {entry.get('error')}",
                    file=sys.stderr,
                )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
