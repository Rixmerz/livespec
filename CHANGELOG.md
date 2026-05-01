# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [SemVer](https://semver.org/).

## [Unreleased]

_Nothing yet._

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
