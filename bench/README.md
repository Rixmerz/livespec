# bench/

Performance baseline for livespec-mcp.

## Run

```bash
uv run python bench/run.py            # all repos: fastapi, requests, rich
uv run python bench/run.py --quick    # just requests (fast)
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

| Repo | LoC (py) | Why |
|------|----------|-----|
| requests | ~10k | tiny baseline |
| fastapi | ~25k | medium, idiomatic ASGI codebase |
| rich | ~30k | long methods, lots of decorators, edge cases |

None require Cython or odd build steps; all clone in seconds.

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
