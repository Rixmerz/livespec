# Agent Usage Data — v0.8 P2 battle-test

This file is the field log behind the v0.8 curation pass. It replaces
the opinion-based tier list (ROADMAP §6) with what real agent sessions
actually do across multiple unfamiliar codebases.

## How this gets filled

1. Pick a target codebase (Django subset, Next.js boilerplate, a large Rust monorepo
   subset, etc.). Clone it and pass `workspace=/abs/path` on every tool call
   (no `LIVESPEC_WORKSPACE` env).
2. Run an agent session: a real bug-fix, feature, refactor, or
   exploration task — not a synthetic benchmark. Let the agent call
   tools naturally.
3. The middleware (`src/livespec_mcp/instrumentation.py`) writes one
   JSONL line per call into `<workspace>/.mcp-docs/agent_log.jsonl`.
4. After 3-5 sessions per codebase, run:
   ```bash
   uv run python bench/agent_log_analyze.py \
       /path/to/codebase1 /path/to/codebase2 ... \
       --json bench/agent_usage.json
   ```
5. Update the **Findings** section below with the data.

The redaction step in the middleware rewrites absolute paths to
`<workspace>` so the aggregated JSON can leave the user's machine
without leaking home-directory layout.

## Target codebases (planned)

| Codebase | Language | Lines | Tasks |
|---|---|---:|---|
| Django subset | Python | ~250K | feature, bugfix, refactor |
| Next.js boilerplate | TypeScript | ~30K | feature, bugfix, refactor |
| a large Rust monorepo subset | Rust | ~500K | feature, bugfix, refactor |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |

Pick TBD slots in-session. One should be a JS/TS app of moderate
size; another should be a language under-represented in the others
(Go or Java).

## Findings

### Session log

| # | Date | Codebase | Task | Calls | Session id |
|---|---|---|---|---:|---|
| 01 | 2026-05-01 | the workflow runner (Python, 130 files / 1173 syms / 4174 edges) | exploration: trace `proxy_tools_search` dispatch flow from MCP entry | 11 | `dfc19fd1-e97f-4501-9315-b8873eafe785` |
| 02 | 2026-05-01 | livespec-mcp itself (Python+8 langs, 84 files / 495 syms / 742 edges) | refactor: identify dead-code / refactor opportunities post-bug-fixes; also validate the 3 fixes landed in real signal | 11 (this session, 22 cumulative) | post-`bc8ba1d` |
| 03 | 2026-05-01 | demo-app (Python, 4 files / 23 syms / 26 edges, 6 RFs / 6 linked) | RF flow: validate `get_requirement_implementation` README-lead answer + `audit_coverage` + `propose_requirements_from_codebase` against an RF-active codebase | 7 (40 cumulative) | post-`44a0dc4` |

Caveats: n=1 session, exploratory task only (no refactor/bugfix
yet). Treat all findings below as **first-pulse signal**, not
conclusions. Refactor + bugfix profiles will exercise different
tool subsets (`who_calls`, `analyze_impact`, `audit_coverage`).

### Tool ranking (data-driven)

| tool | calls | errors | p50 ms | p95 ms | p50 chars | max chars |
|---|---:|---:|---:|---:|---:|---:|
| `who_does_this_call` | 3 | 0 | 0 | 1 | 3108 | 9514 |
| `find_symbol` | 2 | 0 | 2 | 2 | 698 | 698 |
| `quick_orient` | 2 | 0 | 47 | 47 | 3106 | 3106 |
| `get_symbol_source` | 2 | 0 | 1 | 1 | 3745 | 3745 |
| `index_project` | 1 | 0 | 55 | 55 | 553 | 553 |
| `get_project_overview` | 1 | 0 | 78 | 78 | 9171 | 9171 |

6 distinct tools used out of 39 wire tools. **33 silent.**

### Common follow-up patterns

| from | to | count |
|---|---|---:|
| `find_symbol` | `quick_orient` | 2 |
| `index_project` | `get_project_overview` | 1 |
| `get_project_overview` | `find_symbol` | 1 |
| `quick_orient` | `who_does_this_call` | 1 |
| `quick_orient` | `get_symbol_source` | 1 |
| `who_does_this_call` | `get_symbol_source` | 1 |
| `get_symbol_source` | `who_does_this_call` | 1 |
| `who_does_this_call` | `who_does_this_call` | 1 |

The two ubiquitous patterns:

1. **`find_symbol → quick_orient`** (2/2 occurrences). Validates the
   v0.8 P0 hypothesis that an agent's *first contact* with an
   unfamiliar symbol wants the composite, not the metadata-only
   `find_symbol` result.
2. **`index_project → get_project_overview`** as the standard cold
   open. After that, the agent jumps to `find_symbol` or
   `quick_orient` for a specific entry point.

### Tier classification implications (n=1 caveat)

**Validated tier-1 (used)**: `index_project`, `get_project_overview`,
`find_symbol`, `quick_orient`, `who_does_this_call`,
`get_symbol_source`. All 4 P0 quick wins (`quick_orient`,
`who_calls`, `who_does_this_call`, `get_symbol_source`) appeared OR
were stylistically substitutable in the flow — none felt redundant.

**Not exercised (need refactor/bugfix profile)**: `who_calls`,
`analyze_impact`, `git_diff_impact`, `audit_coverage`,
`find_dead_code`, `find_orphan_tests`, `find_endpoints`,
`get_symbol_info`. These are reasonable to expect on a
*"what breaks if I change X"* task, not on an *"explain how X works"*
task. **Need session 02 with a refactor target before drop/keep
decisions.**

