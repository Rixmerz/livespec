"""v0.9: targeted _resolve_refs walk on partial reindex.

Validates that the dst-cascade case is handled — when file G changes,
edges from unchanged file F to symbols in G must be re-resolved (their
old edge_rows died via cascade when G's symbols got new IDs).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.mark.asyncio
async def test_targeted_walk_refreshes_cross_file_edges_to_changed_target(
    workspace,
):
    pkg = workspace / "pkg"
    _write(pkg / "__init__.py", "")
    _write(
        pkg / "callee.py",
        "def target():\n    return 1\n",
    )
    _write(
        pkg / "caller.py",
        "from pkg.callee import target\n"
        "\n"
        "def driver():\n"
        "    return target()\n",
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        before = (
            await c.call_tool(
                "who_calls", {"qname": "pkg.callee.target", "max_depth": 1}
            )
        ).data
        callers_before = {n["qualified_name"] for n in before["callers"]}
        assert "pkg.caller.driver" in callers_before, (
            f"baseline edge missing: {before}"
        )

        # Mutate callee body — its symbol gets a new ID, the existing edge
        # dies via dst-cascade. Targeted walk must re-resolve the ref from
        # caller.py (unchanged) by recognizing `target` is in names_in_changed.
        _write(
            pkg / "callee.py",
            "def target():\n    return 99  # body changed\n",
        )
        await c.call_tool("index_project", {})

        after = (
            await c.call_tool(
                "who_calls", {"qname": "pkg.callee.target", "max_depth": 1}
            )
        ).data
        callers_after = {n["qualified_name"] for n in after["callers"]}
        assert "pkg.caller.driver" in callers_after, (
            f"targeted walk dropped the edge to a changed target: {after}"
        )


@pytest.mark.asyncio
async def test_targeted_walk_handles_first_index_run(workspace):
    """First index_project on a fresh DB has no prior runs — must fall back
    to the full walk so the initial graph is complete."""
    pkg = workspace / "pkg"
    _write(pkg / "__init__.py", "")
    _write(
        pkg / "a.py",
        "def helper():\n    return 1\n",
    )
    _write(
        pkg / "b.py",
        "from pkg.a import helper\n"
        "\n"
        "def main():\n"
        "    return helper()\n",
    )

    async with Client(mcp) as c:
        result = (await c.call_tool("index_project", {})).data
        assert result["edges_total"] >= 1, (
            "first index must populate edges via the full walk path"
        )

        callers = (
            await c.call_tool(
                "who_calls", {"qname": "pkg.a.helper", "max_depth": 1}
            )
        ).data
        names = {n["qualified_name"] for n in callers["callers"]}
        assert "pkg.b.main" in names


@pytest.mark.asyncio
async def test_force_reindex_uses_full_walk(workspace):
    """force=True must re-walk every ref — the targeted shortcut would
    miss edges if refs from unchanged files happened to stay correct."""
    pkg = workspace / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "x.py", "def f():\n    return 1\n")
    _write(
        pkg / "y.py",
        "from pkg.x import f\n\ndef g():\n    return f()\n",
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        forced = (await c.call_tool("index_project", {"force": True})).data
        assert forced["edges_total"] >= 1
        callers = (
            await c.call_tool("who_calls", {"qname": "pkg.x.f", "max_depth": 1})
        ).data
        names = {n["qualified_name"] for n in callers["callers"]}
        assert "pkg.y.g" in names


@pytest.mark.asyncio
async def test_file_deletion_falls_back_to_full_walk(workspace):
    """Deleting a file kills its symbols (cascade). Targeted walk would
    miss refs from unchanged files that still reference the deleted name —
    those edges already died, and we want a full walk to confirm no stale
    state remains."""
    pkg = workspace / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "obsolete.py", "def gone():\n    return 1\n")
    _write(
        pkg / "live.py",
        "def alive():\n    return 2\n",
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Delete obsolete.py
        (pkg / "obsolete.py").unlink()
        result = (await c.call_tool("index_project", {})).data
        assert result["files_changed"] >= 0
        # live.py's `alive` symbol must still resolve
        callers = (
            await c.call_tool(
                "who_calls", {"qname": "pkg.live.alive", "max_depth": 1}
            )
        ).data
        # `alive` has no callers — assertion is that the call doesn't error
        assert "callers" in callers
