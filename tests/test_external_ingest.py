"""Ingesting a second extractor's edges into livespec's own call graph.

v0.32 could only *consume* a Graphify graph as corroborating evidence: it
removed dead-code candidates and wrote nothing. That left the useful half
undone, because the questions an agent actually asks — `who_calls`,
`analyze_impact` — read `symbol_edge`, which corroboration never touches. So a
base class kept alive only by `extends`, or a type used only in a parameter
annotation, still reported zero callers no matter how many external graphs were
lying around.

`ingest_external_graph` writes those edges. Measured on this repo
(2026-09-03), against a code-only Graphify run of its own tree — 3564 nodes,
6089 edges, `input_tokens: 0`:

    1394 of 1593 livespec symbols matched an external node (1 ambiguous)
    1250 external `calls` edges livespec already had  (95% agreement)
     165 edges livespec lacked  (63 calls, 2 indirect_call, 83 uses, 17 references)

and `who_calls(ExternalNode)` went from 1 caller to 5 — the four methods that
take it as a type-position parameter, which livespec does not model at all.

What these tests protect is the part that can go wrong quietly. An ingest that
adds symbols, that cannot be undone, that depends on how many times it has been
run, or that lets an importer read as a caller, is worse than no ingest: the
whole product's numbers become partly someone else's without saying so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_ingest import (
    DEFAULT_RELATIONS,
    EXTERNAL_ORIGIN,
    RELATION_EDGE_TYPE,
)
from livespec_mcp.server import mcp

# --------------------------------------------------------------------------
# Fixtures: a repo whose only link between two symbols is one livespec cannot
# see, plus a graph.json that does see it.
# --------------------------------------------------------------------------


def _repo(workspace: Path) -> None:
    """`Base` is referenced only in type position — a livespec blind spot."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text(
        "class Base:\n    def tag(self) -> str:\n        return 'base'\n"
    )
    (pkg / "service.py").write_text(
        "from pkg.models import Base\n"
        "\n"
        "def describe(item: Base) -> str:\n"
        "    return 'x'\n"
        "\n"
        "def main() -> str:\n"
        "    return describe(None)\n"
    )


