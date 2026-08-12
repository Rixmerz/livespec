#!/usr/bin/env python3
"""Measure what external-graph corroboration actually buys, across many repos.

`find_dead_code(corroborate_with=…)` and `find_orphan_tests(corroborate_with=…)`
drop candidates a second extractor still sees referenced. Two hand-checked repos
said the effect is real but uneven: a Java service lost 8 of 17 orphan tests to
corroboration, while livespec's own suite lost none, because its in-process
harness is a blind spot *both* extractors share.

Two data points is not a finding. This script turns that claim into a number by
running both tools with and without corroboration over a directory of repos and
reporting where the effect concentrates.

Repo paths come from the environment so private trees never land in this tree,
and the emitted report labels repos positionally (`repo-01`) rather than by
name — the same reason `dogfood_tool_value_audit.py` reads its workspaces from
env::

    LIVESPEC_CORROB_ROOT=/abs/dir/of/repos \\
    LIVESPEC_CORROB_OUT=/abs/out.json      \\
    uv run python scripts/dogfood_corroboration.py

Set `LIVESPEC_CORROB_KEEP_NAMES=1` to include real repo names locally (never
commit that output). Graphs are built with Graphify if `graphifyy` is
importable and no `graphify-out/graph.json` already exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastmcp import Client

from livespec_mcp.server import mcp

KEEP_NAMES = os.environ.get("LIVESPEC_CORROB_KEEP_NAMES") == "1"


def _graphify_python() -> str | None:
    """The interpreter that can `import graphify`, if any."""
    import shutil
    import subprocess

    if shutil.which("uv"):
        try:
            out = subprocess.run(
                ["uv", "tool", "run", "--from", "graphifyy", "python", "-c",
                 "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=120,
            )
            candidate = out.stdout.strip()
            if candidate and Path(candidate).exists():
                return candidate
        except (subprocess.SubprocessError, OSError):
            pass
    return None


def build_graph(repo: Path, python: str | None) -> Path | None:
    """Return a graph.json for `repo`, building one if needed."""
    existing = repo / "graphify-out" / "graph.json"
    if existing.is_file():
        return existing
    if python is None:
        return None
    import subprocess

    out = repo / "graphify-out" / "graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    code = f"""
import json
from pathlib import Path
from graphify.extract import collect_files, extract
from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.export import to_json
root = Path({str(repo)!r})
files = collect_files(root)
if not files:
    raise SystemExit(2)
res = extract(files, cache_root=root)
G = build_from_json(res, root=str(root), directed=True)
to_json(G, cluster(G), {str(out)!r})
print(json.dumps({{"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
                   "llm_in": res.get("input_tokens", 0)}}))
"""
    try:
        proc = subprocess.run(
            [python, "-c", code], capture_output=True, text=True, timeout=1800
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return out if out.is_file() and proc.returncode == 0 else None


async def _call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return (await client.call_tool(tool, args)).data
    except Exception as exc:
        return {"isError": True, "error": str(exc)[:200]}


async def measure(repo: Path, graph: Path | None) -> dict[str, Any]:
    row: dict[str, Any] = {"languages": {}}
    async with Client(mcp) as client:
        idx = await _call(client, "index_project", {"workspace": str(repo)})
        if idx.get("isError"):
            return {**row, "skipped": idx.get("error", "index failed")}
        row["languages"] = idx.get("languages") or {}
        row["symbols"] = idx.get("symbols_total")

        for tool, dropped_key in (
            ("find_dead_code", "dropped_as_referenced"),
            ("find_orphan_tests", "dropped_as_reaching_production"),
        ):
            base = await _call(
                client, tool, {"workspace": str(repo), "summary_only": True}
            )
            row[f"{tool}_before"] = base.get("count")
            if graph is None:
                continue
            after = await _call(
                client,
                tool,
                {
                    "workspace": str(repo),
                    "summary_only": True,
                    "corroborate_with": str(graph),
                },
            )
            if after.get("isError"):
                row[f"{tool}_error"] = after.get("error", "")[:160]
                continue
            report = after.get("corroboration") or {}
            row[f"{tool}_after"] = after.get("count")
            row[f"{tool}_matched"] = report.get("candidates_matched")
            row[f"{tool}_dropped"] = report.get(dropped_key)
            row[f"{tool}_relations"] = report.get("dropped_by_relation") or {}
    return row


def _reduction(row: dict[str, Any], tool: str) -> float | None:
    before, after = row.get(f"{tool}_before"), row.get(f"{tool}_after")
    if not before or after is None:
        return None
    return (before - after) / before


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"repos": len(rows)}
    for tool in ("find_dead_code", "find_orphan_tests"):
        reductions = [r for r in (_reduction(x, tool) for x in rows) if r is not None]
        helped = [r for r in reductions if r > 0]
        out[tool] = {
            "measured": len(reductions),
            "helped": len(helped),
            "no_effect": len(reductions) - len(helped),
            "median_reduction_where_it_helped": (
                round(statistics.median(helped), 3) if helped else None
            ),
            "max_reduction": round(max(reductions), 3) if reductions else None,
            "total_before": sum(x.get(f"{tool}_before") or 0 for x in rows),
            "total_after": sum(
                x.get(f"{tool}_after", x.get(f"{tool}_before")) or 0 for x in rows
            ),
        }
        relations: dict[str, int] = {}
        for x in rows:
            for rel, n in (x.get(f"{tool}_relations") or {}).items():
                relations[rel] = relations.get(rel, 0) + n
        out[tool]["dropped_by_relation"] = dict(
            sorted(relations.items(), key=lambda kv: -kv[1])
        )
    return out


async def main() -> int:
    root = os.environ.get("LIVESPEC_CORROB_ROOT")
    if not root:
        print("set LIVESPEC_CORROB_ROOT to a directory of repos", file=sys.stderr)
        return 2
    root_path = Path(root).expanduser().resolve()
    repos = sorted(
        d for d in root_path.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    if not repos:
        print(f"no repos under {root_path}", file=sys.stderr)
        return 2

    python = _graphify_python()
    if python is None:
        print("graphify not importable — measuring baselines only", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for i, repo in enumerate(repos, 1):
        label = repo.name if KEEP_NAMES else f"repo-{i:02d}"
        graph = build_graph(repo, python)
        print(f"[{i}/{len(repos)}] {label} graph={'yes' if graph else 'no'}",
              file=sys.stderr)
        row = await measure(repo, graph)
        rows.append({"repo": label, "has_graph": graph is not None, **row})

    report = {"summary": summarise(rows), "rows": rows}
    dest = os.environ.get("LIVESPEC_CORROB_OUT")
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if dest:
        Path(dest).write_text(text, encoding="utf-8")
        print(f"wrote {dest}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
