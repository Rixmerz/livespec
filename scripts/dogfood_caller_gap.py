#!/usr/bin/env python3
"""Measure the callers livespec's cone lacks and an external graph has.

`find_dead_code(corroborate_with=…)` was measured across 14 repos before it
shipped, and the sweep is what caught the `method` relation inflating the
result by nearly half. This is the same measurement for the other direction:
`who_calls` / `analyze_impact` gained an `external_callers` lane, and the claim
"the external graph knows callers we do not" deserves a number rather than an
anecdote.

For each repo it reports, in livespec's own vocabulary (both endpoints resolved
back to indexed symbols, so every pair is actionable):

    agreed          caller pairs both extractors have
    external_only   caller pairs only the external graph has  <- the lane
    unreachable     of those, with no path at all in livespec's graph
    by_relation     which relations they come from

The `unreachable` split is the honest one. A pair livespec can already reach by
another route is an attribution difference — the external extractor charging a
call to the enclosing function rather than the nested closure that makes it.
A pair with no path at all is a caller livespec genuinely cannot see.

Repo paths come from the environment so private trees never land in this tree,
and the report labels repos positionally (`repo-01`) rather than by name — same
convention as `dogfood_corroboration.py`::

    LIVESPEC_CALLERGAP_ROOT=/abs/dir/of/repos \\
    LIVESPEC_CALLERGAP_OUT=/abs/out.json      \\
    uv run python scripts/dogfood_caller_gap.py

Explicit paths as positional arguments work too. Set
`LIVESPEC_CALLERGAP_KEEP_NAMES=1` to include real repo names locally (never
commit that output). Graphs are reused from `graphify-out/graph.json` when
present and built with Graphify otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import networkx as nx
from fastmcp import Client

from livespec_mcp.domain.external_graph import (
    EVIDENCE_RELATIONS,
    build_claim_index,
    load_external_graph,
)
from livespec_mcp.server import mcp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dogfood_corroboration import _graphify_python, build_graph

KEEP_NAMES = os.environ.get("LIVESPEC_CALLERGAP_KEEP_NAMES") == "1"


def _livespec_graph(conn: Any, project_id: int) -> tuple[nx.DiGraph, dict[int, dict]]:
    """The same cone `who_calls` walks: edges at or above the fan-out floor."""
    g = nx.DiGraph()
    meta: dict[int, dict] = {}
    for row in conn.execute(
        """SELECT s.id, s.name, s.qualified_name, s.start_line, f.path
           FROM symbol s JOIN file f ON f.id = s.file_id WHERE f.project_id = ?""",
        (project_id,),
    ):
        sid = int(row["id"])
        meta[sid] = {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "file_path": row["path"],
            "start_line": row["start_line"],
        }
        g.add_node(sid)
    for row in conn.execute(
        """SELECT e.src_symbol_id, e.dst_symbol_id FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ? AND e.weight >= 0.6""",
        (project_id,),
    ):
        g.add_edge(int(row["src_symbol_id"]), int(row["dst_symbol_id"]))
    return g, meta


async def measure(repo: Path, graph_path: Path | None) -> dict[str, Any]:
    row: dict[str, Any] = {}
    async with Client(mcp) as client:
        try:
            idx = (
                await client.call_tool("index_project", {"workspace": str(repo)})
            ).data
        except Exception as exc:
            return {"skipped": str(exc)[:200]}
        if idx.get("isError"):
            return {"skipped": idx.get("error", "index failed")}
        row["languages"] = idx.get("languages") or {}

    if graph_path is None:
        return {**row, "skipped": "no external graph"}

    from livespec_mcp.state import get_state

    st = get_state(str(repo))
    g, meta = _livespec_graph(st.conn, st.project_id)
    row["symbols"] = len(meta)
    row["livespec_edges"] = g.number_of_edges()

    ext = load_external_graph(graph_path)
    row["external_nodes"] = ext.node_count
    row["external_edges"] = ext.edge_count

    claims = build_claim_index(
        ext,
        (
            (sid, m["file_path"], int(m["start_line"] or 0), m["name"] or "")
            for sid, m in meta.items()
        ),
    )
    row["matched_nodes"] = len(claims.by_node)
    row["ambiguous_nodes"] = len(claims.ambiguous)

    agreed = 0
    external_only: list[tuple[str, int, int]] = []
    for source_id, outbound in ext.outbound.items():
        src = claims.symbol_for(source_id)
        if src is None:
            continue
        for relation, target_id in outbound:
            if relation not in EVIDENCE_RELATIONS:
                continue
            dst = claims.symbol_for(target_id)
            if dst is None or dst == src:
                continue
            if g.has_edge(src, dst):
                agreed += 1
            else:
                external_only.append((relation, src, dst))

    by_relation: dict[str, int] = {}
    unreachable = 0
    gained: dict[int, int] = {}
    for relation, src, dst in external_only:
        by_relation[relation] = by_relation.get(relation, 0) + 1
        gained[dst] = gained.get(dst, 0) + 1
        if not nx.has_path(g, src, dst):
            unreachable += 1

    row["agreed"] = agreed
    row["external_only"] = len(external_only)
    row["unreachable"] = unreachable
    row["by_relation"] = dict(sorted(by_relation.items(), key=lambda kv: -kv[1]))
    row["symbols_gaining_a_caller"] = len(gained)
    if KEEP_NAMES:
        row["top_gainers"] = [
            {"qualified_name": meta[sid]["qualified_name"], "external_callers": n}
            for sid, n in sorted(gained.items(), key=lambda kv: -kv[1])[:10]
        ]

    # One trip through the MCP wire, on the symbol that gains the most, so the
    # aggregate above is never the only thing that was checked.
    if gained:
        top = max(gained, key=lambda k: gained[k])
        async with Client(mcp) as client:
            out = (
                await client.call_tool(
                    "who_calls",
                    {
                        "qname": meta[top]["qualified_name"],
                        "workspace": str(repo),
                        "corroborate_with": str(graph_path),
                    },
                )
            ).data
        row["wire_check"] = {
            "livespec_callers": out.get("count"),
            "external_callers": len(out.get("external_callers") or []),
            "by_relation": (out.get("external_evidence") or {}).get("by_relation"),
        }
    return row


def main() -> int:
    args = [Path(a).resolve() for a in sys.argv[1:]]
    root = os.environ.get("LIVESPEC_CALLERGAP_ROOT")
    repos = args or (
        sorted(p for p in Path(root).iterdir() if (p / ".git").exists())
        if root
        else []
    )
    if not repos:
        print(__doc__)
        return 2

    python = _graphify_python()
    report: dict[str, Any] = {"repos": {}}
    for i, repo in enumerate(sorted(repos), start=1):
        label = repo.name if KEEP_NAMES else f"repo-{i:02d}"
        graph = build_graph(repo, python)
        row = asyncio.run(measure(repo, graph))
        report["repos"][label] = row
        print(f"{label}: {json.dumps(row)[:400]}", flush=True)

    rows = [r for r in report["repos"].values() if "external_only" in r]
    report["totals"] = {
        "repos": len(rows),
        "agreed": sum(r["agreed"] for r in rows),
        "external_only": sum(r["external_only"] for r in rows),
        "unreachable": sum(r["unreachable"] for r in rows),
    }
    print(json.dumps(report["totals"], indent=2))

    out = os.environ.get("LIVESPEC_CALLERGAP_OUT")
    if out:
        Path(out).write_text(json.dumps(report, indent=2))
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