**Silent and likely tier-4 candidates** (still n=1, but priors
match): `list_files`, `start_watcher`, `stop_watcher`,
`watcher_status`, `rebuild_chunks`, `get_index_status`. The first
five match the ROADMAP §2 tier-4 list; `get_index_status` data also
suggests demotion — agent never queries it mid-session, so the
resource (`project://index/status`, paritetic per v0.8 P3b prep) is
sufficient.

**Silent RF tools** (`list_requirements`,
`get_requirement_implementation`, `propose_requirements_from_codebase`,
`audit_coverage`, RF mutation tools): expected — the workflow runner has only 1 RF
linked to 1 symbol and the task didn't touch RF flow. **Need a
session against `demo-app` (which has 4 files with `@rf:`
docstrings) or another RF-active repo before classifying.** Do NOT
demote them on this session alone — the README leads with
"¿Qué código implementa el RF-042?", and dropping RF tools without
testing the RF profile would be exactly the bias ROADMAP §6 warned
against.

### Latency / payload outliers

- `get_project_overview`: 78ms / 9171 chars on a 1173-symbol Python
  repo. Acceptable. Larger repos (a large Rust monorepo, Django) need re-measurement.
- All quick wins under 50ms p95. Solid.
- No timeouts, no errors.

### Surprises and livespec UX gaps

These are **bugs / UX gaps in livespec itself** surfaced by the
session — distinct from curation decisions:

1. **Edge resolver same-name imprecision** (HIGH SIGNAL).
   `who_does_this_call(embed_cache.search, depth=1)` returned 7
   callees, of which ~4 are false positives — multiple symbols with
   the same short name (`list_tools` x3 in different modules,
   `_cosine` x2) all matched against a single call site that
   resolves to one of them. Same pollution showed up in
   `quick_orient(execute_mcp_tool).top_callees` — 5 different `.get`
   methods listed when the body actually only calls
   `internal_proxy.get`. **Action:** review `_resolve_refs` in
   `domain/indexer.py` for short-name fan-out; consider tightening
   to require scope match when multiple candidates exist.

2. **Entry-point flagging missing in `quick_orient`**.
   `proxy_tools_search` is decorated with `@mcp.tool` (framework
   entry point). `quick_orient` reports `callers_count: 0` /
   `top_callers: []`, which is technically correct but misleading —
   an agent reading "0 callers" assumes dead code or low-importance
   leaf. The matcher already detects `mcp.tool` /
   `_ENTRY_POINT_DECORATOR_LASTSEG` for `find_endpoints` /
   infrastructure filtering. **Action:** propagate that into
   `quick_orient` output as `is_entry_point: true` + a
   `framework_decorator: "mcp.tool"` field.

3. **`get_project_overview` top_symbols dominated by structural
   patterns**. Top results in the workflow runner were `.get` (4 different modules),
   `add_parser` (6 CLI subcommands), `run` (5 CLI subcommands).
   PageRank correctly identifies these as high-centrality, but the
   agent gets near-zero "what is this codebase about" signal from
   that list. The `_is_infrastructure` heuristic catches dunders /
   1-line wrappers / DI helpers but misses *repeated structural
   names across modules*. **Action:** add a "name appears in ≥N
   files with same short name" filter as opt-in (default off, or
   only when N is high enough to be confident).

4. **`get_index_status` never called** — confirms P3b prep
   direction. Once 2-3 more sessions corroborate, drop the tool
   wrapper, keep the resource.

5. **`who_calls` silent in exploration profile** — expected. Need to
   re-run on a refactor task to verify it earns its tier-1 slot.

### Decisions taken from session 01

- ✅ KEEP all 4 P0 quick wins. `quick_orient` and `get_symbol_source`
  earned their place; `who_does_this_call` was the most-called tool
  of the session; `who_calls` deferred for refactor session.
- ✅ KEEP `find_symbol`, `index_project`, `get_project_overview`,
  `who_does_this_call` in tier-1.
- ⚠️  Need session 02 (refactor) and 03 (RF flow) before final
  curation. RF tools and aggregator tools (`audit_coverage`,
  `find_dead_code`, etc.) cannot be classified on n=1 exploratory.
- 🐛 Filed 3 livespec bugs/UX gaps (edge resolver fan-out,
  entry-point flagging, top_symbols structural noise) — these are
  **higher leverage than tool curation** for v0.8 P3 main pass
  ordering. Fix the resolver before claiming the data is clean.
- 📌 `list_files` + watcher trio + `rebuild_chunks` silent on first
  session — priors strongly support drop, but wait for session 02
  to confirm.

### Next sessions planned

| # | Codebase | Task profile | Goal |
|---|---|---|---|
| 04 | Django subset (TBD) | bugfix: trace a known issue | scale check, larger codebase |
| 05 | TS/JS app (TBD) | feature: add an endpoint | language coverage |

---

## Session 02 — livespec-mcp refactor profile (2026-05-01)

### Aggregate (sessions 01 + 02 combined)

