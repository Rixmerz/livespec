"""v0.7 B6: scan_docstrings_for_rf_hints — brownfield helper that surfaces
RF candidates from existing docstrings without requiring `@rf:` annotations.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_scan_docstrings_surfaces_action_verbs(workspace):
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "auth.py").write_text(
        "def login(user, pw):\n"
        '    """Validates user credentials and returns a session token."""\n'
        "    return True\n"
        "\n"
        "def logout(token):\n"
        '    """Invalidates the session token."""\n'
        "    return True\n"
        "\n"
        "def refresh(token):\n"
        '    """Returns a fresh token. The old one is dropped."""\n'
        "    return True\n"
        "\n"
        "def util():\n"
        "    return 1\n"  # no docstring -> not in hints
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_docstrings_for_rf_hints", {})).data

    qnames = {h["qualified_name"] for h in out["hints"]}
    assert "pkg.auth.login" in qnames
    assert "pkg.auth.logout" in qnames
    # `util` has no docstring -> not in hints
    assert "pkg.auth.util" not in qnames

    # `refresh` starts with "Returns" which is a stop word -> filtered
    refresh_hints = [h for h in out["hints"] if h["qualified_name"] == "pkg.auth.refresh"]
    assert refresh_hints == [], "Returns... is a stop-first-word, must be filtered"

    # verb_histogram_top includes the dominant verbs
    top_words = {item["word"] for item in out["verb_histogram_top"]}
    assert "validates" in top_words
    assert "invalidates" in top_words


@pytest.mark.asyncio
async def test_scan_docstrings_skips_already_linked(workspace):
    """Symbols with an existing rf_symbol link don't appear in hints."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text(
        "def linked_fn():\n"
        '    """Manages the catalog."""\n'
        "    return None\n"
        "\n"
        "def unlinked_fn():\n"
        '    """Manages a different concern."""\n'
        "    return None\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_requirement", {"rf_id": "RF-001", "title": "A"})
        await c.call_tool(
            "link_rf_symbol",
            {"rf_id": "RF-001", "symbol_qname": "pkg.m.linked_fn"},
        )
        out = (await c.call_tool("scan_docstrings_for_rf_hints", {})).data

    qnames = {h["qualified_name"] for h in out["hints"]}
    assert "pkg.m.linked_fn" not in qnames, "already-linked symbols must be excluded"
    assert "pkg.m.unlinked_fn" in qnames


@pytest.mark.asyncio
async def test_scan_docstrings_summary_only(workspace):
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text(
        "def f():\n"
        '    """Validates input."""\n'
        "    return 1\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "scan_docstrings_for_rf_hints", {"summary_only": True}
            )
        ).data
    assert "count" in out
    assert "verb_histogram_top" in out
    assert "hints" not in out
