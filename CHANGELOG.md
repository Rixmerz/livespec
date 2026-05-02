# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [SemVer](https://semver.org/).

## [Unreleased]

## [0.9.0] — 2026-05-01

The "Django readiness" release. Drives the v0.8 P2 battle-test bugs
(#12-#16) to closure end-to-end. The primary signal: same Django
codebase, same queries, dramatically cleaner answers.

| Tool | v0.8 | v0.9 | Delta |
|---|---:|---:|---:|
| `find_dead_code` count | 824 | 514 | −38% noise |
| `find_dead_code` functions | 450 | 189 | −58% |
| `find_endpoints(django)` | 20 | 162 | +8× |

### Removed (breaking) — v0.9 P6
- **`get_index_status` tool**. Honors the v0.8 P3.2 deprecation
  contract. Read the `project://index/status` resource for the
  same payload.

### Added — v0.9 P0 perf
- **Targeted `_resolve_refs` walk** on partial reindex. Closes the
  v0.7 deferred item. When a re-index changes only a subset of
  files (no `force`, no deletions, prior index run exists), the
  resolver walks only refs whose src is in a changed file OR whose
  `target_name` matches a name re-inserted in a changed file. Refs
  from unchanged files to unchanged files keep their existing
  edges (INSERT OR IGNORE on the same `(src, dst)` is a no-op).
  Measured on `requests`: partial reindex 25.3ms → 12.3ms (−51%).
  On Django the relative win is larger (refs scale superlinearly
  with symbols).

### Added — v0.9 P2 pagination on traversals
- **`who_calls`, `who_does_this_call`, `analyze_impact`** now accept
  the v0.7 B3 pagination contract — `limit` (default 200), `cursor`,
  `summary_only`. Closes session-04 bugs #12 and #13. At
  `max_depth=2` on `BaseBackend.authenticate` the unpaginated
  response was 102 KB (400 callers / 71 files); `analyze_impact`
  at `max_depth=3` was 332 KB (664 callers + 848 calls_into).

### Added — v0.9 P3 weight filter on traversals
- **`who_calls` / `who_does_this_call` / `quick_orient` /
  `analyze_impact`** default to `min_weight=0.6`, dropping the
  resolver fan-out edges (weight 0.5 — short-name candidates the
  static analyzer couldn't disambiguate). Closes session-04 bugs
  #14 and #17. Pass `min_weight=0.0` to recover the legacy
  unfiltered cone. The internal correctness tools (`find_dead_code`,
  `audit_coverage`) continue to count every edge so an ambiguous
  caller still proves the symbol is reachable.

### Added — v0.9 P4 Django dead-code accuracy (#16)
- **Skip non-Python files in `find_dead_code` by default**. The
  module-level reference scanner is Python-only — JS/Go/Java
  callsites are invisible to it. Vendored xregexp.js helpers
  (~70 of them) used to be over-reported on Django. New
  `include_non_python=True` opt-in restores the legacy behavior.
- **Recognize string-based dotted-path references** in the
  module-level scanner. Django settings register implementations
  as strings: `INSTALLED_APPS = ['app.apps.AdminConfig']`,
  `MIDDLEWARE`, `PASSWORD_HASHERS`, `default_app_config`.
  `_collect_module_refs` now adds the trailing identifier of any
  validated dotted-path string constant to the refs set.
- **Recognize Django framework inner-class hooks**:
  `class Meta:` / `class Migration:` inner classes are read
  reflectively by Django's metaclasses. Guarded by parent-segment
  PascalCase check so a stray module-level `class Meta:` is still
  flagged dead.

### Added — v0.9 P5 Django CBV detection in `find_endpoints` (#15)
- **`find_endpoints(framework='django')`** now scans class
  signatures for inheritance from Django's class-based view bases
  (View, TemplateView, ListView, DetailView, FormView, CreateView,
  UpdateView, DeleteView, RedirectView, archive views), auth
  mixins (LoginRequiredMixin, PermissionRequiredMixin,
  UserPassesTestMixin, AccessMixin), auth views (LoginView,
  LogoutView, PasswordResetView family), MiddlewareMixin,
  AutocompleteJsonView, and DRF-adjacent (APIView, ViewSet
  family). Matched classes ship a `django_cbv_base` field naming
  the responsible parent. Endpoints from both passes (decorator +
  CBV) are merged on `qualified_name` and sorted by
  `(file_path, start_line)` for stable cursor pagination.

### Tooling
- Default surface: 17 → **16 tools** after dropping
  `get_index_status`. Plugin tier unchanged (11 RF + 3 docs).
  Total max active: **30**.
- Tests: 157 → **175** (+18 net: +4 targeted resolver, +6
  traversal pagination, +4 weight filter, +4 dead-code Django,
  +4 CBV detection; −4 deprecation tests deleted with the tool).
- Schema: v7 (no migration in v0.9).

### Deferred to v0.10+
- Out-of-tree runtime registration detection (Django
  `PASSWORD_HASHERS` + `DATABASES` backend dotted-paths,
  `Field.register_lookup()` runtime calls). The remaining 514
  Django dead-code candidates are largely this pattern.
- Closure-capture detection in non-Python languages (TS arrow
  callbacks, Rust closures). Still open from v0.8.
- Optional LLM-assisted RF refinement on
  `propose_requirements_from_codebase`. Still open from v0.7.
- Session 05 (TS/JS feature flow) for language coverage closeout.

---

## [0.8.0] — 2026-05-01

The "curation" release. v0.7 piled on tools (39 + 4 deprecated aliases);
v0.8 cuts the surface to **17 default tools** plus two auto-loading
plugins (RF mutation = 11 tools, doc management = 3 tools). The
curation is data-driven: 3 sessions of real-agent battle-test logged
40 calls across 3 codebases (the workflow runner, livespec-mcp, demo-app)
and 24 of 39 tools never got called. The drops follow the data, not
the prior intuition. Stakeholder posture stays locked in: RF
traceability is the differentiator (RF agentic tools stay tier-1),
agent UX is the product (4 quick-win composites added before the
battle-test).

### Removed (breaking) — tier-4 drops (v0.8 P3.3)
- **8 tools dropped** based on zero or near-zero agent calls in
  3 sessions across 3 profiles:
  - `list_files` — Grep/ripgrep host with path glob covers it
  - `start_watcher`, `stop_watcher`, `watcher_status` — race-condition
    trap for editing agents; re-run `index_project` on demand
  - `rebuild_chunks` — auto-runs inside `index_project`
  - `get_call_graph` — `who_calls` + `who_does_this_call` cover both
    cones with cleaner output
  - `get_symbol_info` — `quick_orient` (composite) +
    `get_symbol_source` (body) cover both modes
  - `search` — FTS5 lane logged 0 agent calls; `find_symbol` +
    `quick_orient` are the canonical lookup path
- **Deprecated v0.6 RF tool aliases** are gone (P3a):
  - `link_requirement_to_code`     → use `link_rf_symbol`
  - `link_requirements`            → use `link_rf_dependency`
  - `unlink_requirements`          → use `unlink_rf_dependency`
  - `get_requirement_dependencies` → use `get_rf_dependency_graph`

### Changed (breaking) — plugin auto-detect (v0.8 P3.1, P3.4, P3.5)
- New `tools/plugins/` framework: at server startup the active
  workspace's DB is probed; plugins auto-load based on table state.
  `LIVESPEC_PLUGINS=none|all|rf,docs` env var overrides the soft
  default.
- **`livespec-rf` plugin** (auto-on when the `rf` table has rows for
  the active project, or when `LIVESPEC_PLUGINS` includes `rf`):
  registers the 11 RF mutation/linking tools — `create_requirement`,
  `update_requirement`, `delete_requirement`, `link_rf_symbol`,
  `bulk_link_rf_symbols`, `link_rf_dependency`, `unlink_rf_dependency`,
  `get_rf_dependency_graph`, `scan_rf_annotations`,
  `scan_docstrings_for_rf_hints`, `import_requirements_from_markdown`.
- **`livespec-docs` plugin** (auto-on when the `doc` table has rows,
  or when `LIVESPEC_PLUGINS` includes `docs`): registers the 3 doc-
  management tools — `generate_docs`, `list_docs`, `export_documentation`.
- The agentic-read RF tools (`list_requirements`,
  `get_requirement_implementation`, `propose_requirements_from_codebase`,
  `audit_coverage`) stay in the default surface — they answer questions
  an agent ASKS during work.

### Deprecated (non-breaking, drops in v0.9) — v0.8 P3.2
- **`get_index_status` tool**. Use the `project://index/status`
  resource (parity since P3b prep). The tool now ships
  `deprecated`/`replacement`/`removal` keys in its payload and emits
  a one-time stderr warning per process.

### Added — v0.8 P0 quick wins
- **`get_symbol_source(qname)`** — body slice extraction. Lighter than
  `get_symbol_info(detail='full')` when only the source text is needed.
  Returns `{qualified_name, file_path, start_line, end_line, source,
  body_hash}`.
- **`who_calls(qname, max_depth=1)`** — agentic alias for the backward
  cone of `analyze_impact`. Returns only the caller list, no forward
  cone, no RF rollup. Use when the agent's question is "what would
  break if I touched this?".
- **`who_does_this_call(qname, max_depth=1)`** — forward-direction
  counterpart of `who_calls`.
- **`quick_orient(qname)`** — composite first-contact snapshot.
  Combines symbol metadata, the first non-empty docstring line, the
  top-5 direct callers and top-5 direct callees ranked by PageRank, and
  any linked RFs. Replaces a typical `find_symbol` → `get_symbol_info`
  → `analyze_impact` → `get_requirement_implementation` chain with a
  single call.

### Added — v0.8 P2 prep (battle-test harness)
- **`bench/agent_log_analyze.py`** — aggregator over one or more
  `agent_log.jsonl` streams. Per-tool call count, errors, latency
  p50/p95, result_chars p50/max; top follow-up pairs (`A → B` within
  a session — surfaces 3-tool chains that a composite tool could
  collapse); silent-tool list (registered but never called — drop
  candidates). Markdown by default, `--json` for diffing across runs.
  Pre-fills the input feed for the v0.8 P3 curation pass.
- **`docs/AGENT_USAGE_DATA.md`** — skeleton for the field log. Lists
  target codebases, methodology notes, and the Findings template
  to fill once P2 sessions complete.

### Added — v0.8 P1 instrumentation
- **Agent dispatch logging middleware**
  (`src/livespec_mcp/instrumentation.py`). Writes one JSONL line per
  `tools/call` to `<workspace>/.mcp-docs/agent_log.jsonl` with
  `{ts, tool_name, args_redacted, latency_ms, result_chars, error,
  session_id, workspace}`. Args are redacted: any string containing
  the absolute workspace path is rewritten to `<workspace>/...` so
  logs are shareable. `LIVESPEC_AGENT_LOG=0` disables. Failures
  writing the log are swallowed — instrumentation never breaks
  dispatch. Sets up the v0.8 P2 battle-test (5 codebases × 3-5
  sessions) and feeds the v0.8 P3 data-driven curation pass.

### Added — v0.8 P2 battle-test sessions
- **3 sessions logged** across 3 codebases (the workflow runner 1173 syms, livespec-mcp
  itself 495 syms, demo-app 23 syms / 6 RFs), 40 calls total.
  Surfaces 11 bugs (#1-11), all fixed in this release.

### Fixed — v0.8 P2 bug batch (#1-11)
- **#1 Edge resolver same-name fan-out** (`_resolve_refs`). Multiple
  symbols sharing a short name (`list_tools` x3, `_cosine` x2)
  matched against a single call site, polluting `who_calls` and
  `quick_orient.top_callees`. Same-file fallback weight 0.7 when scope
  doesn't disambiguate. livespec-mcp edge count 969 → 752 (−227,
  ~22% reduction in false positives).
- **#2 Entry-point flag** in `quick_orient`. `@mcp.tool` / `@app.route`
  / etc. with 0 callers no longer reads as "dead". Output now ships
  `is_entry_point: bool` + `framework_decorators: [...]`.
- **#3 Structural-pattern noise** in `get_project_overview`. Top
  symbols dominated by `.get` x4 modules, `add_parser` x6 CLI
  subcommands, `run` x5 etc. — high PageRank but zero "what is this
  codebase about" signal. New filter excludes names that appear in
  ≥3 distinct files; opt-out via `include_structural_patterns=True`.
- **#4 `__main__` guards** as entry points. `bench.run.main`,
  `server.main` etc. flagged dead despite being called from
  `if __name__ == "__main__":` blocks. Module-level AST walk now
  collects refs from those guards.
- **#5 List/tuple-stored function refs**. `_m001_drop_dead_tables`
  through `_m007_visibility` flagged dead despite being referenced
  in the `MIGRATIONS = [(version, name, fn), ...]` list literal.
  Module-level walk now picks up bare-name refs in collection
  literals.
- **#6 Cross-file middleware lifecycle hooks**.
  `AgentLogMiddleware.on_call_tool` flagged dead despite being
  registered cross-file via `mcp.add_middleware(AgentLogMiddleware())`.
  Detection extended to recognize classes passed as arguments to
  `add_middleware` / similar registration calls.
- **#7 Test-fixture leakage** in `git_diff_impact.suggested_tests`.
  Files under `tests/fixtures/`, `tests/data/`, `__fixtures__/` now
  excluded from suggestions (they are not tests, they are inputs).
- **#8 `__init__.py` orphan flag** in `audit_coverage`. Package-marker
  files (`__init__.py`, `mod.rs`, `package-info.java`, `lib.rs`,
  `index.{ts,js}`) excluded from `modules_truly_orphan`.
- **#9 RF test-coverage signal** in `audit_coverage`. New
  `rf_test_coverage` field + `rfs_with_test_coverage` count surfaces
  test edges (`relation='tests'`) as a positive signal distinct from
  `relation='implements'`.
- **#10 Test-file proposals** from `propose_requirements_from_codebase`.
  No more "RF-009 Test Shortener" groupings: paths under `tests/`,
  `test/`, `__tests__/`, `fixtures/` skipped.
- **#11 Closure-callback nested fns** in `find_dead_code`.
  `start_watcher._do_reindex` flagged dead despite being passed as
  a callback (`Watcher(on_reindex=_do_reindex)`). Per-file
  `_used_nested_def_names` walk recognizes nested-def references in
  the parent scope's body.

After all 11 fixes wire-validated against livespec-mcp itself,
`find_dead_code` reports 0 (vs 18 pre-fix) — 100% noise reduction on
the dogfood repo.

### Added — v0.8 P4 pitch alignment
- `README.md` rewrite: new headline framing RF traceability as the
  defensible differentiator (not "(optional)"), tool surface restructured
  by tier (default / livespec-rf plugin / livespec-docs plugin),
  Performance section with battle-test numbers, "Agent vs human user"
  section explaining the surface split.
- `docs/AGENT_QUICKSTART.md` documents the canonical brownfield flow.
- `docs/AGENT_USAGE_DATA.md` captures the field log behind the
  curation decisions (40 calls / 3 sessions / 3 profiles).

### Tooling
- Default surface: **17 tools** (down from 39 in v0.7). Plugins add
  11 (rf) + 3 (docs) = **31 max active** when both plugins are loaded.
  Removed 4 deprecated v0.6 aliases for a true wire-count of 31 with
  no deprecated surface.
- Tests: 118 → **157**. Net +39 (+10 quick wins, +5 instrumentation,
  +8 analyzer, +12 plugin autoload, +4 deprecation, +others;
  −9 search/watcher/embeddings tests, −1 alias-compat).
- Schema: v7 (no migration in v0.8).

### Deferred to v0.9
- Drop the deprecated `get_index_status` tool (resource has been
  parity-equivalent since v0.8 P3b prep).
- Closure-capture detection in non-Python languages (TS arrow
  callbacks, Rust closures).
- `_resolve_refs` targeted re-walk (Django partial 7s → 1s) — still
  open from v0.7.
- Optional LLM-assisted RF refinement on `propose_requirements_from_codebase`.

---

## [0.7.0] — 2026-05-01

The "brownfield" release. Closes the friction gap between "fresh project
with livespec from day 1" and "existing 50K-symbol Rust monorepo,
adopting livespec on Tuesday afternoon". Three new agent-facing tools
(bulk_link_rf_symbols, scan_docstrings_for_rf_hints,
propose_requirements_from_codebase) plus correctness fixes that the
a large Rust monorepo stress test surfaced.

### Added — brownfield onboarding flow
- **`propose_requirements_from_codebase()`** (B2) — the headline feature.
  Heuristic RF discovery: groups symbols by qname prefix at
  `module_depth`, ranks each group by PageRank-weighted score, proposes
  one RF candidate per actionable group with humanized title +
  description from the top symbol's docstring + suggested_symbols list.
  Pair with create_requirement + bulk_link_rf_symbols to convert from
  "no RFs" to "fully traced" in N rounds instead of N×M.
- **`bulk_link_rf_symbols(mappings)`** (B1) — batch-link N RF↔symbol
  pairs in one transaction. Returns per-entry result so failures don't
  abort the batch. Idempotent (re-link returns ok=True linked=False).
- **`scan_docstrings_for_rf_hints()`** (B6) — surfaces RF candidates
  from existing docstrings that aren't yet linked. First sentence +
  leading verb extraction; verb_histogram_top output gives the agent
  the input signal for B2.

### Added — tool quality
- **Pagination on aggregator tools** (B3) — `find_dead_code`,
  `audit_coverage`, `find_orphan_tests`, `find_endpoints`,
  `git_diff_impact` now accept `limit` (default 200) + `cursor` +
  `summary_only`. Triggered by the a large Rust monorepo stress test where
  `audit_coverage` produced 286K chars, `find_dead_code` 4.4M chars,
  `git_diff_impact` 7.3M chars — all over the MCP 25K-token budget.
- **`find_dead_code` skips Rust `pub` items** (B4) — symbols whose
  visibility is `pub` / `exported` / `public` are excluded by default
  (they have invisible callers from outside the indexed crate).
  `pub(crate)` and `pub(super)` are NOT skipped (scope-bounded).
  Override with `include_public=True`. The 23K dead-flagged symbols on
  a large Rust monorepo dropped to a manageable list.
- **Schema migration v7**: `symbol.visibility` column populated by the
  extractor for Rust (`pub`/`pub(crate)`/`pub(super)`/`private`),
  TS/JS (`exported`), Java/PHP (`public`/`private`/`protected`).
- **`find_symbol` is separator-agnostic** (B5) — query
  `SyncQueue::push` matches Rust qnames stored as
  `app.src.server.sync_queue.SyncQueue::push`. Works in both
  directions (`Type.method` query also reaches `Type::method` qnames)
  and accepts `/` as a separator (path-style searches).

### Tooling
- Tools: 32 → 35 (+ 4 deprecated v0.6 aliases still present through
  v0.7 → wire count 39).
- Tests: 97 → 118 (+3 find_symbol normalization, +6 pagination,
  +2 visibility, +3 bulk_link, +3 rf_hints, +4 propose_requirements).

### Deferred to v0.8
- Drop the v0.6 deprecated aliases (`link_requirement_to_code`,
  `link_requirements`, `unlink_requirements`,
  `get_requirement_dependencies`) — they were promised through v0.7.
- `_resolve_refs` targeted re-walk (partial reindex on Django: 7s → ~1s).
- LLM-assisted RF refinement: optional sampling layer on top of B2's
  heuristic to refine titles + descriptions with the agent's reasoning.

---

## [0.6.0] — 2026-05-01

The "hardening" release. Stops the feature treadmill to pay down debt:
explicit migration framework, unified error shape, performance baseline on
a 40K-symbol repo with the obvious hotspot patched, deprecated tools
removed, ambiguous tool names disambiguated. Pitch reframed honestly —
"living traceability + on-demand docs" instead of overclaiming on the docs
side.

### Removed (breaking)
- **`use_workspace` MCP tool** (deprecated since v0.2). Pass
  `workspace=<path>` to every tool, or set `LIVESPEC_WORKSPACE` in the env.

### Renamed (deprecated aliases retained through v0.7)
- `link_requirement_to_code`     → `link_rf_symbol`
- `link_requirements`            → `link_rf_dependency`
- `unlink_requirements`          → `unlink_rf_dependency`
- `get_requirement_dependencies` → `get_rf_dependency_graph`

The old names still work — they delegate to the new implementations and
will be removed in v0.7. Naming disambiguates the two link concepts:
`link_rf_symbol` (RF → code) vs `link_rf_dependency` (RF → RF).

### Added
- **Explicit migration framework** (P2) — replaces ad-hoc
  `_migrate_v1_to_v2` with `schema_migrations(version, name, applied_at)`
  table backing an ordered, append-only migration list. Each migration
  is a small idempotent function; once applied, the version is recorded
  so subsequent connects skip already-applied work. Six migrations
  registered, retroactively covering every v0.1→v0.5 schema change.
- **Unified error payload helper** (P4) — `tools/_errors.py:mcp_error()`
  enforces a single shape across every tool error site:
  `{error, isError, did_you_mean?, hint?}`. Refactored ~15 sites in
  analysis, requirements, docs, and search tools. Removed the legacy
  `warning` field on `analyze_impact`.
- **Hints on actionable errors** — RF-not-found, symbol-not-found,
  file-not-indexed, cycle-detected, embeddings-missing, git-not-on-PATH,
  git-timeout. Each one now ships with a one-line `hint` field telling
  the agent what to run next.
- **Graph cache** (P3) — `domain/graph.py` now caches the loaded
  `GraphView` keyed by `(db_path, project_id, last_index_run_id)`.
  Building the NetworkX object from SQL costs ~4s on a 40K-symbol repo
  and was repeated on every analysis call; cache hits drop to µs and
  invalidate automatically when a new index run lands.
- **Django stress test** (P3) — `bench/run.py --large` runs against
  Django 5.1.4 (~40K symbols, 1M edges). Numbers documented in
  `bench/README.md`.

### Fixed
- **Duplicate (qname, start_line) crash** — Django's compatibility shims
  (`def cached_property(...)` defined twice under a Python-version `if`)
  produced symbols that tripped the v0.6 schema's UNIQUE constraint.
  `_replace_symbols` now deduplicates by `(qname, start_line)` before
  insert, keeping the first occurrence (source order).

### Changed
- **README pitch** — was "living documentation"; now "living
  traceability + on-demand docs" with an explicit table calling out
  what is/isn't auto-maintained. Drift is detected, not fixed —
  auto-doc-on-drift is a deferred v0.7+ candidate.

### Deferred to v0.7
- **`_resolve_refs` targeted re-walk** — partial reindex on Django
  takes 7s because the resolver re-walks all 1M `symbol_ref` rows. Filter
  to refs whose `target_name` matches a name in the changed file.
- **Auto-doc-on-drift watcher mode** — optional, opt-in, with a clear
  cost UX (LLM calls implicit).
- **Multi-tenant memory pressure handling** — current LRU=8 doesn't
  consider per-workspace RSS; a Django-scale cache could hit ~5GB worst
  case across 8 workspaces.
- **Drop deprecated v0.6 aliases** (`link_requirement_to_code`,
  `link_requirements`, `unlink_requirements`,
  `get_requirement_dependencies`).

### Tooling
- Tests: 83 → 97 (+4 migrations, +6 error shape, +3 graph cache, +1
  alias-still-works).
- Tool count: 33 → 32 (use_workspace removed) plus 4 deprecated aliases
  through v0.7. Wire count during the deprecation window: 36.

---

## [0.5.0] — 2026-05-01

The "self-improvement from real-world usage" release. Bug fixes from a
demo-project run, two new agent-modeling features (decorators + RF
dependency graph), and a hardened matcher with a regression-locked golden
dataset. Closes the last multi-language scoped-resolution gap (Rust).

### Added
- **`find_endpoints(framework=None)`** — list symbols decorated with
  framework entry-point markers (route, command, fixture, tool, task, etc).
  Per-framework presets: flask, fastapi, click, pytest, fastmcp, celery,
  django.
- **RF dependency graph** (P2):
  - `link_requirements(parent_rf_id, child_rf_id, kind)` with cycle
    detection on insert. `kind` ∈ {requires, extends, conflicts}.
  - `unlink_requirements(parent, child, kind=None)` — drops one specific
    edge or every edge between the pair.
  - `get_requirement_dependencies(rf_id, direction='both', max_depth=5)` —
    walk the RF graph forward / backward / both.
  - `analyze_impact(target_type='requirement')` now cascades through
    dependents — changing RF-001 surfaces every RF that transitively
    depends on it as `dependent_requirements`.
- **Multi-RF, confidence override, and explicit negation in the matcher**:
  - `@rf:RF-001, RF-002` — multi
  - `@rf:RF-001:0.85` — per-line confidence override (range [0.0, 1.0])
  - `@not_rf:RF-001` / `@!rf:RF-001` — cancel any L1+L2 hit on the listed
    RF in this docstring (overrides verb-anchored false positives that
    the negation-window heuristic missed).
- **Golden-dataset regression test** (`tests/data/matcher_golden.jsonl`,
  35 cases) — locks every supported annotation form against silent
  regression.
- **Rust scoped resolution** via `use` declarations (P4.A3) — closes the
  last common-language gap from P0.4. `use crate::module::Item`,
  `Item as alias`, brace groups, and recursive paths all populate the
  imports map. Cross-module Rust calls now resolve to weight=1.0 edges.
- **`audit_coverage`: `modules_implicitly_covered` + `modules_truly_orphan`**
  (P0.A1) — splits `modules_without_rf` into "reached transitively by
  rf-linked symbols" vs "actually orphan". The truly_orphan list is the
  actionable subset. Real bug surfaced on the demo-app run.
- **`symbol.decorators` (JSON)** — schema migration v3 adds the column
  and queues a forced re-extract via `_migration_state.needs_reextract`.
  Python `_py_extract` populates from `decorator_list`. Tree-sitter langs
  ship with `[]` until per-language extractors land.

### Changed
- **`find_dead_code` skips framework-decorated symbols by default**
  (P1.B1). A `@app.route` handler, `@click.command`, `@pytest.fixture`,
  `@mcp.tool`, etc. is no longer flagged as dead even with zero
  in-project callers — they have hidden callers (HTTP routers, CLI
  dispatchers, pytest collection, MCP). Pass `include_infrastructure=True`
  to bypass.
- **`git_diff_impact` clean error messages** (P0.A2) — previously dumped
  the full `git diff --help` output (~80 lines) into the error field
  when the workspace wasn't a git repo or the ref was unknown. Now
  classifies common stderr signatures and returns a single line.
- **body_hash invariant under reformat** (P0.D2) — Python is unchanged
  (already stable through ast.dump). Tree-sitter languages now seed the
  body hash from a leaf-token walk that ignores whitespace, blank lines,
  and comment nodes. Reformat (autoformat run, indent change, blank-line
  shuffle, comment add/remove) no longer drifts the doc; real semantic
  change still does.
- **`call_target_and_leftmost`** treats `::` as a path separator alongside
  `.`, enabling Rust `Item::method()` and PHP `Class::method()` to extract
  rightmost target + leftmost identifier correctly.

### Tooling
- Tool count: 30 → 33 (+ link_requirements, unlink_requirements,
  get_requirement_dependencies; find_endpoints replaced an internal helper).
- Tests: 76 → 83 (+1 audit transitive split, +1 git_diff not-a-repo,
  +3 body_hash stability, +2 decorators in dead/endpoints, +1 Rust
  scoped, +5 RF deps, +1 golden dataset runner, +misc).
- Schema migration v3: `symbol.decorators` + `rf_dependency` table.

### Deferred to v0.6
- mkdocs site (C5) — nontrivial setup, not blocking.
- Auto-doc on drift mode in the watcher — needs careful UX around LLM
  cost.
- Streaming graph queries via SQLite recursive CTE — only matters above
  ~50K symbols.

---

## [0.4.0] — 2026-05-01

The "multi-language parity + agent UX" release. Closes the scoped-resolution
debt from P0.4 across 5 more languages, adds three aggregator tools that
reuse the call graph + RF tables for free, and surfaces `did_you_mean`
suggestions on misspelled symbol identifiers.

### Added
- **Scoped resolution for TS/JS, Go, Ruby, PHP** (P1) — closes the multi-language
  parity gap from P0.4 (Python-only). ES6 imports + CommonJS requires for
  TS/JS, package imports + aliases for Go, `require_relative` for Ruby (+
  Const.method receiver lookup), `use` namespaces for PHP (+ `Class::method`
  scoped-call lookup). Cross-file/cross-package calls now emit `symbol_edge`
  rows with `weight=1.0`.
- **`find_dead_code()`** (P2) — symbols with zero callers and zero RF links;
  filters entry-point paths (`tests/`, `scripts/`, `bin/`, `__main__.py`,
  `manage.py`) and implicit entry points (dunders, FastMCP `register` outers,
  DI helpers).
- **`audit_coverage()`** (P2) — three RF coverage signals:
  `modules_without_rf`, `rfs_without_implementation`, `rfs_low_confidence`
  (avg confidence < 0.7).
- **`find_orphan_tests()`** (P2) — test functions whose forward call cone
  never reaches a non-test symbol.
- **`did_you_mean` field** (P2) on every `Symbol '<x>' not found` error
  across 5 tools (`get_symbol_info`, `get_call_graph`, `analyze_impact`,
  `link_requirement_to_code`, `generate_docs`). Two-pass matcher: SQL
  substring + difflib edit-distance.
- **`stop_all_watchers()` + `atexit` hook** (P2) — server shutdown flushes
  WAL files cleanly.

### Changed
- `_resolve_module_path()` for TS/JS converts relative paths and strips
  `.ts/.tsx/.js/.jsx/.mjs/.cjs` plus trailing `/index`.
- `call_target_and_leftmost()` now reads `receiver` (Ruby), `scope` (PHP),
  `object` (JS member access) fields. Strips PHP `$` and namespace
  backslashes when computing the leftmost identifier.

### Tooling
- Tool count: 26 → 29.
- Tests: 53 → 71 (59 default + 2 embeddings + 10 new in this batch).
- New language fixtures: TS / JS / Go / Ruby / PHP cross-module dirs.

### Fixed (CI)
- `.github/workflows/ci.yml` switched from `uv pip install --system`
  (PEP 668: externally managed `/usr` Python on Ubuntu runners) to per-matrix
  `uv venv --python X.Y` + `uv run pytest`. Matrix now actually runs each
  Python version; embeddings job also fixed.

---

## [0.3.0] — 2026-04-30

The "honesty + agent-loop" release. Closes the multi-language coverage debt
from v0.2 and adds the killer demo tool: `git_diff_impact`.

### Added
- **`git_diff_impact(base_ref, head_ref, max_depth)`** — changed files →
  callers → impacted RFs → suggested test files. The CI/PR-review entry
  point. (P1)
- **`delete_requirement(rf_id)`** — cascade-removes `rf_symbol` links. (P1)
- **`import_requirements_from_markdown(path)`** — bulk-create RFs from
  `## RF-NNN: Title` markdown with `**Prioridad:**` / `**Módulo:**`
  metadata. Idempotent. (P2)
- **`code://symbol/{qname}` resource** — fetch the source body of a symbol
  by qualified name. (P2)
- **`watch=True` flag on `index_project`** — start the file watcher in the
  same call. (P1)
- **Hypothesis property tests** — 4 properties covering matcher invariants,
  resolver weights, and indexer idempotence. (P2)
- **Memory benchmark** — RSS sampling during index of `requests` repo,
  baseline in `bench/results-baseline.json`. (P2)
- **GitHub Actions CI** — matrix Python 3.10/3.11/3.12 + dedicated
  embeddings job. (P2)
- **Ruby + PHP fixtures + extractor tests** — upgrades both languages from
  "untested" to "tested" in the language-support table. (P2)
- **Embeddings smoke test** — guarded by `pytest -m embeddings`, validates
  fastembed + sqlite-vec end-to-end when extras are installed. (P1)

### Changed
- **Auto-scan `@rf:` annotations after every `index_project`** — traceability
  stays fresh without requiring a separate `scan_rf_annotations` call. (P0)
- **PageRank infrastructure filter** in `get_project_overview` —
  `_is_infrastructure` heuristic excludes DI helpers, dunders, FastMCP
  `register` outers, and 1-line wrappers from the top-N by default. Opt-in
  with `include_infrastructure=True`. (P0)
- **Scoped resolution by imports for Python** — `symbol_ref.scope_module`
  populated from `import` / `from … import …` statements. Edges resolve to
  weight=1.0 when the target is in scope, weight=0.5 only as global
  fallback. (P0)
- **Migration `_migration_state.needs_reextract`** consumed correctly so
  stats reflect post-upgrade reality. (P0)

### Tooling
- **26 MCP tools** (was 23 in v0.2). Net additions: `git_diff_impact`,
  `delete_requirement`, `import_requirements_from_markdown`.
- **53 tests** total (51 default + 2 `embeddings`-marked).
- **8 languages with passing extractor tests:** Python, Go, Java,
  JavaScript, TypeScript, Rust, Ruby, PHP.

---

## [0.2.0] — internal

Multi-tenant + tool consolidation. Tagging skipped; rolled into v0.3.

### Added
- `use_workspace(path)` runtime workspace switching, then per-call
  `workspace=` argument. LRU cache (size=8) of DB connections.
- `start_watcher` / `stop_watcher` / `watcher_status` (watchdog wrapper).
- Bench suite (`bench/run.py`, `bench/results-baseline.json`).
- Large-repo procedural fixture (100+ symbols).
- Regression test suite locking in 4 prior bugs (edge wipe on idempotent
  re-index, FTS5 score corruption, signature drift, lost edges from
  unchanged files during incremental).

### Changed
- **Tool consolidation 25 → 23.** Six v0.1 tools were removed in favor of
  parameterized variants (see migration table below).
- Stateless server: workspace is resolved per-call (env →
  `LIVESPEC_WORKSPACE` → cwd) instead of held as global state.
- Persistent `symbol_ref` table (replaces in-memory ref dict from earlier
  experiments).

### Removed (breaking)
| v0.1 tool | Replacement |
|-----------|-------------|
| `find_references(identifier)` | `analyze_impact(target_type='symbol', target=qname, max_depth=1)` — read `impacted_callers` |
| `suggest_rf_links(rf_id)` | `search(query=<rf.title + rf.description>, scope='code')` + post-filter |
| `embed_pending()` | `rebuild_chunks(embed='yes')` |
| `generate_docs_for_symbol(identifier)` | `generate_docs(target_type='symbol', identifier=…)` |
| `generate_docs_for_requirement(rf_id)` | `generate_docs(target_type='requirement', identifier=rf_id)` |
| `detect_stale_docs(target_type)` | `list_docs(target_type, only_stale=True)` |

---

## [0.1.0] — internal

Bootstrap. Phases 0–6 of the original design.

### Added
- FastMCP 2.x server with stdio transport, `fastmcp.json` entry.
- SQLite schema (`project`, `file`, `symbol`, `edge`, `rf`, `rf_symbol`,
  `doc`, `chunk` + FTS5 + optional `vec0` virtual table). WAL mode,
  `foreign_keys=ON`.
- Tree-sitter + `tree-sitter-language-pack` parsing for the multi-language
  generic extractor.
- Python `ast`-based extractor for high-precision Python (functions,
  classes, methods, decorators, calls).
- NetworkX call graph + PageRank with pure-Python fallback.
- xxhash content/body/signature hashing for incremental re-index.
- Two-level `@rf:` annotation matcher (`@rf:RF-NNN` weight 1.0,
  verb-anchored phrase weight 0.7) with negation guard.
- BM25 (`rank-bm25`) + FTS5 keyword search; optional `[embeddings]` extra
  with `fastembed` + `sqlite-vec` and Reciprocal Rank Fusion.
- `generate_docs` (dual-mode: caller-supplied vs MCP sampling), drift
  detection on body + signature hashes, `export_documentation` to markdown
  or JSON.
- 7 user-facing prompts: `onboard_project`, `analyze_change_impact`,
  `audit_requirement_coverage`, `extract_requirements_from_module`,
  `document_undocumented_symbols`, `refresh_stale_docs`, `explain_symbol`.
- Resources: `project://overview`, `project://index/status`,
  `project://requirements`, `project://requirements/{rf_id}`,
  `project://files/{path*}`, `project://symbols/{qname*}`,
  `doc://symbol/{qname*}`, `doc://requirement/{rf_id}`.

[Unreleased]: https://github.com/Rixmerz/livespec-mcp/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.7.0
[0.6.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.3.0
