# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- v0.4 plan tracking (release hygiene, multi-language scoped resolution, aggregator tools).

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

[Unreleased]: https://github.com/Rixmerz/livespec-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.3.0