| tool | calls | errors | p50 ms | p95 ms | max chars |
|---|---:|---:|---:|---:|---:|
| `who_calls` | 5 | 0 | 1 | 8 | 7194 |
| `quick_orient` | 4 | 0 | 8 | 60 | 3298 |
| `index_project` | 3 | 0 | 352 | 400 | 764 |
| `who_does_this_call` | 3 | 0 | 0 | 1 | 9514 |
| `get_project_overview` | 2 | 0 | 74 | 74 | 10462 |
| `get_index_status` | 2 | 0 | 1 | 1 | 788 |
| `find_dead_code` | 2 | 0 | 2 | 2 | 6624 |
| `find_symbol` | 2 | 0 | 2 | 2 | 698 |
| `get_symbol_source` | 3 | 0 | 1 | 1 | 3745 |
| `audit_coverage` | 1 | 0 | 1 | 1 | 420 |
| `analyze_impact` | 1 | 0 | 1 | 1 | 5523 |
| `git_diff_impact` | 1 | 0 | 34 | 34 | 3125 |

10 distinct tools used out of 39 across 22 calls / 3 sessions.

### Validation of bug fixes #1, #2, #3 in production

- **Bug #1 (resolver fan-out)**: edges count on livespec-mcp dropped
  from 969 → 742 after re-index with the fix (−227, ~23% reduction).
  All `who_calls` / `who_does_this_call` results in this session
  showed clean cones — no more `.get` x4 modules pollution. Resolver
  test (`test_same_name_fanout_prefers_same_file`) green in CI.
- **Bug #2 (entry-point flag)**: `quick_orient(register.index_project)`
  returned `is_entry_point: true, framework_decorators: ["mcp.tool"]`
  and `callers_count: 0` — agent sees "0 callers BUT entry point",
  no longer ambiguous. `quick_orient(compute_index_status)`
  (helper, not decorated) returns `is_entry_point: false` — correct.
- **Bug #3 (structural noise)**: `get_project_overview` top_symbols
  now leads with `parse_annotations`, `connect`, `Settings`,
  `mcp_error`, `extract` — actual semantic anchors of livespec.
  `structural_patterns_filtered` returned
  `["Greeter", "__init__", "greet", "helper", "list_tools", "main",
  "register", "run", "topLevelOne", "top_level_one"]` — exactly the
  noise (test fixtures + `register` outers + multi-module `__init__`).

The fixes are real, end-to-end. Subsequent sessions can trust the
call graph signal.

### NEW bugs surfaced in session 02 (`find_dead_code` false positives)

`find_dead_code` over livespec-mcp itself returned 18 candidates,
but 9 of them are NOT dead — they're entry points the detector
misses. This is **higher-impact than the previous 3 bugs because
it directly contradicts the tool's contract** ("anything in the
result is unreachable from in-project callers AND not implementing
any RF AND not exposed publicly"). Concrete misses:

4. **`if __name__ == "__main__":` block not recognized as entry
   point.** Flagged: `bench.run.main`, `bench.agent_log_analyze.main`,
   `src.livespec_mcp.server.main`. All three are CLI entry points.
   Fix: when a function is named `main` AND its module ends with a
   `__main__` guard call to it, treat as entry point. Or: detect any
   function name appearing as the body of `if __name__ == "__main__":`
   in the same file.

5. **Functions stored in tuples / lists not detected as referenced.**
   Flagged: `storage.db._m001_drop_dead_tables` through
   `_m007_visibility` (6 of them). They're called via the `MIGRATIONS`
   list of `(version, name, fn)` tuples — `fn` is a function reference
   the static analyzer doesn't follow. Fix: when a name appears as a
   bare attribute reference (not a call) in a list/tuple/dict literal,
   add a "referenced" edge of weight 0.6 (lower than calls but enough
   to keep dead-code suppressed). Same pattern would catch decorators
   stored in tables, plugin registries, etc.

6. **Framework middleware lifecycle hooks not flagged as entry
   points.** Flagged: `instrumentation.AgentLogMiddleware.on_call_tool`,
   plus the class itself. FastMCP invokes these via duck-typing, no
   call site in the user's code. Fix: extend
   `_ENTRY_POINT_DECORATOR_LASTSEG` to include "method names commonly
   used by middleware frameworks" (`on_call_tool`, `on_request`,
   `on_event`, `dispatch`) OR (cleaner) detect the pattern of a class
   being passed to `add_middleware()` / similar registration call.

7. **`suggested_tests` in `git_diff_impact` includes test fixtures.**
   `git_diff_impact(HEAD~3..HEAD)` listed
   `tests/fixtures/python/same_name_fanout/embed_cache.py` in
   `suggested_tests` — that's a fixture file, not a test. Fix:
   filter `tests/` matches to only files named `test_*.py` /
   `*_test.py`, exclude fixture subdirs (`tests/fixtures/`,
   `tests/data/`).

These 4 are filed for v0.8 P3 main pass. None blocks further sessions
— they're contract-level UX bugs in `find_dead_code` and
`git_diff_impact`, not signal pollution like #1-3 were.

### Decisions taken from session 02

- ✅ Bug fixes #1/#2/#3 confirmed working in production. Lock them in.
- ⚠️  `find_dead_code` accuracy on livespec-mcp itself is poor (9/18
  false positives). Do NOT use it as a refactor primary; prefer
  manual review backed by `who_calls`. Future fix lands as bug #5.
- ✅ `who_calls`, `quick_orient`, `analyze_impact` all show clean
  cones post-resolver-fix. They are the **trustable** refactor tools
  right now.
- 📌 `audit_coverage` returned `modules_truly_orphan: 84` for
  livespec-mcp — expected (livespec is meant to be USED on
  RF-active codebases, not necessarily dogfooded on its own code).
  This is a **design data point**: a project with 0 RFs still gets
  meaningful answers from livespec because the code-intel layer is
  RF-independent. RF tier-1 placement is justified, but RF tools
  must NOT error out / refuse on RF-empty repos.

