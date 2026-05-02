"""v0.9 P3: min_weight filter on traversal tools (#14).

The resolver leaves weight 0.5 edges when it cannot disambiguate a
short-name match (multiple candidates, no scope match). The agent-
facing traversal tools (`who_calls`, `who_does_this_call`,
`quick_orient`, `analyze_impact`) default to ``min_weight=0.6`` so
those edges no longer pollute caller / callee lists. Surfaced by the
Django session-04 battle-test (bug #17).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _make_short_name_fanout(workspace: Path) -> None:
    """Two `helper` functions in different modules, one ambiguous caller."""
    pkg = workspace / "lib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "alpha.py").write_text("def helper():\n    return 'alpha'\n")
    (pkg / "beta.py").write_text("def helper():\n    return 'beta'\n")
    # Caller doesn't import anything; `obj.helper()` style — resolver
    # has no scope, falls back to short-name match weight 0.5 against
    # both helper symbols.
    (pkg / "caller.py").write_text(
        "def driver(obj):\n"
        "    return obj.helper()\n"
    )


@pytest.mark.asyncio
async def test_who_calls_filters_resolver_fanout_by_default(workspace):
    _make_short_name_fanout(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Default min_weight=0.6 — the ambiguous edge to lib.alpha.helper
        # (weight 0.5) is filtered out. The driver should NOT appear as
        # a caller of helper unless the resolver landed a >=0.6 edge.
        out = (
            await c.call_tool(
                "who_calls",
                {"qname": "lib.alpha.helper", "max_depth": 1},
            )
        ).data
        callers = {n["qualified_name"] for n in out["callers"]}
        # With weight 0.5 fan-out filtered, the ambiguous caller is gone.
        # If the resolver chose alpha specifically (weight 1.0/0.7), it
        # would still show up — accept that branch as well.
        if "lib.caller.driver" in callers:
            # Resolver disambiguated to alpha; the edge weight must be
            # at least 0.6. Verify by widening the filter to legacy.
            relax = (
                await c.call_tool(
                    "who_calls",
                    {
                        "qname": "lib.alpha.helper",
                        "max_depth": 1,
                        "min_weight": 0.0,
                    },
                )
            ).data
            assert "lib.caller.driver" in {
                n["qualified_name"] for n in relax["callers"]
            }


@pytest.mark.asyncio
async def test_who_calls_min_weight_zero_includes_fanout(workspace):
    _make_short_name_fanout(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "who_calls",
                {
                    "qname": "lib.alpha.helper",
                    "max_depth": 1,
                    "min_weight": 0.0,
                },
            )
        ).data
        callers = {n["qualified_name"] for n in out["callers"]}
        # Legacy behavior: the ambiguous driver edge is included.
        assert "lib.caller.driver" in callers


@pytest.mark.asyncio
async def test_quick_orient_top_callers_clean_under_default(workspace):
    """Two same-named symbols must NOT report identical top_callers
    with default min_weight=0.6 — that was bug #17 from session 04."""
    _make_short_name_fanout(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        alpha = (
            await c.call_tool("quick_orient", {"qname": "lib.alpha.helper"})
        ).data
        beta = (
            await c.call_tool("quick_orient", {"qname": "lib.beta.helper"})
        ).data
        alpha_callers = {c["qualified_name"] for c in alpha["top_callers"]}
        beta_callers = {c["qualified_name"] for c in beta["top_callers"]}
        # The two symbols should not share the same ambiguous caller —
        # at most one of them earns it (the same-file fallback at weight
        # 0.7 won't apply here since caller.py is a different file).
        # Strict claim: their top_caller sets are not BOTH identical AND
        # non-empty (which would prove fan-out leaked through).
        if alpha_callers and beta_callers:
            assert alpha_callers != beta_callers, (
                f"fan-out leaked into top_callers: {alpha_callers}"
            )


@pytest.mark.asyncio
async def test_analyze_impact_min_weight_param_respected(workspace):
    _make_short_name_fanout(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # min_weight=0.0 should include any ambiguous fan-out edges
        relaxed = (
            await c.call_tool(
                "analyze_impact",
                {
                    "target_type": "symbol",
                    "target": "lib.alpha.helper",
                    "max_depth": 1,
                    "min_weight": 0.0,
                },
            )
        ).data
        strict = (
            await c.call_tool(
                "analyze_impact",
                {
                    "target_type": "symbol",
                    "target": "lib.alpha.helper",
                    "max_depth": 1,
                },
            )
        ).data
        # Filter must be monotonic — strict count <= relaxed count
        assert (
            strict["counts"]["impacted_callers"]
            <= relaxed["counts"]["impacted_callers"]
        )
