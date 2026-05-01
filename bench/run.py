#!/usr/bin/env python3
"""Benchmark harness for livespec-mcp.

Clones (or reuses) a small set of real-world repos, runs `index_project`
cold + warm + partial-touch, then prints a JSON report. Designed to be
checked into CI to catch performance regressions across commits.

Usage:
    python bench/run.py                # default repos: fastapi, requests
    python bench/run.py --quick        # smaller subset, ~30s total
    python bench/run.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from livespec_mcp.config import Settings
from livespec_mcp.domain.indexer import index_project
from livespec_mcp.storage.db import connect

REPOS = {
    "fastapi": ("https://github.com/fastapi/fastapi.git", "0.118.0"),
    "requests": ("https://github.com/psf/requests.git", "v2.32.3"),
    "rich": ("https://github.com/Textualize/rich.git", "v13.9.4"),
}

QUICK_REPOS = ["requests"]


def _clone(repo: str, ref: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, repo, str(dest)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _bench_repo(workspace: Path) -> dict[str, Any]:
    # Clean any prior state
    state_dir = workspace / ".mcp-docs"
    if state_dir.exists():
        shutil.rmtree(state_dir)

    settings = Settings(
        workspace=workspace,
        state_dir=state_dir,
        db_path=state_dir / "docs.db",
        docs_dir=state_dir / "docs",
        models_dir=state_dir / "models",
    )
    settings.ensure_dirs()

    # Cold run
    conn = connect(settings.db_path)
    t0 = time.perf_counter()
    cold = index_project(settings, conn)
    cold_ms = (time.perf_counter() - t0) * 1000
    conn.close()

    # Warm run (no changes — should be fast)
    conn = connect(settings.db_path)
    t1 = time.perf_counter()
    warm = index_project(settings, conn)
    warm_ms = (time.perf_counter() - t1) * 1000
    conn.close()

    # Touch one file and re-index
    touch_target = next((p for p in workspace.rglob("*.py") if p.stat().st_size > 1000), None)
    partial: dict[str, Any] = {}
    if touch_target is not None:
        original = touch_target.read_text(encoding="utf-8", errors="replace")
        try:
            touch_target.write_text(original + "\n# bench touch\n")
            conn = connect(settings.db_path)
            t2 = time.perf_counter()
            partial_stats = index_project(settings, conn)
            partial_ms = (time.perf_counter() - t2) * 1000
            conn.close()
            partial = {
                "ms": round(partial_ms, 1),
                "files_changed": partial_stats.files_changed,
                "edges_total": partial_stats.edges_total,
            }
        finally:
            touch_target.write_text(original)

    db_size_mb = settings.db_path.stat().st_size / (1024 * 1024) if settings.db_path.exists() else 0

    # P2.4: memory footprint of NetworkX graph + PageRank.
    memory: dict[str, Any] = {}
    try:
        import psutil

        from livespec_mcp.domain.graph import load_graph, page_rank

        process = psutil.Process()
        rss_before_mb = process.memory_info().rss / (1024 * 1024)
        conn = connect(settings.db_path)
        # Need a project_id for load_graph
        project_id = conn.execute("SELECT id FROM project LIMIT 1").fetchone()
        if project_id is not None:
            view = load_graph(conn, int(project_id["id"]))
            rss_after_load_mb = process.memory_info().rss / (1024 * 1024)
            t = time.perf_counter()
            ranks = page_rank(view.g)
            pr_ms = (time.perf_counter() - t) * 1000
            rss_after_pr_mb = process.memory_info().rss / (1024 * 1024)
            memory = {
                "rss_before_mb": round(rss_before_mb, 1),
                "rss_after_load_mb": round(rss_after_load_mb, 1),
                "rss_after_pagerank_mb": round(rss_after_pr_mb, 1),
                "graph_nodes": view.g.number_of_nodes(),
                "graph_edges": view.g.number_of_edges(),
                "pagerank_ms": round(pr_ms, 1),
                "ranks_computed": len(ranks),
            }
        conn.close()
    except ImportError:
        memory = {"skipped": "psutil not installed"}

    return {
        "files": cold.files_total,
        "symbols": cold.symbols_total,
        "edges": cold.edges_total,
        "languages": cold.languages,
        "cold_ms": round(cold_ms, 1),
        "warm_ms": round(warm_ms, 1),
        "partial": partial,
        "db_mb": round(db_size_mb, 2),
        "loc_per_sec": (
            round(cold.symbols_total * 1000 / cold_ms, 0) if cold_ms > 0 else 0
        ),
        "memory": memory,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Subset of repos for fast iteration")
    parser.add_argument("--json", type=Path, default=None, help="Write the report to this file")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "livespec-bench")
    args = parser.parse_args()

    targets = QUICK_REPOS if args.quick else list(REPOS.keys())

    report: dict[str, Any] = {"results": {}}
    args.cache.mkdir(parents=True, exist_ok=True)

    for name in targets:
        url, ref = REPOS[name]
        repo_dir = args.cache / name
        print(f"[{name}] cloning {ref} ...", file=sys.stderr)
        try:
            _clone(url, ref, repo_dir)
        except subprocess.CalledProcessError as e:
            report["results"][name] = {"error": f"clone failed: {e}"}
            continue
        print(f"[{name}] benchmarking ...", file=sys.stderr)
        try:
            report["results"][name] = _bench_repo(repo_dir)
        except Exception as e:
            report["results"][name] = {"error": f"bench failed: {e!r}"}

    output = json.dumps(report, indent=2, sort_keys=True)
    print(output)
    if args.json:
        args.json.write_text(output, encoding="utf-8")
        print(f"\nWrote {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
