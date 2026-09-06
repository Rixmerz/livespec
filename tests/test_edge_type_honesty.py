"""`who_calls` must only call something a caller when it calls it.

Ingestion (v0.33) put `references` and `inherits` rows into `symbol_edge`
alongside livespec's own `calls`. Those are real dependencies — a type
annotation does break when you change the type — and they belong in
`analyze_impact`. But `who_calls` reads the same table and does not
distinguish edge types, so every one of them arrived as a "caller".

Measured on livespec itself against a Graphify graph of its own tree:
`who_calls` for one class went from 2 to 40 after an ingest, and 38 of the 40
were methods taking it as a parameter type. Nothing in the payload said which
two actually call it. That is the same class of mistake the import relations
were kept out of the default ingest to avoid — it just arrived through a
different door.

The fix has three halves and all three are load-bearing:
  1. the default answer counts only invocation edges,
  2. what the filter excluded is reported rather than silently dropped,
  3. depth-1 rows say which edge, and whose claim it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.graph import (
    INVOCATION_EDGE_TYPES,
    ancestors_within,
    descendants_within,
)
from livespec_mcp.server import mcp

try:  # networkx is a hard dependency; import here to keep the graph tests local
    import networkx as nx
except ImportError:  # pragma: no cover
    nx = None


# --------------------------------------------------------------------------
# The traversal primitive
# --------------------------------------------------------------------------


def _graph_with_mixed_edges():
    g = nx.DiGraph()
    g.add_edge(2, 1, edge_type="calls", weight=1.0, origin="livespec")
    g.add_edge(3, 1, edge_type="references", weight=0.9, origin="external:graphify")
    g.add_edge(4, 3, edge_type="calls", weight=1.0, origin="livespec")
    return g


def test_no_edge_types_argument_keeps_the_legacy_walk():
    """Every existing caller of these functions must be unaffected."""
    g = _graph_with_mixed_edges()

    assert ancestors_within(g, 1, 2) == {2, 3, 4}


def test_filtering_happens_during_the_walk_not_after_it():
    """Node 4 reaches 1 only THROUGH the `references` edge. If the filter ran
    on the result set instead of the traversal, 4 would survive as a caller —
    the same lie, one hop further out."""
    g = _graph_with_mixed_edges()

    assert ancestors_within(g, 1, 2, edge_types=frozenset({"calls"})) == {2}


def test_the_filter_composes_with_min_weight():
    g = nx.DiGraph()
    g.add_edge(2, 1, edge_type="calls", weight=0.5, origin="livespec")
    g.add_edge(3, 1, edge_type="calls", weight=1.0, origin="livespec")

    assert ancestors_within(g, 1, 1, min_weight=0.6) == {3}
    assert ancestors_within(g, 1, 1, min_weight=0.6, edge_types=frozenset({"calls"})) == {3}
    assert ancestors_within(g, 1, 1, edge_types=frozenset({"references"})) == set()


def test_forward_direction_filters_the_same_way():
    g = _graph_with_mixed_edges()

    assert descendants_within(g, 4, 2) == {3, 1}
    assert descendants_within(g, 4, 2, edge_types=frozenset({"calls"})) == {3}


def test_invocation_set_is_exactly_what_livespec_itself_writes():
    """The default is a no-op on any index nobody has ingested into. If
    livespec's own extraction ever writes a third edge type, this list has to
    grow with it or the default starts hiding real callers."""
    assert INVOCATION_EDGE_TYPES == frozenset({"calls", "invokes_route"})


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def _repo(workspace: Path) -> None:
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text("class Base:\n    pass\n")
    (pkg / "service.py").write_text(
        "from pkg.models import Base\n\ndef describe(item: Base) -> str:\n    return 'x'\n"
    )


def _graph(workspace: Path, links: list[tuple[str, str, str]]) -> str:
    from livespec_mcp.state import get_state

    st = get_state(str(workspace))
    syms = {
        r["qualified_name"]: dict(r)
        for r in st.conn.execute(
            """SELECT s.qualified_name, s.name, s.start_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (st.project_id,),
        )
    }
    wanted = {q for link in links for q in link[:2]}
    nodes = [
        {
            "id": q,
            "label": syms[q]["name"],
            "source_file": syms[q]["file_path"],
            "source_location": f"L{syms[q]['start_line']}",
            "_callable": True,
            "_origin": "ast",
        }
        for q in sorted(wanted)
    ]
    p = workspace / "g.json"
    p.write_text(
        json.dumps(
            {
                "directed": True,
                "nodes": nodes,
                "links": [
                    {
                        "source": src,
                        "target": dst,
                        "relation": rel,
                        "confidence": "EXTRACTED",
                        "_origin": "ast",
                    }
                    for src, dst, rel in links
                ],
            }
        )
    )
    return str(p)