def _symbols(workspace: Path) -> dict[str, dict]:
    """qualified_name -> {file_path, start_line, name} for the indexed repo."""
    from livespec_mcp.state import get_state

    st = get_state(str(workspace))
    return {
        r["qualified_name"]: dict(r)
        for r in st.conn.execute(
            """SELECT s.qualified_name, s.name, s.start_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (st.project_id,),
        )
    }


def _edge_rows(workspace: Path) -> list[tuple]:
    from livespec_mcp.state import get_state

    st = get_state(str(workspace))
    return [
        tuple(r)
        for r in st.conn.execute(
            """SELECT e.src_symbol_id, e.dst_symbol_id, e.edge_type, e.origin
               FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?
               ORDER BY 1, 2, 3""",
            (st.project_id,),
        )
    ]


def _graph(workspace: Path, links: list[tuple[str, str, str]], *, name: str = "g.json") -> str:
    """Write a Graphify-shaped graph over the indexed symbols.

    `links` are `(source qname, target qname, relation)` triples; positions are
    taken from the index so the reader's position lookup matches exactly.
    """
    syms = _symbols(workspace)
    wanted = {q for link in links for q in link[:2]}
    nodes, node_id = [], {}
    for i, qname in enumerate(sorted(wanted)):
        meta = syms[qname]
        node_id[qname] = f"n{i}"
        nodes.append(
            {
                "id": f"n{i}",
                "label": meta["name"],
                "source_file": meta["file_path"],
                "source_location": f"L{meta['start_line']}",
                "_callable": True,
                "_origin": "ast",
                "community": 0,
            }
        )
    payload = {
        "directed": True,
        "nodes": nodes,
        "links": [
            {
                "source": node_id[src],
                "target": node_id[dst],
                "relation": relation,
                "confidence": "EXTRACTED",
                "_origin": "ast",
            }
            for src, dst, relation in links
        ],
    }
    p = workspace / name
    p.write_text(json.dumps(payload))
    return str(p)


TYPE_POSITION_LINK = ("pkg.service.describe", "pkg.models.Base", "references")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_edge_we_extract_is_labelled_ours(workspace: Path):
    """The migration's default is what makes the column trustworthy.

    If existing rows came back NULL, every provenance check downstream would
    have to treat "unknown" as "ours", which is the assumption the column
    exists to stop making."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    origins = {row[3] for row in _edge_rows(workspace)}
    assert origins == {"livespec"}


# --------------------------------------------------------------------------
# The boundary that matters most: the symbol table stays ours
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_never_creates_a_symbol(workspace: Path):
    """An external node with no livespec counterpart is counted, not created."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        before = len(_symbols(workspace))
        graph_path = _graph(workspace, [TYPE_POSITION_LINK])
        # Add a node for a symbol livespec does not have at all.
        raw = json.loads(Path(graph_path).read_text())
        raw["nodes"].append(
            {
                "id": "ghost",
                "label": "neverIndexed",
                "source_file": "pkg/service.py",
                "source_location": "L900",
                "_callable": True,
                "_origin": "ast",
            }
        )
        raw["links"].append(
            {"source": "ghost", "target": "n0", "relation": "calls", "_origin": "ast"}
        )
        Path(graph_path).write_text(json.dumps(raw))

        out = (
            await c.call_tool(
                "ingest_external_graph",
                {"graph_path": graph_path, "dry_run": False},
            )
        ).data
    assert len(_symbols(workspace)) == before
    assert out["skipped"].get("endpoint_not_indexed", 0) >= 1


# --------------------------------------------------------------------------
# Reversibility and idempotence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        before = _edge_rows(workspace)
        out = (
            await c.call_tool(
                "ingest_external_graph",
                {"graph_path": _graph(workspace, [TYPE_POSITION_LINK])},
            )
        ).data
    assert out["dry_run"] is True
    assert out["edges_to_add"] == 1
    assert "next" in out
    assert _edge_rows(workspace) == before


@pytest.mark.asyncio
async def test_remove_restores_the_livespec_only_graph_exactly(workspace: Path):
    """Not "roughly the same count" — the same rows.

    A delete predicate that is even slightly too wide would take livespec's own
    edges with it, and the symptom (a few missing callers) is invisible until
    someone deletes code that was not dead."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        before = _edge_rows(workspace)
        graph_path = _graph(workspace, [TYPE_POSITION_LINK])
        await c.call_tool("ingest_external_graph", {"graph_path": graph_path, "dry_run": False})
        assert len(_edge_rows(workspace)) == len(before) + 1
        removed = (await c.call_tool("ingest_external_graph", {"remove": True})).data
    assert removed["removed"] == 1
    assert _edge_rows(workspace) == before


@pytest.mark.asyncio
async def test_applying_twice_leaves_one_copy_of_each_edge(workspace: Path):
    """State after an ingest depends on the graph and the index, never on how
    many times ingest ran."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        graph_path = _graph(workspace, [TYPE_POSITION_LINK])
        args = {"graph_path": graph_path, "dry_run": False}
        first = (await c.call_tool("ingest_external_graph", args)).data
        after_first = _edge_rows(workspace)
        second = (await c.call_tool("ingest_external_graph", args)).data
    assert first["edges_replaced"] == 0
    assert second["edges_replaced"] == 1
    assert second["edges_added"] == 1
    assert _edge_rows(workspace) == after_first


@pytest.mark.asyncio
async def test_a_second_dry_run_still_predicts_the_work(workspace: Path):
    """Rows this ingest owns are not counted as agreement.

    They are deleted and rewritten on every apply, so counting them as
    "livespec already has this" would make a dry run predict a no-op for a run
    that rewrites 145 rows."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        graph_path = _graph(workspace, [TYPE_POSITION_LINK])
        await c.call_tool("ingest_external_graph", {"graph_path": graph_path, "dry_run": False})
        again = (await c.call_tool("ingest_external_graph", {"graph_path": graph_path})).data
    assert again["edges_to_add"] == 1
    assert again["already_known"] == 0


# --------------------------------------------------------------------------
# What the edges are worth
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_type_position_use_is_reported_but_not_as_a_caller(
    workspace: Path,
):
    """The whole point, and the honesty tax that comes with it.

    `describe(item: Base)` does not CALL `Base`; it names it in a parameter
    annotation. That is a real dependency — changing `Base` can break
    `describe` — and it is exactly the blind spot the ingest exists to fill.
    But putting it in a list labelled "callers" makes `who_calls` lie, and the
    first version of this feature did: measured on livespec itself, one class
    went from 2 callers to 40 after an ingest, 38 of them methods taking it as
    a parameter type. An agent reading "40 callers" cannot tell which two
    actually call it.

    So the default answer is unchanged by the ingest, and the dependency is
    reported next to it with the argument that reveals it.
    """
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        base = (await c.call_tool("who_calls", {"qname": "pkg.models.Base"})).data
        assert base["count"] == 0

        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [TYPE_POSITION_LINK]), "dry_run": False},
        )
        after = (await c.call_tool("who_calls", {"qname": "pkg.models.Base"})).data
        widened = (
            await c.call_tool(
                "who_calls",
                {"qname": "pkg.models.Base", "edge_types": ["calls", "references"]},
            )
        ).data

    # Default: not a caller, but impossible to miss.
    assert after["count"] == 0
    assert after["excluded_by_edge_type"] == {"references": 1}
    assert "edge_types" in after["excluded_by_edge_type_hint"]
    assert after["external_edges"]["by_origin"] == {EXTERNAL_ORIGIN: 1}

    # Opted in: there it is, labelled with the edge and with whose claim it is.
    assert widened["count"] == 1
    caller = widened["callers"][0]
    assert caller["qualified_name"] == "pkg.service.describe"
    assert caller["edge_type"] == "references"
    assert caller["via_external_edge"] == EXTERNAL_ORIGIN


