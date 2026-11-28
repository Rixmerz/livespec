# bench/

Performance baseline for livespec.

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

## Ecosystem benchmark (`ecosystem.py`)

livespec + FlowTrace (+ delta-cube) on two pinned real repositories: what the
index sees statically, what a traced test run adds, and how much an agent would
have to read with and without the tools.

```bash
uv run python bench/ecosystem.py --json bench/results-ecosystem.json   # both repos, ~6 min
uv run python bench/ecosystem.py --repo flask                          # one repo
uv run python bench/ecosystem.py --quick                               # click only, trimmed
```

Prerequisites (a missing one ends the run with a message): `git`, `uv`, a
`flowtrace-debugger` checkout (`--flowtrace` / `FLOWTRACE_DIR`, default
`/home/user/flowtrace-debugger`), network for the clones and the pinned test
requirements. delta-cube (`--delta-cube` / `DELTA_CUBE_DIR`, needs its `.venv`)
is optional; without it the block is recorded as skipped. Each repo gets a fresh
venv in a scratch directory (`--keep` / `--workdir` to inspect it); traces live
outside the checkout; `LIVESPEC_HOME` and `DCC_DATA_DIR` point into the scratch
directory so no machine-wide state is written. Proxy and TLS variables are
removed from the test runs (httpx and requests fail tests otherwise).
`--quick` = click only, one plain run instead of two, no cleaned-trace
comparison, no delta-cube. Deterministic apart from timings (two full runs
differ only in timings and in delta-cube's `payload_chars`, by the length of the
scratch path).

### Why click and flask

Probed on the same machine with the same flags. rich: one traced suite run had
written >5 GB in under two minutes and was stopped. httpx: 5 tests fail before
any tracing, 163 MB trace. requests: 72 s plain, 8 environment-sensitive
failures. click (8.1.8) and flask (3.1.0) run offline in ~1-2 s, trace to
84 / 46 MB at `FLOWTRACE_MAX_ARG_LENGTH=64`, and are full of dynamic dispatch:
`ParamType.convert` polymorphism, `callback=` hooks and `super()` chains in
click; decorators that replace methods, properties, `current_app` proxies and
view registries in flask. Both are pinned by tag and commit sha (the clone is
verified); flask's unpinned runtime dependencies are pinned in the script.

### Results (committed in `results-ecosystem.json`, one machine, single runs)

| | click | flask |
|---|---|---|
| files / symbols / call edges | 70 / 1,389 / 5,618 | 85 / 1,598 / 20,714 |
| index (cold) | 0.74 s | 0.85 s |
| `find_dead_code` default, static -> after traces | 5 -> 4 (3 `not_seen`, 1 `no_evidence`) | 16 -> 7 (3 `not_seen`, 4 `no_evidence`; 7 rescued) |
| `include_public=True` | same as default (no effect on Python) | same as default |
| `include_infrastructure=True` (extra) | 250 -> 125 (30 rescued, 39 `not_seen`) | 170 -> 38 (33 rescued, 19 `not_seen`) |
| tests plain / traced / factor | 0.90 s / 4.29 s / 4.8x (2.8x vs the first, cold, plain run) | 1.48 s / 3.74 s / 2.5x (1.8x) |
| pass/fail set under tracing | identical (628 passed) | identical (489 passed, 1 fails with and without tracing) |
| trace | 62.2 MB, 79,197 spans | 39.3 MB, 52,455 spans |
| events resolved | 100 % (all by `file`+`line`) | 100 % (all by `file`+`line`) |
| runtime edges observed / new / already static | 590 / 254 / 336 | 458 / 284 / 174 |
| spans that are tracer artifacts (enter-time) / parent already exited | 0 / 0 | 0 / 0 |
| ingest dry-run / apply, peak RSS | 0.8 s / 0.8 s, 105 MB | 0.6 s / 0.5 s, 102 MB |

The static candidate counts (`include_infrastructure` 250 / 170) differ from the
earlier committed run (262 / 204) because livespec itself moved between the two
runs, not because of tracing; compare the three columns below only within one
row of one table.

#### Before -> after the tracer fix (same livespec build, same pinned repos)

"before" = FlowTrace without the serializer guard / generator pause
(`8b1bbf1`) ingested by the current livespec; "after" = this branch. Single runs.