@pytest.mark.asyncio
async def test_a_clean_index_is_untouched_by_any_of_this(workspace: Path):
    """No ingest, no behaviour change, and no new noise in the payload."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("who_calls", {"qname": "pkg.service.describe"})).data

    assert out["edge_types"] == ["calls", "invokes_route"]
    assert "excluded_by_edge_type" not in out
    assert "external_edges" not in out


@pytest.mark.asyncio
async def test_the_excluded_dependency_is_named_and_recoverable(workspace: Path):
    """A filter that silently drops the reference trades one lie for another.
    The count must be honest AND the dependency must stay findable."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {
                "graph_path": _graph(
                    workspace, [("pkg.service.describe", "pkg.models.Base", "uses")]
                ),
                "dry_run": False,
            },
        )
        default = (await c.call_tool("who_calls", {"qname": "pkg.models.Base"})).data
        summary = (
            await c.call_tool("who_calls", {"qname": "pkg.models.Base", "summary_only": True})
        ).data

    assert default["count"] == 0
    assert default["excluded_by_edge_type"] == {"references": 1}
    hint = default["excluded_by_edge_type_hint"]
    assert "analyze_impact" in hint
    assert '"references"' in hint
    # summary_only must not hide which question was answered.
    assert summary["edge_types"] == ["calls", "invokes_route"]


@pytest.mark.asyncio
async def test_analyze_impact_still_counts_every_dependency(workspace: Path):
    """The other half of the argument. A type annotation DOES break when the
    type changes, so the blast-radius tool must keep seeing it — the filter is
    about what the word "caller" means, not about ignoring the edge."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {
                "graph_path": _graph(
                    workspace, [("pkg.service.describe", "pkg.models.Base", "uses")]
                ),
                "dry_run": False,
            },
        )
        impact = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "symbol", "target": "pkg.models.Base"},
            )
        ).data

    names = {m["qualified_name"] for m in impact["impacted_callers"]}
    assert "pkg.service.describe" in names


@pytest.mark.asyncio
async def test_every_graph_reading_tool_admits_the_borrowed_edges(workspace: Path):
    """An ingested edge moves PageRank, dead-code counts and coverage, not just
    `who_calls`. A tool whose number the ingest changed and that does not say so
    leaves the agent unable to calibrate what it is reading."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {
                "graph_path": _graph(
                    workspace, [("pkg.service.describe", "pkg.models.Base", "uses")]
                ),
                "dry_run": False,
            },
        )
        calls = {
            "quick_orient": {"qname": "pkg.models.Base"},
            "read_unit": {"qname": "pkg.service.describe"},
            "get_project_overview": {},
            "find_dead_code": {"summary_only": True},
            "find_orphan_tests": {"summary_only": True},
            "audit_coverage": {"summary_only": True},
            "who_calls": {"qname": "pkg.models.Base"},
            "who_does_this_call": {"qname": "pkg.service.describe"},
        }
        missing = []
        for tool, args in calls.items():
            payload = (await c.call_tool(tool, args)).data
            if "external_edges" not in payload:
                missing.append(tool)

    assert missing == [], f"these tools read the ingested graph but never say so: {missing}"


@pytest.mark.asyncio
async def test_saying_so_costs_nothing_on_an_index_without_an_ingest(
    workspace: Path,
):
    """The disclosure is attached to nine tools now, so it has to be free when
    there is nothing to disclose — an index seek, not a scan of every edge."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        payload = (await c.call_tool("get_project_overview", {})).data
    assert "external_edges" not in payload

    from livespec_mcp.domain.graph import _has_any_external_edge
    from livespec_mcp.state import get_state

    st = get_state(str(workspace))
    plan = [
        r[3]
        for r in st.conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM symbol_edge WHERE origin < 'livespec' LIMIT 1"
        )
    ]
    assert any("idx_edge_origin" in step for step in plan), plan
    assert _has_any_external_edge(st.conn) is False