@pytest.mark.asyncio
async def test_an_ingested_call_edge_does_become_a_caller(workspace: Path):
    """The filter must not throw the baby out. A `calls` relation livespec's
    resolver missed is a caller, and arrives as one by default."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        graph_path = _graph(workspace, [("pkg.service.main", "pkg.models.Base.tag", "calls")])
        await c.call_tool("ingest_external_graph", {"graph_path": graph_path, "dry_run": False})
        after = (await c.call_tool("who_calls", {"qname": "pkg.models.Base.tag"})).data

    assert after["count"] == 1
    assert after["callers"][0]["qualified_name"] == "pkg.service.main"
    assert after["callers"][0]["edge_type"] == "calls"
    assert after["callers"][0]["via_external_edge"] == EXTERNAL_ORIGIN
    assert "excluded_by_edge_type" not in after


@pytest.mark.asyncio
async def test_dead_code_shrinks_and_admits_why(workspace: Path):
    """An ingested edge kills a dead candidate in the base query, long before
    any reporting code runs. The count must not read as a livespec finding."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        before = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        # `main` is the only candidate: `Base`/`tag` are filtered as
        # infrastructure long before corroboration or ingestion get a say.
        assert before["count"] == 1
        assert "external_edges" not in before

        await c.call_tool(
            "ingest_external_graph",
            {
                "graph_path": _graph(
                    workspace, [("pkg.models.Base.tag", "pkg.service.main", "calls")]
                ),
                "dry_run": False,
            },
        )
        after = (await c.call_tool("find_dead_code", {"summary_only": True})).data
    assert after["count"] == before["count"] - 1
    assert after["external_edges"]["by_origin"] == {EXTERNAL_ORIGIN: 1}


@pytest.mark.asyncio
async def test_analyze_impact_reports_the_borrowed_evidence_too(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [TYPE_POSITION_LINK]), "dry_run": False},
        )
        out = (
            await c.call_tool(
                "analyze_impact", {"target_type": "symbol", "target": "pkg.models.Base"}
            )
        ).data
    # `describe` via the ingested edge, then `main` which calls `describe`:
    # an ingested edge is an ordinary edge, so the cone walks through it.
    assert out["counts"]["impacted_callers"] == 2
    assert out["external_edges"]["by_origin"] == {EXTERNAL_ORIGIN: 1}


