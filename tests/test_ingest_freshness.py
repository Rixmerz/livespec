"""An ingest that cannot say when it happened cannot say it has gone stale.

Ingestion was reversible and idempotent from the start, and amnesiac. The rows
carried `origin`; nothing recorded the file they came from, its state at the
time, or how many were written. So the only moment anyone could learn that
ingested edges no longer described the code was the `index_project` run that
destroyed some of them — it sampled the count before and after. Every later
call, every `who_calls` reading those rows, saw nothing.

And the rows that run did *not* destroy are the dangerous half: still in the
graph, still answering questions, derived from a graph.json built against code
that has since moved.

Migration 23 records the provenance, `ingest_freshness` reads it back, and the
`external_edges` block every graph-reading tool already carries is where it
surfaces. This file pins all three, plus the two things that make it affordable
(a parse cache) and safe (the write lock).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_graph import (
    _GRAPH_CACHE,
    clear_external_graph_cache,
    load_external_graph,
)
from livespec_mcp.domain.external_ingest import (
    EXTERNAL_ORIGIN,
    ingest_freshness,
    read_ingest,
)
from livespec_mcp.server import mcp


def _repo(workspace: Path) -> None:
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text("class Base:\n    pass\n")
    (pkg / "service.py").write_text(
        "from pkg.models import Base\n"
        "\n"
        "def describe(item: Base) -> str:\n"
        "    return 'x'\n"
    )


def _graph(workspace: Path, links: list[tuple[str, str, str]], *, name="g.json") -> str:
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
    p = workspace / name
    p.write_text(
        json.dumps(
            {
                "directed": True,
                "nodes": [
                    {
                        "id": q,
                        "label": syms[q]["name"],
                        "source_file": syms[q]["file_path"],
                        "source_location": f"L{syms[q]['start_line']}",
                        "_callable": True,
                        "_origin": "ast",
                    }
                    for q in sorted(wanted)
                ],
                "links": [
                    {
                        "source": s,
                        "target": d,
                        "relation": r,
                        "confidence": "EXTRACTED",
                        "_origin": "ast",
                    }
                    for s, d, r in links
                ],
            }
        )
    )
    return str(p)


USE_LINK = ("pkg.service.describe", "pkg.models.Base", "uses")


def _state(workspace: Path):
    from livespec_mcp.state import get_state

    return get_state(str(workspace))


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_applied_ingest_remembers_which_graph_it_read(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        path = _graph(workspace, [USE_LINK])
        await c.call_tool(
            "ingest_external_graph", {"graph_path": path, "dry_run": False}
        )
        rows = read_ingest(_state(workspace).conn, _state(workspace).project_id)

    assert len(rows) == 1
    row = rows[0]
    assert row["origin"] == EXTERNAL_ORIGIN
    assert row["graph_path"] == path
    assert row["edges_written"] == 1
    assert row["graph_hash"]
    assert json.loads(row["relations"])


@pytest.mark.asyncio
async def test_a_dry_run_remembers_nothing(workspace: Path):
    """It wrote no edges, so claiming an ingest happened would make the next
    freshness check compare against a state that never existed."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph", {"graph_path": _graph(workspace, [USE_LINK])}
        )
        assert read_ingest(_state(workspace).conn, _state(workspace).project_id) == []


@pytest.mark.asyncio
async def test_provenance_replaces_itself_rather_than_accumulating(workspace: Path):
    """Same rule as the edges: the state after an ingest depends on the current
    graph and index, never on how many times it has run."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        path = _graph(workspace, [USE_LINK])
        for _ in range(3):
            await c.call_tool(
                "ingest_external_graph", {"graph_path": path, "dry_run": False}
            )
        rows = read_ingest(_state(workspace).conn, _state(workspace).project_id)

    assert len(rows) == 1


@pytest.mark.asyncio
async def test_remove_forgets_the_ingest_too(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [USE_LINK]), "dry_run": False},
        )
        await c.call_tool("ingest_external_graph", {"remove": True})
        rows = read_ingest(_state(workspace).conn, _state(workspace).project_id)

    assert rows == []


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fresh_ingest_says_nothing(workspace: Path):
    """Silent on the happy path — a warning that is always on is a warning
    nobody reads when it means something."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [USE_LINK]), "dry_run": False},
        )
        payload = (await c.call_tool("who_calls", {"qname": "pkg.models.Base"})).data

    assert "stale" not in payload["external_edges"]


