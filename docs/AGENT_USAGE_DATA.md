# Agent Usage Data — v0.8 P2 battle-test

This file is the field log behind the v0.8 curation pass. It replaces
the opinion-based tier list (ROADMAP §6) with what real agent sessions
actually do across multiple unfamiliar codebases.

## How this gets filled

1. Pick a target codebase (Django subset, Next.js boilerplate, a large Rust monorepo
   subset, etc.). Clone it, set `LIVESPEC_WORKSPACE` to its path.
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
| 02 | livespec-mcp itself | refactor: extract another helper / modify a tool surface | exercises `who_calls`, `analyze_impact`, `git_diff_impact`, `audit_coverage` |
| 03 | demo-app | RF flow: `list_requirements` → `get_requirement_implementation` → `link_rf_symbol` | validates RF tier-1 hypothesis |
| 04 | Django subset (TBD) | bugfix: trace a known issue | scale check, larger codebase |
| 05 | TS/JS app (TBD) | feature: add an endpoint | language coverage |

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
