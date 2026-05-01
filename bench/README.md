# bench/

Performance baseline for livespec-mcp.

## Run

```bash
uv run python bench/run.py            # all repos: fastapi, requests, rich
uv run python bench/run.py --quick    # just requests (fast)
uv run python bench/run.py --large    # Django stress test (~2.5 min cold)
uv run python bench/run.py --json bench/results-latest.json
```

The harness clones each repo into `~/.cache/livespec-bench/` (re-used across
runs), then for each one measures:

- **cold_ms** — first `index_project` against an empty `.mcp-docs/`
- **warm_ms** — second call, no file changes (should be near-zero)
- **partial.ms** — touch one Python file then re-index
- **db_mb** — SQLite file size after indexing
- **loc_per_sec** — symbols processed per cold second (rough throughput)

## Why these repos

| Repo | LoC (py) | Symbols indexed | Why |
|------|----------|-----------------|-----|
| requests | ~10k | 745 | tiny baseline |
| fastapi | ~25k | ~5k | medium, idiomatic ASGI codebase |
| rich | ~30k | ~6k | long methods, lots of decorators, edge cases |
| django | ~250k | ~40k | **stress test** — 53× requests scale, 1M+ edges |

None require Cython or odd build steps; all clone in seconds (django takes
~10s on first clone via shallow tag).

## Django stress-test results (v0.6 baseline, 2026-05-01)

`bench/results-large.json` captures a single run on a 2024 MacBook Pro
(Apple Silicon). Numbers vary ±15% across runs but the shape is stable.

| Metric | Value | Note |
|---|---|---|
| files | 2,898 | 2,786 .py + 112 .js |
| symbols | 39,789 | post-extraction, deduped |
| edges | 1,048,273 | call graph |
| **cold_ms** | 147,991 | full extract — tree-sitter parse cost dominates |
| **warm_ms** | 861 | no file changes — content-hash short-circuit |
| **partial.ms** | 7,044 | one file touched — still re-resolves all refs (v0.7 candidate) |
| db_mb | 123 | proportional to symbols + edges + WAL |
| loc_per_sec | 269 | symbols/sec during cold extraction |
| pagerank_ms | 3,617 | NetworkX scipy backend |
| rss_after_load_mb | 592 | full graph + sym_meta in memory |
| rss_after_pagerank_mb | 609 | +17MB for the rank scores |

### Interpretation

- **Cold ingestion is parse-bound.** ~50ms/file. Acceptable as a one-off;
  watcher mode keeps it warm afterward.
- **Warm path is fast.** Sub-second when nothing changed — content-hash
  early exit works.
- **Partial reindex (7s) is the obvious hotspot.** When one file changes,
  `_resolve_refs` re-walks every `symbol_ref` row in the project (~1M for
  django). The result is idempotent (INSERT OR IGNORE) but the iteration
  is wasted. v0.7 candidate: filter refs to those whose `target_name`
  matches a name in the changed file — typical case is <100 candidates,
  not 1M.
- **Memory.** 600MB peak per workspace. Multi-tenant LRU=8 worst-case is
  4.8GB if every cached workspace is at django scale. v0.7+: LRU eviction
  on memory pressure.
- **Graph cache** (v0.6 P3) eliminates the repeated load-cost in the
  multi-tool agent flow. Without the cache, every `analyze_impact` /
  `get_call_graph` / `get_project_overview` call would rebuild the
  NetworkX object (~4s on django). With the cache, second-call cost is
  the cache lookup (~µs); invalidation is automatic on the next index run.

## Comparing across commits

The output is JSON, so commit the latest baseline as
`bench/results-baseline.json` and diff future runs against it. There's no
automatic regression gate yet — the suite is meant to surface deltas, not
fail builds. A simple eyeball check is enough until we have month-over-month
trend data.

## Not yet covered

- TS/Go/Rust real repos (need a fixture beyond Python)
- Memory footprint of the NetworkX graph (RSS sampling)
- Search latency at p50/p95
- Vector-lane benchmarks (depends on `[embeddings]` extras)