| | click before -> after | flask before -> after |
|---|---|---|
| spans that are serializer artifacts, enter-time (share of all spans) | 20,732 (27.8 % incl. exit-time heuristic) -> 0 (0.9 %, all `Parameter.__repr__` called by C-level `list.index` inside click's `sort_key`: a real call) | 9,037 (16.8 %) -> 0 |
| spans whose parent had already exited | 0 -> 0 | 1,116 -> 0 |
| spans in the trace | 112,888 -> 79,197 | 63,071 -> 52,455 |
| runtime edges observed / new | 722 / 398 -> 590 / 254 | 482 / 308 -> 458 / 284 |
| `include_infrastructure` after traces (rescued / `not_seen`) | 111 (25 / 25) -> 125 (30 / 39) | 40 (27 / 21) -> 38 (33 / 19) |
| known-wrong `augment_usage_errors -> Parameter.process_value` | present -> gone | |
| known-wrong `dispatch_request -> SecureCookieSession.__init__.on_update` | | present -> present (werkzeug `CallbackDict` callback; not the tracer) |
| overhead factor | 6.1x -> 4.8x | 3.0x -> 2.5x |
| tests pass/fail identical under tracing | yes -> yes | yes -> yes |

The consumer guard alone (current livespec, old FlowTrace traces): 20,732
(click) and 9,037 + 1,116 (flask) spans are reported as `skipped.
parent_not_yet_entered` / `parent_already_exited` and add no edge or hit, which
matches the script's own count of those orders exactly. What it cannot see
stays: exit-time artifacts (`repr(result)`, `str(exc)`; by order they are
indistinguishable from a call made as the function's last statement) and spans
nested under a discarded one, so with the old tracer click still has 139 runtime
edges into `__repr__`/`__str__`-style methods and 75 from generator functions.
Only the tracer fix removes those at the source.

Flask's one baseline failure (`test_bad_environ_raises_bad_request`: expects 400,
gets 404) is werkzeug 3.1.9 against flask 3.1.0, not tracing.

### What the numbers do not say (read before quoting them)

- **Tracer artifacts, fixed.** An earlier run of this benchmark found 27.8 %
  (click) and 16.8 % (flask) of spans were produced by FlowTrace's own
  serialisation (`repr()` / `str()` of traced classes while rendering args,
  results and errors) and 1,116 flask spans were parented to an already-exited
  generator span. FlowTrace now suppresses spans created by its own
  serialisation and no longer leaves a generator's span current after a `yield`;
  livespec additionally refuses order-impossible spans from older traces
  (`skipped`, `discarded_spans`). The script still measures all of this
  (`analyse_trace`) so a regression shows up as a non-zero count; its exit-time
  heuristic flags 751 click spans that are real (`list.index` calling `__repr__`).
- Even clean, an edge links the nearest **traced** ancestor, so a callback run
  by an untraced library (werkzeug's `CallbackDict`, Jinja) is attributed to
  whatever traced function was on the stack. Both are reviewed in the JSON (`source_review`,
  `wrong_edges_confirmed_in_source` = `known_wrong_edges`, each checked against
  the index: the generator one is gone, the `CallbackDict` one remains).
  `yield from sub()` still attributes `sub`'s body to whoever drives the outer
  generator.
- Rescue is real where the static graph is blind: click `_get_windows_console_stream`
  and flask `sansio.app._make_timedelta` (passed as `get_converter=`) are static
  dead-code candidates that are used; the trace removes them.
- `not_seen` means "this run did not execute it". Only the tests of the repo ran.

### Payload proxy

An agent session cannot run inside a script, so the script measures what an
agent would have to **read**: 7 tasks per repo ("who calls X", "what breaks if I
change Y", "is Z unused", "entry point", "what does V do"; 4 per repo need a
runtime-only edge). *By file*: every `.py` file under `src/` and `tests/` that
contains the identifier(s) a careful reader would grep, read whole, plus files
grep cannot find (the dispatcher of a runtime edge). *By tool*: the characters
returned by the minimal livespec call sequence, every call counted (here one call
per task). Tokens are chars/4, approximate. The tool answer is checked against
the source (`truth` in the JSON); the check is recall of the named true
callers/callees, plus a list of known-wrong edges that appear in the answer.

| | click | flask |
|---|---|---|
| by file (whole files) | 939,856 chars (~235k tok) | 751,272 chars (~188k tok) |
| by file, `src/` hits only | 874,409 | 603,961 |
| grep output only (cannot answer alone) | 5,875 | 7,662 |
| by tool, 7 calls | 10,266 chars (~2.6k tok) | 9,776 chars (~2.4k tok) |
| tool answer correct, with traces / static only | 7 / 7, 3 / 7 | 7 / 7, 3 / 7 |

This is a proxy, not an agent run. It leaves out: reasoning and wrong turns,
partial reads (offset/limit), whether an agent picks the right call, the cost of
the index and of producing a trace, tool-call overhead, and precision (two of
the 14 correct answers contained one known-wrong runtime edge before the fix
(click-3 `augment_usage_errors`, flask-6 `DebugFilesKeyError.__str__`); none does
now, and the two answers are 2,904 -> 2,511 and 920 -> 677 characters). Whole-file reads
are a pessimistic baseline; the grep-only row is the optimistic one.

delta-cube `hotspots` top 10 is recorded per repo (index ~1.1-1.3 s, hotspots
~0.5 s, ~1.9k-char payload) without interpretation.

## Not yet covered

- TS/Go/Rust real repos (need a fixture beyond Python)
- Memory footprint of the NetworkX graph (RSS sampling)
- Search latency at p50/p95
- Search latency at scale (FTS5)