@pytest.mark.asyncio
async def test_every_read_tool_learns_the_edges_went_stale(workspace: Path):
    """The gap this closes. Before, only the index run that destroyed edges
    could say anything, and the survivors it did not destroy went on answering
    `who_calls` from a graph built against code that has moved."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [USE_LINK]), "dry_run": False},
        )
        # Change the file the ingested edge points INTO, then reindex: the FK
        # cascade takes the edge with the symbol.
        (workspace / "pkg" / "models.py").write_text(
            "class Base:\n    def added(self):\n        return 1\n"
        )
        await c.call_tool("index_project", {})
        payload = (
            await c.call_tool("find_dead_code", {"summary_only": True})
        ).data

    stale = payload["external_edges"]["stale"][EXTERNAL_ORIGIN]
    assert "edges_lost" in stale["status"]
    assert stale["edges_written"] == 1
    assert stale["edges_now"] == 0
    assert "re-extract" in stale["hint"]


@pytest.mark.asyncio
async def test_a_rewritten_graph_is_reported_as_changed(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        path = _graph(workspace, [USE_LINK])
        await c.call_tool(
            "ingest_external_graph", {"graph_path": path, "dry_run": False}
        )
        # Same path, different content — what `graphify update` produces after
        # the code changed.
        _graph(workspace, [("pkg.service.describe", "pkg.models.Base", "references")])
        st = _state(workspace)
        from livespec_mcp.domain.graph import external_edge_summary

        stale = ingest_freshness(
            st.conn, st.project_id, external_edge_summary(st.conn, st.project_id) or {}
        )

    assert "graph_changed" in stale[EXTERNAL_ORIGIN]["status"]


@pytest.mark.asyncio
async def test_rewriting_the_same_content_is_not_a_change(workspace: Path):
    """`graphify update` and `graphify watch` rewrite graph.json on every run,
    so mtime moves constantly while the content usually does not. Crying stale
    on every watcher tick would train the reader to ignore the field."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        path = Path(_graph(workspace, [USE_LINK]))
        await c.call_tool(
            "ingest_external_graph", {"graph_path": str(path), "dry_run": False}
        )
        same = path.read_text()
        path.write_text(same)  # new mtime, identical bytes
        st = _state(workspace)
        from livespec_mcp.domain.graph import external_edge_summary

        stale = ingest_freshness(
            st.conn, st.project_id, external_edge_summary(st.conn, st.project_id) or {}
        )

    assert stale == {}


@pytest.mark.asyncio
async def test_a_deleted_graph_says_so_and_offers_the_exit(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        path = Path(_graph(workspace, [USE_LINK]))
        await c.call_tool(
            "ingest_external_graph", {"graph_path": str(path), "dry_run": False}
        )
        path.unlink()
        payload = (await c.call_tool("who_calls", {"qname": "pkg.models.Base"})).data

    stale = payload["external_edges"]["stale"][EXTERNAL_ORIGIN]
    assert "graph_missing" in stale["status"]
    assert "remove=True" in stale["hint"]


# --------------------------------------------------------------------------
# Auto-ingest
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_ingest_is_off_unless_asked(workspace: Path):
    """An ingest writes rows into `symbol_edge`. A write that happens because
    someone saved a file is not something to opt anyone into silently."""
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [USE_LINK]), "dry_run": False},
        )
        (workspace / "pkg" / "models.py").write_text("class Base:\n    x = 1\n")
        result = (await c.call_tool("index_project", {})).data

    assert "external_ingest_refreshed" not in result
    assert result["external_edges_stale"]["dropped_by_reextract"] == 1


