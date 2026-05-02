"""Regression tests for bugs caught during smoke testing.

Bugs tracked:
1. `index_project` second call (no file changes) wiped every call edge.
   Fixed in 2d3287e.
2. Partial re-index (some files changed) wiped edges from unchanged files.
   Fixed in df55874.

v0.8 P3.3 dropped the search/RAG tool wrappers, so the FTS5 scoring
regression tests are gone — the underlying RAG domain code is exercised
by future plugin tooling if/when search is reintroduced.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_idempotent_reindex_keeps_edges(sample_repo):
    """Re-running index_project on an unchanged workspace must not drop any edges."""
    async with Client(mcp) as c:
        first = (await c.call_tool("index_project", {})).data
        assert first["edges_total"] > 0, "fixture should produce some edges"
        before = first["edges_total"]
        # Re-run with no file changes
        second = (await c.call_tool("index_project", {})).data
        assert second["files_changed"] == 0
        assert second["edges_total"] == before, (
            f"idempotent re-index lost edges: before={before} after={second['edges_total']}"
        )


@pytest.mark.asyncio
async def test_partial_reindex_preserves_unchanged_edges(sample_repo):
    """Touching one file must not remove edges from files that did not change."""
    async with Client(mcp) as c:
        baseline = (await c.call_tool("index_project", {})).data
        before_edges = baseline["edges_total"]
        assert before_edges >= 2

        # Modify api.py only — leave auth.py alone. Edges from auth.py callers
        # to auth.py callees must survive.
        api_path = sample_repo / "pkg" / "api.py"
        text = api_path.read_text()
        api_path.write_text(text + "\n# trivial change\n")

        second = (await c.call_tool("index_project", {})).data
        assert second["files_changed"] == 1
        # Edges must not collapse — the partial bug saw counts drop to ~10% of baseline.
        # Allow small movement (a few edges removed or added) but reject massive loss.
        assert second["edges_total"] >= before_edges - 2, (
            f"partial re-index lost too many edges: "
            f"before={before_edges} after={second['edges_total']}"
        )


@pytest.mark.asyncio
async def test_signature_drift_marks_doc_stale(sample_repo):
    """P2.4: changing a function signature without touching body must mark stale."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Persist a doc so there's something to compare against
        await c.call_tool(
            "generate_docs",
            {
                "target_type": "symbol",
                "identifier": "pkg.auth.verify",
                "content": "doc for verify",
            },
        )
        # Mutate the SIGNATURE of verify (not its body)
        auth_path = sample_repo / "pkg" / "auth.py"
        text = auth_path.read_text()
        new = text.replace("def verify(user, password):", "def verify(user, password, mfa_token):")
        assert text != new, "fixture must contain the original verify signature"
        auth_path.write_text(new)
        await c.call_tool("index_project", {"force": True})
        stale = (
            await c.call_tool("list_docs", {"target_type": "symbol", "only_stale": True})
        ).data
        targets = {s["target"]: s["drift"] for s in stale["stale"]}
        assert "pkg.auth.verify" in targets, f"signature drift not detected: {stale}"
        assert "signature" in targets["pkg.auth.verify"]


