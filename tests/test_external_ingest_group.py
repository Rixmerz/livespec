"""Ingesting a merged graph across the repos of a group DB.

livespec already crosses repo boundaries in exactly one way: `route_ref` joins
a client call site to a server handler when both ends speak HTTP and the paths
line up. Everything else stops at the repo edge, because `symbol_edge` needs
both endpoints in the same database — which a `[workspace] group_db` provides,
and nothing was using for this.

Graphify ships `merge-graphs`, which fuses several `graph.json` files into one.
A group DB plus a merged graph is the only combination where livespec can hold
both ends of a non-HTTP cross-repo dependency: a shared type, a base class in a
common package, a direct import between two checked-out services.

The ingest used to look at `st.project_id` alone, so every one of those edges
landed in `skipped.endpoint_not_indexed` — the other end existed, in the same
database, three rows away.

The honest caveat is tested too: each repo stores paths relative to its own
root, so two repos with the same relative path produce one key both claim.
That stays dropped, and now says why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_ingest import EXTERNAL_ORIGIN
from livespec_mcp.server import mcp


def _repo(root: Path, module: str, body: str, *, group_db: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / f"{module}.py").write_text(body)
    (root / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{group_db}"\n')
    return root


def _symbols(workspace: Path) -> dict[str, dict]:
    """Every symbol in the whole group, keyed by qualified name."""
    from livespec_mcp.state import get_state

    st = get_state(str(workspace))
    pids = st.group_project_ids()
    placeholders = ",".join("?" for _ in pids)
    return {
        r["qualified_name"]: dict(r)
        for r in st.conn.execute(
            f"""SELECT s.qualified_name, s.name, s.start_line,
                       f.path AS file_path, f.project_id
                FROM symbol s JOIN file f ON f.id = s.file_id
                WHERE f.project_id IN ({placeholders})""",
            tuple(pids),
        )
    }


def _merged_graph(path: Path, syms: dict[str, dict], links: list[tuple[str, str, str]]) -> str:
    """What `graphify merge-graphs` produces: one graph spanning both trees."""
    wanted = {q for link in links for q in link[:2]}
    path.write_text(
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
    return str(path)


@pytest.mark.asyncio
async def test_an_edge_between_two_repos_of_a_group_is_ingested(tmp_path: Path):
    """The whole point. Both endpoints exist in one database, so the edge can."""
    shared = tmp_path / "grp" / "shared.db"
    back = _repo(
        tmp_path / "back",
        "models",
        "class Ticket:\n    pass\n",
        group_db=shared,
    )
    front = _repo(
        tmp_path / "front",
        "view",
        "def render(item):\n    return str(item)\n",
        group_db=shared,
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(back)})
        await c.call_tool("index_project", {"workspace": str(front)})
        syms = _symbols(front)
        graph = _merged_graph(
            tmp_path / "merged.json",
            syms,
            [("pkg.view.render", "pkg.models.Ticket", "uses")],
        )
        dry = (
            await c.call_tool(
                "ingest_external_graph",
                {"workspace": str(front), "graph_path": graph},
            )
        ).data
        assert dry["projects"] == 2
        assert dry["edges_to_add"] == 1, dry["skipped"]

        await c.call_tool(
            "ingest_external_graph",
            {"workspace": str(front), "graph_path": graph, "dry_run": False},
        )
        # Read from the OTHER repo: the dependency is on its symbol.
        seen = (
            await c.call_tool("who_calls", {"workspace": str(back), "qname": "pkg.models.Ticket"})
        ).data

    # It is NOT in the cone: the NetworkX view is built per project
    # (`WHERE f.project_id = ?`), so an edge whose ends sit in two repos is in
    # no view at all. Same reason `invokes_route` has always had its own lane,
    # and the same answer — a direct query that spans the database, reported
    # separately rather than folded into a count that means "within this repo".
    assert seen["count"] == 0
    peer = seen["cross_repo_callers"][0]
    assert peer["qualified_name"] == "pkg.view.render"
    assert peer["edge_type"] == "references"
    assert peer["via_external_edge"] == EXTERNAL_ORIGIN
    assert peer["project_root"] == str(front)
    assert "another repo" in seen["cross_repo_callers_hint"]


@pytest.mark.asyncio
async def test_remove_takes_the_cross_repo_edges_back_out(tmp_path: Path):
    """Reversibility has to survive the group too: a delete scoped to the home
    project would strand every edge whose source lives in a sibling repo."""
    shared = tmp_path / "grp" / "shared.db"
    back = _repo(tmp_path / "back", "models", "class Ticket:\n    pass\n", group_db=shared)
    front = _repo(
        tmp_path / "front",
        "view",
        "def render(item):\n    return str(item)\n",
        group_db=shared,
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(back)})
        await c.call_tool("index_project", {"workspace": str(front)})
        graph = _merged_graph(
            tmp_path / "merged.json",
            _symbols(front),
            [("pkg.view.render", "pkg.models.Ticket", "uses")],
        )
        await c.call_tool(
            "ingest_external_graph",
            {"workspace": str(front), "graph_path": graph, "dry_run": False},
        )
        # Removing from the BACK repo must still reach an edge whose source is
        # in FRONT — same group, one database.
        removed = (
            await c.call_tool("ingest_external_graph", {"workspace": str(back), "remove": True})
        ).data
        after = (
            await c.call_tool("who_calls", {"workspace": str(back), "qname": "pkg.models.Ticket"})
        ).data

    assert removed["removed"] == 1
    assert "cross_repo_callers" not in after


@pytest.mark.asyncio
async def test_two_repos_sharing_a_path_are_dropped_and_told_why(tmp_path: Path):
    """The honest limit. Each repo stores paths relative to its own root, so
    `pkg/models.py` in two repos is one (file, line) key that both claim.

    Dropping is right — writing the edge into the wrong repo is worse than not
    writing it — but "ambiguous" alone would send someone hunting for a bad
    graph. The cause is colliding paths, and no re-run changes it.
    """
    shared = tmp_path / "grp" / "shared.db"
    a = _repo(tmp_path / "a", "models", "class Ticket:\n    pass\n", group_db=shared)
    b = _repo(tmp_path / "b", "models", "class Ticket:\n    pass\n", group_db=shared)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(a)})
        await c.call_tool("index_project", {"workspace": str(b)})
        syms = _symbols(a)
        # One node at the colliding path, plus a link so it is actually walked.
        graph_path = tmp_path / "merged.json"
        graph_path.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": [
                        {
                            "id": "collide",
                            "label": "Ticket",
                            "source_file": syms["pkg.models.Ticket"]["file_path"],
                            "source_location": f"L{syms['pkg.models.Ticket']['start_line']}",
                            "_callable": True,
                            "_origin": "ast",
                        }
                    ],
                    "links": [],
                }
            )
        )
        dry = (
            await c.call_tool(
                "ingest_external_graph",
                {"workspace": str(a), "graph_path": str(graph_path)},
            )
        ).data

    assert dry["ambiguous_nodes"] == 1
    assert dry["ambiguous_cross_project"] == 1
    assert "relative to its own root" in dry["ambiguous_cross_project_hint"]
    assert dry["edges_to_add"] == 0


@pytest.mark.asyncio
async def test_an_ungrouped_workspace_behaves_exactly_as_before(tmp_path: Path):
    """`group_project_ids()` is `[project_id]` without a group DB, so this is
    the same query it always ran — asserted rather than assumed, because the
    default install is the one that must not move."""
    root = tmp_path / "solo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "models.py").write_text("class Ticket:\n    pass\n")
    (root / "pkg" / "view.py").write_text(
        "from pkg.models import Ticket\n\n\ndef render(i: Ticket):\n    return 1\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(root)})
        graph = _merged_graph(
            tmp_path / "g.json",
            _symbols(root),
            [("pkg.view.render", "pkg.models.Ticket", "uses")],
        )
        dry = (
            await c.call_tool(
                "ingest_external_graph",
                {"workspace": str(root), "graph_path": graph},
            )
        ).data

    assert dry["projects"] == 1
    assert dry["edges_to_add"] == 1
    assert "ambiguous_cross_project" not in dry