@pytest.mark.asyncio
async def test_auto_ingest_rebuilds_the_edges_a_reextract_destroyed(workspace: Path):
    _repo(workspace)
    (workspace / ".livespec.toml").write_text("[graph]\nauto_ingest = true\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool(
            "ingest_external_graph",
            {"graph_path": _graph(workspace, [USE_LINK]), "dry_run": False},
        )
        before = (await c.call_tool("who_calls", {"qname": "pkg.models.Base"})).data
        assert before["excluded_by_edge_type"] == {"references": 1}

        (workspace / "pkg" / "models.py").write_text("class Base:\n    x = 1\n")
        result = (await c.call_tool("index_project", {})).data
        after = (await c.call_tool("who_calls", {"qname": "pkg.models.Base"})).data

    assert result["external_ingest_refreshed"]["edges_added"] == 1
    assert after["excluded_by_edge_type"] == {"references": 1}
    assert "stale" not in after["external_edges"]


@pytest.mark.asyncio
async def test_auto_ingest_never_fails_the_index(workspace: Path):
    """A missing graph leaves exactly the situation the staleness report
    already describes. Failing the whole index over it would be worse."""
    _repo(workspace)
    (workspace / ".livespec.toml").write_text("[graph]\nauto_ingest = true\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        path = Path(_graph(workspace, [USE_LINK]))
        await c.call_tool(
            "ingest_external_graph", {"graph_path": str(path), "dry_run": False}
        )
        path.unlink()
        (workspace / "pkg" / "models.py").write_text("class Base:\n    x = 1\n")
        result = (await c.call_tool("index_project", {})).data

    assert result["symbols_total"] > 0
    assert "external_ingest_refresh_failed" in result


# --------------------------------------------------------------------------
# The parse cache
# --------------------------------------------------------------------------


def test_the_same_graph_is_parsed_once(tmp_path: Path):
    """`[graph] external` makes corroboration the default for a repo, and then
    every `find_dead_code` re-parses the file — measured at 522 ms per call on
    this repo's own 3.8 MB graph, which is a small one."""
    clear_external_graph_cache()
    p = tmp_path / "g.json"
    p.write_text(
        json.dumps(
            {
                "directed": True,
                "nodes": [
                    {
                        "id": "a",
                        "label": "a",
                        "source_file": "a.py",
                        "source_location": "L1",
                        "_callable": True,
                    }
                ],
                "links": [],
            }
        )
    )
    first = load_external_graph(p)
    second = load_external_graph(p)

    assert first is second


def test_rewriting_the_file_invalidates_it(tmp_path: Path):
    """Keyed on (mtime, size), so `graphify update` invalidates the cache by
    writing the file — which is exactly when the parse should be redone, and
    there is no explicit invalidation anyone can forget to call."""
    clear_external_graph_cache()
    p = tmp_path / "g.json"

    def _write(label: str) -> None:
        p.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": [
                        {
                            "id": "a",
                            "label": label,
                            "source_file": "a.py",
                            "source_location": "L1",
                            "_callable": True,
                        }
                    ],
                    "links": [],
                }
            )
        )

    _write("first")
    a = load_external_graph(p)
    _write("second_label_is_longer")
    b = load_external_graph(p)

    assert a is not b
    assert b.by_id["a"].label == "second_label_is_longer"


def test_the_cache_stays_small(tmp_path: Path):
    """Each entry is a full parse of a multi-megabyte file; an unbounded cache
    here is a memory leak on a monorepo with several graphs."""
    clear_external_graph_cache()
    for i in range(10):
        p = tmp_path / f"g{i}.json"
        p.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": [
                        {
                            "id": "a",
                            "label": f"a{i}",
                            "source_file": "a.py",
                            "source_location": "L1",
                            "_callable": True,
                        }
                    ],
                    "links": [],
                }
            )
        )
        load_external_graph(p)

    assert len(_GRAPH_CACHE) <= 4