### Updated tier signal (n=2 sessions, mixed profiles)

**Used in ≥1 session AND likely tier-1 by usage**:
`index_project`, `get_project_overview`, `find_symbol`,
`quick_orient`, `who_calls`, `who_does_this_call`,
`get_symbol_source`, `analyze_impact`, `git_diff_impact`,
`find_dead_code`, `audit_coverage`. **11/39.**

**Silent across both sessions** (priors say drop, still need n=3+):
`list_files`, `start_watcher`, `stop_watcher`, `watcher_status`,
`rebuild_chunks`, `get_call_graph` (subsumed by who_calls + 
who_does_this_call composition), `get_symbol_info` (subsumed by
quick_orient + get_symbol_source).

**Untouched RF tools** (need session 03 against RF-active repo):
`list_requirements`, `get_requirement_implementation`,
`propose_requirements_from_codebase`, `scan_rf_annotations`,
`scan_docstrings_for_rf_hints`, `link_rf_symbol`,
`bulk_link_rf_symbols`, `link_rf_dependency`, `unlink_rf_dependency`,
`get_rf_dependency_graph`, `create_requirement`, `update_requirement`,
`delete_requirement`, `import_requirements_from_markdown`.

`get_index_status` was called 2x — but both as quick orientation
right after `index_project`, which is the purview of the
`project://index/status` resource. **Tool-wrapper drop case
strengthened**, do it in P3 main pass.

## Methodology notes

- **`result_cited_in_final_answer`** is NOT recorded by the
  middleware. It's a post-hoc annotation: read the agent's final
  text output, look for qnames or RF ids that match the tool result,
  flag those calls as cited. A tool whose results never appear in
  the final answer is suspect — it's noise, not signal.
- **Sessions** are bucketed by `session_id` (FastMCP-assigned).
  The follow-up pair analysis only counts pairs where both calls
  share a session — cross-session sequences are noise.
- **Counts are agent calls, not unique calls.** A tool called 50
  times in one session by a flailing agent looks the same as a tool
  called 50 times across 50 different sessions. Sample size matters;
  prefer raw count + sessions-with-call.

---

## Session 03 — demo-app RF flow (2026-05-01)

### Aggregate (sessions 01 + 02 + 03 combined)

40 calls, 3 sessions, 3 distinct workspaces, 15 distinct tools used
out of 39.

| tool | calls | errors | sessions | category |
|---|---:|---:|---:|---|
| `quick_orient` | 6 | 0 | 2 | code intel (P0) |
| `index_project` | 5 | 0 | 3 | code intel |
| `who_calls` | 5 | 0 | 1 | code intel (P0) |
| `get_project_overview` | 4 | 0 | 3 | code intel |
| `get_symbol_source` | 3 | 0 | 2 | code intel (P0) |
| `who_does_this_call` | 3 | 0 | 2 | code intel (P0) |
| `get_requirement_implementation` | 2 | 0 | 1 | RF |
| `audit_coverage` | 2 | 0 | 2 | RF |
| `get_index_status` | 2 | 0 | 2 | code intel |
| `find_dead_code` | 2 | 0 | 1 | code intel |
| `find_symbol` | 2 | 0 | 2 | code intel |
| `list_requirements` | 1 | 0 | 1 | RF |
| `propose_requirements_from_codebase` | 1 | 0 | 1 | RF |
| `analyze_impact` | 1 | 0 | 1 | code intel |
| `git_diff_impact` | 1 | 0 | 1 | code intel |

**24 silent tools** (none of: `bulk_link_rf_symbols`,
`create_requirement`, `delete_requirement`, `update_requirement`,
`link_rf_symbol`, `link_rf_dependency`, `unlink_rf_dependency`,
`get_rf_dependency_graph`, `import_requirements_from_markdown`,
`scan_rf_annotations`, `scan_docstrings_for_rf_hints`,
`get_call_graph`, `get_symbol_info`, `list_files`, `start_watcher`,
`stop_watcher`, `watcher_status`, `rebuild_chunks`,
`export_documentation`, `generate_docs`, `list_docs`, `search`,
`bulk_link_rf_symbols`).

### RF tools — validation against an RF-active codebase

`get_requirement_implementation("RF-001")` returned the linked
implementation in 1 round-trip, with confidence=1.0 and
source="annotation". The README's lead question — "¿Qué código
implementa el RF-042?" — works as advertised.

`list_requirements` returned all 6 RFs with module + priority +
status + link_count. Useful for orienting at the start of an
RF-active session.