# --------------------------------------------------------------------------
# Import wiring stays out of the cone unless asked for
# --------------------------------------------------------------------------


def test_import_relations_are_known_but_not_default():
    assert {"imports", "imports_from", "re_exports"} <= set(RELATION_EDGE_TYPE)
    assert not (DEFAULT_RELATIONS & {"imports", "imports_from", "re_exports"})


@pytest.mark.asyncio
async def test_an_import_is_not_ingested_as_a_caller_by_default(workspace: Path):
    """`main` imports nothing of `Base`, but Graphify records the module wiring.
    Ingested by default, that row would make `who_calls(Base)` report an
    importer as a caller."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        graph_path = _graph(workspace, [("pkg.service.main", "pkg.models.Base", "imports")])
        default = (await c.call_tool("ingest_external_graph", {"graph_path": graph_path})).data
        opted_in = (
            await c.call_tool(
                "ingest_external_graph",
                {"graph_path": graph_path, "relations": ["imports"]},
            )
        ).data
    assert default["edges_to_add"] == 0
    assert default["skipped"]["relation_not_requested"] == 1
    assert opted_in["edges_to_add"] == 1
    assert "import_relations_warning" in opted_in


@pytest.mark.asyncio
async def test_an_unknown_relation_is_a_shaped_error(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "ingest_external_graph",
                {
                    "graph_path": _graph(workspace, [TYPE_POSITION_LINK]),
                    "relations": ["calls", "summons"],
                },
            )
        ).data
    assert out["isError"] is True
    assert "summons" in out["error"]
    assert "calls" in out["did_you_mean"]


# --------------------------------------------------------------------------
# Failing loudly (the v0.32 invariant, carried forward)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_graph_of_a_different_repo_is_refused(workspace: Path):
    """Reported as "0 edges to add", a foreign graph would read as agreement."""
    _repo(workspace)
    foreign = workspace / "foreign.json"
    foreign.write_text(
        json.dumps(
            {
                "directed": True,
                "nodes": [
                    {
                        "id": "x",
                        "label": "thing",
                        "source_file": "somewhere/else.rb",
                        "source_location": "L2",
                        "_callable": True,
                        "_origin": "ast",
                    }
                ],
                "links": [],
            }
        )
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("ingest_external_graph", {"graph_path": str(foreign)})).data
    assert out["isError"] is True
    assert "almost no files" in out["error"]


@pytest.mark.asyncio
async def test_no_graph_anywhere_is_an_error_not_an_empty_success(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("ingest_external_graph", {})).data
    assert out["isError"] is True
    assert "No external graph" in out["error"]


# --------------------------------------------------------------------------
# Staleness: the index moves, the ingested rows do not
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reindexing_after_an_ingest_says_the_edges_may_be_stale(
    workspace: Path,
):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [TYPE_POSITION_LINK]), "dry_run": False},
        )
        (workspace / "pkg" / "service.py").write_text(
            "from pkg.models import Base\n\n\ndef describe(item: Base) -> str:\n"
            "    return 'y'\n\n\ndef main() -> str:\n    return describe(None)\n"
        )
        out = (await c.call_tool("index_project", {})).data
    stale = out["external_edges_stale"]
    assert stale["by_origin"] == {EXTERNAL_ORIGIN: 1}
    # The edge's source symbol lived in the file that changed, so the cascade
    # took it. Counting only survivors would have reported nothing at all.
    assert stale["dropped_by_reextract"] == 1
    assert stale["surviving_by_origin"] == {}


@pytest.mark.asyncio
async def test_a_quiet_reindex_does_not_cry_stale(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [TYPE_POSITION_LINK]), "dry_run": False},
        )
        out = (await c.call_tool("index_project", {})).data
    assert "external_edges_stale" not in out


@pytest.mark.asyncio
async def test_our_own_resolver_reclaims_an_edge_it_later_derives(workspace: Path):
    """An ingested label must not outlive the ingest's usefulness.

    The window is narrow — an ingested row only survives a re-index when
    neither endpoint's file changed, since the FK cascade takes the rest — but
    inside it the resolver can derive an edge an ingest already wrote. Left
    labelled external, `who_calls` would keep attributing to a `graph.json` a
    caller livespec found on its own, and `remove=True` would delete an edge
    livespec earned. So the resolver's upsert claims the row, exactly as it
    already claims the weight.

    Driven at the resolver's own statement rather than through a repo, because
    staging the collision end-to-end means engineering a partial re-index whose
    only observable effect is which of two identical rows won."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [TYPE_POSITION_LINK]), "dry_run": False},
        )

    from livespec_mcp.state import get_state

    st = get_state(str(workspace))
    src, dst, edge_type, origin = next(r for r in _edge_rows(workspace) if r[3] == EXTERNAL_ORIGIN)
    assert origin == EXTERNAL_ORIGIN

    # Verbatim the upsert `_resolve_refs` runs for every edge it derives.
    st.conn.execute(
        """INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight)
           VALUES(?,?,?,?)
           ON CONFLICT(src_symbol_id, dst_symbol_id, edge_type)
           DO UPDATE SET weight = MAX(symbol_edge.weight, excluded.weight),
                         origin = 'livespec'""",
        (src, dst, edge_type, 1.0),
    )
    row = st.conn.execute(
        "SELECT origin, weight FROM symbol_edge WHERE src_symbol_id=? "
        "AND dst_symbol_id=? AND edge_type=?",
        (src, dst, edge_type),
    ).fetchone()
    assert row["origin"] == "livespec"
    assert row["weight"] == 1.0

    # And having reclaimed it, `remove=True` must leave it alone.
    async with Client(mcp) as c:
        removed = (await c.call_tool("ingest_external_graph", {"remove": True})).data
    assert removed["removed"] == 0
    assert any(r[:3] == (src, dst, edge_type) for r in _edge_rows(workspace))


def test_a_node_two_symbols_both_claim_is_dropped_not_guessed(tmp_path: Path):
    """A wrong mapping writes a wrong edge into the call graph.

    Position and name are both fallible — a decorator line, an `if/else def`
    shim, two overloads at the same spot. When they disagree the honest move is
    to lose the edge, because a missing caller is a gap and an invented one is
    a lie that survives into `analyze_impact`."""
    from livespec_mcp.domain.external_graph import load_external_graph
    from livespec_mcp.domain.external_ingest import map_nodes_to_symbols

    graph_file = tmp_path / "g.json"
    graph_file.write_text(
        json.dumps(
            {
                "directed": True,
                "nodes": [
                    {
                        "id": "shared",
                        "label": "handler",
                        "source_file": "app.py",
                        "source_location": "L10",
                        "_callable": True,
                        "_origin": "ast",
                    }
                ],
                "links": [],
            }
        )
    )
    graph = load_external_graph(graph_file)
    both = [
        {"id": 1, "name": "handler", "start_line": 10, "file_path": "app.py"},
        {"id": 2, "name": "handler", "start_line": 10, "file_path": "app.py"},
    ]
    mapping, ambiguous, cross_project = map_nodes_to_symbols(graph, both)
    assert mapping == {}
    assert ambiguous == 1
    # Both claimants are in the same project here, so this is the fallible-
    # position case, not the colliding-paths-across-a-group case.
    assert cross_project == 0

    mapping, ambiguous, cross_project = map_nodes_to_symbols(graph, both[:1])
    assert mapping == {"shared": 1}
    assert ambiguous == 0
    assert cross_project == 0

    # Two repos of a group DB, each storing `app.py` relative to its own root.
    # Still dropped — writing the edge into the wrong repo is worse — but the
    # cause is reported separately, because no amount of re-running fixes it.
    grouped = [
        {**both[0], "project_id": 1},
        {**both[1], "project_id": 2},
    ]
    mapping, ambiguous, cross_project = map_nodes_to_symbols(graph, grouped)
    assert mapping == {}
    assert ambiguous == 1
    assert cross_project == 1