`audit_coverage` worked but exposed two issues (see #8, #9 below).

`propose_requirements_from_codebase(skip_already_covered=False)`
returned 3 proposals — 2 useful (Store, Api groupings), 1 noise
(test module proposal, see #10 below).

**RF tier-1 placement validated by data.** `get_requirement_implementation`
+ `list_requirements` + `audit_coverage` + `propose_requirements_from_codebase`
all answered real questions an agent on an RF-active repo would ask.
Mutation tools (`link_rf_symbol`, `create_requirement`, etc.) still
silent — those are human-ceremony tools per the original tier-vision
in CLAUDE.md, which the data corroborates.

### NEW bugs surfaced in session 03

8. **`audit_coverage` should auto-exclude `__init__.py` from
   `modules_truly_orphan`.** `shortener/__init__.py` flagged as
   truly orphan, but `__init__.py` is never the right place to put
   `@rf:` annotations (no-op imports / package marker). Fix: filter
   modules whose basename is `__init__.py` from
   `modules_truly_orphan` and `modules_without_rf`. Same for Java
   `package-info.java`, Rust `mod.rs`, etc.

9. **`audit_coverage` doesn't surface RF test coverage as a positive
   signal.** Test files (`tests/test_shortener.py`) flagged as
   truly orphan even though they exercise the RF-linked symbols.
   The schema already supports `relation: tests` in `rf_symbol`. Fix:
   either (a) auto-link test files to the RFs of the symbols they
   call (matcher extension), or (b) add a `tests_per_rf` field to
   `audit_coverage` output that counts test edges as a separate
   coverage signal. Option (a) is the longer fix; (b) is the cheap
   immediate one.

10. **`propose_requirements_from_codebase` doesn't filter test
    modules.** Returned a `RF-009 "Test Shortener"` proposal grouping
    the 5 test functions as a "feature". Tests aren't a feature
    per se. Fix: skip module groups whose path is under `tests/` or
    `__tests__/` (mirror the find_dead_code skip-list).

These are all low-priority compared to bugs #1-7 — the RF flow's
hot path (`get_requirement_implementation`, `list_requirements`)
is clean, and the noise is on the periphery (audit edge cases,
proposal noise). Queue for v0.8 P3 or v0.9.

### Decisions taken from session 03

- ✅ **RF tools tier-1 placement is data-validated.**
  `get_requirement_implementation`, `list_requirements`,
  `audit_coverage`, `propose_requirements_from_codebase` answered
  real questions in the only repo profile (RF-active) where they
  should be exercised.
- ✅ **RF mutation tools (link, bulk_link, create/update/delete RF,
  scan_*) belong in plugin tier per original CLAUDE.md vision.**
  They were silent in the brownfield-discovery flow (an agent that
  *queries* RFs, not *manages* them — which is the human role).
- ⚠️  3 RF UX gaps (audit_coverage edge cases, proposal noise).
  Lower priority than bugs #1-7 — none affect the hot path.
- ✅ **livespec gracefully handles 0-RF repos** (session 02 had
  `modules_truly_orphan: 84` for livespec itself, no errors). RF
  tools were silent, not erroring. Confirms RF is a differentiator,
  not a precondition.

---

## Session 04 — Django bugfix profile (2026-05-01)

Workspace: `~/.cache/livespec-bench/django` (Django 5.1.4, 2898 files /
**39,789 symbols** / 465,179 edges). Session id
`1661cf99-d8f8-433d-a3b2-2e1dc8fa9f1e`. Task: trace
`AuthenticationMiddleware.process_request` blast radius for a
hypothetical "slow login on custom auth backends" bugfix.

### Aggregate (sessions 01 + 02 + 03 + 04 combined)

65 calls across 4 sessions / 4 workspaces / 4 profiles. **16 distinct
tools used** out of 17 default. The single silent tool is
`find_orphan_tests` — flagging it for closer evaluation in a future
profile that exercises test coverage explicitly.

| tool | calls | sessions | category |
|---|---:|---:|---|
| `index_project` | 10 | 4 | code intel |
| `quick_orient` | 8 | 3 | code intel (P0) |
| `who_calls` | 7 | 2 | code intel (P0) |
| `find_dead_code` | 6 | 2 | code intel |
| `get_project_overview` | 5 | 4 | code intel |
| `find_symbol` | 4 | 3 | code intel |
| `who_does_this_call` | 4 | 3 | code intel (P0) |
| `audit_coverage` | 4 | 3 | RF agentic |
| `get_symbol_source` | 4 | 3 | code intel (P0) |
| `analyze_impact` | 3 | 2 | code intel |
| `find_endpoints` | 2 | 1 | code intel |
| `get_index_status` | 2 | 2 | code intel (deprecated v0.8) |
| `git_diff_impact` | 2 | 1 | code intel |
| `get_requirement_implementation` | 2 | 1 | RF agentic |
| `list_requirements` | 1 | 1 | RF agentic |
| `propose_requirements_from_codebase` | 1 | 1 | RF agentic |
| `find_orphan_tests` | 0 | 0 | code intel — silent |

### Performance at scale (Django, 40K symbols)

- `index_project` cold: ~96.9s (first run, full extract). Within the
  expected ~25-100s range for a 2898-file Python repo per the README
  perf table.
- `quick_orient`: 47ms p50, ~2.8s p95. The p95 is the FIRST call after
  index — graph cache cold. Subsequent calls drop to <50ms.
- `find_dead_code(summary_only=True)`: 61ms p50. Reasonable.
- `who_calls(max_depth=1)`: 1-8ms. Fast even on Django.
- `get_project_overview`: 74ms p50, 3.9s p95. The p95 is again the
  cold-cache call — drops back to <100ms warm.

### NEW bugs surfaced in session 04

12. **`who_calls` / `who_does_this_call` overflow at scale**.
    `who_calls('BaseBackend.authenticate', max_depth=2)` returned 102KB
    (400 callers across 71 files). The aggregator-tool pagination
    contract from v0.7 B3 (`limit`/`cursor`/`summary_only`) does NOT
    extend to these slim agentic tools. On Django the agent had to
    drop to `max_depth=1` to get a usable response. Fix: extend
    pagination to `who_calls`, `who_does_this_call`, and
    `analyze_impact`.

13. **`analyze_impact` overflow at depth>1**.
    `analyze_impact('ModelBackend.authenticate', max_depth=3)`
    returned 332KB / 664 callers / 848 calls_into / 134 distinct
    files. Same root cause as #12. Pagination must include both
    `impacted_callers` and `calls_into` arrays.

14. **`analyze_impact.calls_into` inflation from short-name fan-out**.
    At `max_depth=1` on `ModelBackend.authenticate`, `calls_into`
    returned ~70 symbols. The actual body calls only ~10 things. The
    rest are short-name fan-outs: `obj.get(...)` resolves to every
    `get` method in the codebase (cache backends, view classes,
    QuerySet, etc.); same for `.get_by_natural_key`,
    `.check_password`, `.set_password`. Without type inference the
    static analyzer cannot disambiguate `obj.x()` to a single class.
    Mitigation: filter weight<0.6 edges out of forward-cone traversals
    by default (drop the ambiguous fan-out from the response, keep
    them in the DB).

15. **`find_endpoints(framework='django')` misses class-based views
    with mixins**. Returned 20 endpoints, all decorator-based
    (`@login_required`, `@permission_required`, `@staff_member_required`).
    Django's class-based views protected by `LoginRequiredMixin` /
    `PermissionRequiredMixin` are NOT detected. Fix:
    `_ENTRY_POINT_DECORATOR_LASTSEG` is decorator-only; for Django,
    add a parallel detection of classes inheriting from named mixins.

16. **`find_dead_code` over-reports on Django** — 824 dead candidates
    (801 in `django/`). Many are class-based views referenced only
    via `path(..., view=MyView.as_view())` in `urls.py`. The static
    analyzer doesn't recognize URL routing as a call site. Fix: scan
    `urls.py` files for `path(...)` / `re_path(...)` / `include(...)`
    and treat their callable arguments as entry-point references.
    Also: classes inheriting from framework mixins
    (`LoginRequiredMixin`, `PermissionRequiredMixin`,
    `MiddlewareMixin`, etc.) have implicit lifecycle hooks called by
    the framework.

17. **`quick_orient.top_callers` polluted by short-name fan-out
    across cross-file methods**.
    `quick_orient('AuthenticationMiddleware.process_request')` and
    `quick_orient('RemoteUserMiddleware.process_request')` returned
    the same 5 top callers (`make_middleware_decorator`,
    `_check_bad_or_missing_cookie`, etc.). The resolver matches every
    `process_request` short-name across the codebase. Same root cause
    as #14. Mitigation: filter weight<0.6 edges in the displayed
    `top_callers`/`top_callees`, keep total count honest.

These five bugs (#12-16) are all v0.9 candidates. #17 overlaps with
#14. None block continued sessions; they shape the priority list for
the next release.

### Decisions taken from session 04

- ✅ **Default surface holds at scale.** `quick_orient`,
  `who_calls(max_depth=1)`, `find_dead_code(summary_only=True)`,
  `get_project_overview`, `audit_coverage(summary_only=True)`,
  `find_symbol`, `get_symbol_source` all returned usable signal on
  39K symbols.
- ⚠️  **Pagination contract must extend** to `who_calls`,
  `who_does_this_call`, `analyze_impact`. Three overflow cases in one
  session, all on the slim agentic tools.
- ⚠️  **Resolver fan-out at depth>1** is the same bug as v0.8 P2 #1,
  but in the *graph traversal* layer instead of the resolver layer.
  Same-file disambiguation closed the producer side; the consumer side
  (forward/backward cones) still surfaces ambiguous edges. Filter by
  weight in the traversal default.
- ⚠️  **Django/framework-aware entry-point detection** needs URL
  routing + mixin class detection. Without it, `find_dead_code` and
  `find_endpoints` are inaccurate on the most common Python web
  framework on the planet.
- 📌 **Targeted resolver walk** (v0.9 P0) was active during the cold
  index — partial-reindex behavior on Django still TBD because we
  haven't touched files yet. A follow-up bench would close the loop
  on the 7s → 1s claim.
- 📌 **Sessions 01-03 caveat is largely cleared.** n=4 sessions /
  4 profiles is enough to lock the v0.8 tier list. Session 05 (TS/JS
  feature) remains valuable for language coverage.

---

## Session 05 — Deno Fresh feature profile (2026-05-01)

Workspace: `~/my_projects/SpeedRunners-landing` (Deno Fresh app, 217
files / **2532 symbols** / 16,525 edges across TypeScript + TSX +
JavaScript). Session id `0608d774-2501-4e7e-955c-b1de7b92a30b`. Task:
trace `apiFetch` (the central HTTP helper) and assess refactor scope.

### Aggregate (sessions 01 + 02 + 03 + 04 + 05 = 74 calls / 5 profiles)

5/5 profiles covered: exploration (the workflow runner), refactor (livespec-mcp), RF
flow (demo-app), Django-bugfix (Django), TS feature
(SpeedRunners-landing). Single silent default-tier tool remains:
`find_orphan_tests`.

### Performance at scale (Deno Fresh, 2532 symbols)

- `index_project` cold: ~6s (TSX/TS/JS extractor).
- `quick_orient` <50ms (warm cache).
- `find_dead_code(summary_only=True, include_non_python=True)`: ~80ms.
- `analyze_impact(summary_only=True, max_depth=2)`: ~10ms.

### Confirmed working on TS

- `find_symbol`: matches across `.ts`/`.tsx`/`.js` separator-agnostic.
- `quick_orient`: clean signal — `apiFetch` reported 11 callers / 2
  callees with `apiPost`/`apiPatch`/`apiDelete`/`apiGet` as top-5.
- `who_calls(max_depth=2)`: paginated correctly (count=27,
  next_cursor=10) — both Fresh islands and their event handlers
  surfaced as expected callers of `apiPost`.
- `get_symbol_source`: full TS body extracted with no escaping issues.
- `analyze_impact(summary_only=True)`: 73 impacted_callers / 4
  calls_into. Counts exact under summary mode.
- `audit_coverage(summary_only=True)`: gracefully handles 0-RF TS
  repo (217 modules orphan, no errors).

### NEW bugs surfaced in session 05

18. **`get_project_overview.top_symbols` polluted by bundle output**.
    Top 18 of 20 symbols on a Fresh app live in
    `_fresh/client/assets/echarts-theme-*.js`,
    `_fresh/client/assets/fresh-island__*.js`, etc. — these are
    minified bundler output, not source code. Real source-level
    centerpiece (`lib.api.apiFetch`) shows up at rank 4 buried under
    bundler noise. Fix: skip top-level dirs `_fresh/`, `dist/`,
    `build/`, `.next/`, `out/` from `top_symbols` by default.
    Detect minified files (single-line content with mangled
    one-letter identifiers) as a fallback.

19. **`find_dead_code` over-reports on Fresh apps**. With
    `include_non_python=True` (required for TS), 974 candidates —
    630 in `_fresh/` (bundler output again, same root cause as #18)
    and 222 in `islands/` (Fresh islands referenced via JSX
    `<MyIsland />` from `routes/*.tsx`, but the extractor doesn't
    treat JSX element usage as a call edge). Fix: extend the
    `is_entry_point_path` skip list with bundler-output dirs;
    extend the entry-point heuristic to mark Fresh `islands/*`,
    Next.js `pages/*` / `app/*`, and SvelteKit `routes/*` as
    framework-routing entry points (similar to v0.9 P5 Django
    CBV detection).

20. **JSX element references not captured as edges**. `<ContactList />`
    in a route file doesn't create an edge to the `ContactList`
    component in islands/. Detected only via PageRank on the
    function's transitive callers. The TSX extractor would need to
    walk JSX elements (`JSXElement`/`JSXOpeningElement`) and emit
    refs.

### Decisions taken from session 05

- ✅ **TS/JS feature flow validated**. `quick_orient`, `who_calls`,
  `get_symbol_source`, `analyze_impact` all returned clean signal
  on TypeScript symbols. The 4 quick-win agentic tools earn their
  tier-1 slots across 5 profiles.
- ✅ **Pagination contract paid off** on `who_calls` (count=27 with
  limit=10).
- ✅ **`audit_coverage` handles 0-RF TS repos gracefully** —
  consistent with the v0.8 dogfood result on livespec-mcp itself.
- ⚠️  **TS-specific dead-code accuracy needs framework awareness**
  similar to v0.9 P5 (Django CBV) — Fresh islands, Next.js pages,
  SvelteKit routes. Queued for v0.10+.
- ⚠️  **`top_symbols` filter must skip bundler output** —
  `_fresh/`, `dist/`, `build/`, `.next/`, `out/`. Trivial fix,
  high impact on first-impression UX for any TS/JS user.

### Updated tier signal (n=5 sessions, 5 profiles, 74 calls)

Default surface (16 tools after v0.9 P6 drop): all called at least
once across 5 profiles **except `find_orphan_tests`**. That last
silent tool needs a session that explicitly looks for orphan tests
(refactor profile on a repo with broken test coverage); leaving it
in tier-1 for now.

Plugin tier (11 RF mutation + 3 docs): RF mutation tools called only
in the demo-app session (the one with @rf: annotations).
Docs tools never called by an agent in 5 sessions — confirms the
v0.8 P3.5 decision to plugin-gate them.

---

### Wire validation of v0.9 fixes against Django (post-`d9d936d`)

After landing v0.9 P3 (weight filter), P4 (non-Python skip + dotted-
path strings + Meta inner classes), and P5 (Django CBV detection),
the same Django workspace re-indexed and queried:

| Tool | v0.8 baseline (session 04) | v0.9 (post-fixes) | Delta |
|---|---:|---:|---:|
| `find_dead_code` count | 824 | **514** | −38% noise |
| `find_dead_code` functions | 450 | 189 | **−58%** (JS skip + dotted strings dominate) |
| `find_dead_code` classes | 293 | 251 | −14% (Meta + AppConfig protected) |
| `find_dead_code` methods | 81 | 74 | −9% |
| `find_endpoints(framework='django')` | 20 | **162** | **+710%** (CBVs surfaced) |

The 514 remaining dead candidates on Django itself are largely:
- `Argon2PasswordHasher`, `ScryptPasswordHasher` — referenced by
  string in `PASSWORD_HASHERS` in user settings (out-of-tree).
- Database backend internal classes (Oracle, MySQL, GIS variants)
  — registered via dotted-path strings in user `DATABASES` settings.
- Lookups (`OverlapsLeftLookup`, etc.) — registered via
  `Field.register_lookup(...)` runtime calls.
- Public Django API exposed via `from django.x import Y` for
  third-party code — no in-project caller.

Pattern: each remaining false-positive needs either runtime-call
detection or out-of-tree-string awareness — both larger surface
than v0.9. Marked for v0.10+ planning.

`find_endpoints(framework='django')` 20 → 162 confirms the CBV
detection is the right fix. Before P5 the agent had no way to ask
"what views does this Django app expose?" and get a real answer.

### Updated tier signal (n=4 sessions, 4 profiles, 65 calls)

**Tier-1 (data-validated, ≥1 use across sessions of relevant profile)**:
1. `index_project` — every session
2. `get_project_overview` — every session
3. `find_symbol` — orient on unfamiliar names
4. `quick_orient` — first-contact composite (P0 win)
5. `who_calls` — backward cone (P0 win, refactor profile)
6. `who_does_this_call` — forward cone (P0 win, exploration)
7. `get_symbol_source` — body extraction (P0 win)
8. `analyze_impact` — wider blast radius
9. `git_diff_impact` — PR review
10. `find_dead_code` — refactor profile
11. `audit_coverage` — RF profile (the workflow runner session 02 — 0 RFs — also
    invoked it productively)
12. `get_requirement_implementation` — README's lead question
13. `list_requirements` — RF orientation
14. `propose_requirements_from_codebase` — brownfield onboarding

14/39 tools data-validated as tier-1.

**Plugin candidates (RF mutation, silent in agent flows)**:
`link_rf_symbol`, `bulk_link_rf_symbols`, `link_rf_dependency`,
`unlink_rf_dependency`, `get_rf_dependency_graph`,
`scan_rf_annotations`, `scan_docstrings_for_rf_hints`,
`create_requirement`, `update_requirement`, `delete_requirement`,
`import_requirements_from_markdown`. **11 tools** — exactly the set
CLAUDE.md `## Tool tier vision` flagged as `livespec-rf` plugin.

**Plugin candidates (docs, untouched)**:
`generate_docs`, `list_docs`, `export_documentation`. **3 tools** —
matches CLAUDE.md `livespec-docs` plugin proposal.

**Tier-4 / drop candidates (silent across all sessions)**:
`list_files`, `start_watcher`, `stop_watcher`, `watcher_status`,
`rebuild_chunks`, `get_call_graph`, `get_symbol_info`,
`get_index_status` (called only as immediate-after-index orientation,
which the resource subsumes), `search`, `bulk_link_rf_symbols`. The
last is a mutation tool that lands in plugin, not drop. **8 tools**
to drop / move to resource.

**Coverage gap remaining**: refactor profile against a non-Python
codebase (TS/JS feature work) and a scale check (Django/a large Rust monorepo). But
the v0.8 curation **can be drafted now from this data**, with
known-unknowns flagged for v0.9 follow-up.

### Stakeholder posture lock-in

This data corroborates everything in CLAUDE.md `## Stakeholder
posture`:
- RFs are first-class — RF tier-1 tools (4) all earned their slots.
- Agent UX is the product — the 4 P0 quick wins (`quick_orient`,
  `who_calls`, `who_does_this_call`, `get_symbol_source`) account
  for 17/40 calls (43% of all session traffic) across 2 sessions
  on different repos. They were the right wins to build first.
- Tool tier vision (CLAUDE.md `## Tool tier vision`) holds: the
  proposed plugin `livespec-rf` (RF mutation) maps 1:1 with the
  silent-in-agent-flows set; `livespec-docs` plugin maps 1:1 with
  the docs-management silent set.

The v0.8 P3 main pass — drops + plugin auto-detect — can land on
this data without further sessions. Sessions 04-05 (Django scale +
TS feature) are nice-to-have but no longer blocking.

---

## Wire validation of bugs #4-#10 (post-`a8daf0d`)

After landing fixes #4-#10 (`c14e8d4`) and the cross-file polish
(`a8daf0d`), re-running find_dead_code on livespec-mcp itself dropped
candidates from **18 → 1** (~94% noise reduction). Pre-fix the list
contained:

- `bench.run.main`, `bench.agent_log_analyze.main`, `server.main`
  (3 false positives via `__main__` guard) — all GONE ✓
- `storage.db._m001..._m007` (7 false positives via MIGRATIONS list
  literal) — all GONE ✓
- `instrumentation.AgentLogMiddleware` + `AgentLogMiddleware.on_call_tool`
  (cross-file framework registration in server.py) — both GONE ✓
- `Settings.safe_path`, `Chunk.content_hash`, `Watcher._run_worker`,
  `Watcher._on_any_event`, `AppState.project_id` — all GONE (covered by
  protected_class_qnames extension)

Single remaining flag: `start_watcher._do_reindex`. This is a
**nested function inside another function's body**, passed as a
callback to `Watcher(on_reindex=_do_reindex, ...)`. Closure pattern
— distinct from the 10 documented bugs. The fix would either:
- Walk function bodies for inner FunctionDef references (could
  produce false-skips), OR
- Track closure-capture in the extractor (proper fix, larger scope).

Queue for v0.9. The 94% reduction is good enough to ship.

The other tools also wire-validated against demo-app:
- `audit_coverage`: `shortener/__init__.py` no longer flagged orphan ✓
- `audit_coverage`: `rfs_with_test_coverage` count + `rf_test_coverage`
  list present ✓ (empty for this repo since RFs use `relation: implements`,
  not `tests`, but the field is wired)
- `git_diff_impact`: `suggested_tests` lists 8 real `test_*.py` files,
  zero `fixtures/` leakage ✓ (HEAD~5..HEAD diff over the bug-fix batch)

All ten bugs are closed end-to-end. Call graph signal is precise,
entry points are flagged, top_symbols is signal-rich, audit_coverage
respects package markers and credits test coverage, git_diff_impact
suggested_tests doesn't leak fixtures, propose_requirements skips
test trees.
