# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [SemVer](https://semver.org/).

## [Unreleased]

### Added — `find_symbol` typo suggestions
- `find_symbol` with zero matches now includes a `did_you_mean` list
  (same suggester as the not-found errors) instead of a bare empty
  result — found dogfooding v0.14.0 through the jig proxy: a typoed
  query left the agent at a dead end. Key absent when there are
  matches or no close candidates.

## [0.14.0] - 2026-06-12

Personal-fit sprint (multi-repo friction) + v0.13 framework sprint —
v0.13 was never tagged, so this release ships both batches.

### Fixed — embedding model cache survives reboots
- fastembed's default cache is `$TMPDIR/fastembed_cache` — wiped on
  every reboot on tmpfs systems, silently re-downloading ~200MB of
  model weights. Models now cache in
  **`~/.cache/livespec-mcp/fastembed`** (XDG-aware, shared across all
  workspaces). An explicit `FASTEMBED_CACHE_PATH` env var still wins.
  The unused per-workspace `Settings.models_dir` field was removed —
  per-workspace model copies were never the right design.

### Added — index-freshness recipe for Claude Code
- README documents a `PostToolUse` hook that runs
  `livespec-mcp index "$CLAUDE_PROJECT_DIR"` in the background after
  every Edit/Write — incremental hash-skip makes no-op runs cheap.
  This is the recommended freshness mechanism for single-user
  sessions; the watcher remains for multi-agent-free environments.

### Added — closure-capture detection for TS/JS/TSX + Rust
- **`find_dead_code(include_non_python=True)` no longer flags nested
  named functions referenced in their parent's body** — the
  `new Watcher(onEvent)` / `let cb: fn() -> i32 = on_event;` callback
  pattern. Port of the Python v0.8 fix #11 scan to tree-sitter; open
  since v0.8. Go is exempt by design (no named nested functions exist —
  closures are anonymous and never become symbols).

### Fixed — resources broken under multi-tenant (since v0.12)
- **Every `project://` / `doc://` / `code://` resource raised
  `WorkspaceRequiredError` in real use** since v0.12 made `workspace`
  per-call (resources have no parameter channel; the in-repo test
  fixture masked it). Resources now bind to the **most recently used
  workspace** (whatever the last tool call touched) — correct by
  construction for single-repo sessions. Before any tool call, JSON
  resources return an `mcp_error`-shaped payload with a hint; text
  resources a one-line explanation.
- **Resource error payloads now use the `mcp_error` shape**
  (`{error, isError, hint?}`) instead of ad-hoc `{"error": ...}` JSON —
  closes the v0.6 P4 contract gap for the resource surface.

### Changed — explicit unsupported-language reporting
- Files whose extension maps to a language **without an extractor**
  (c, cpp, c_sharp, kotlin, swift, scala) are no longer indexed as
  silent zero-symbol rows. They're skipped at walk time and counted in
  a new `languages_unsupported` payload key on `index_project`
  (`{"cpp": 12, ...}`), so agents see the coverage gap instead of
  inferring it. `.livespec.toml [index].languages` now validates
  against extractor-supported labels only.
- `index_project(force=True)` restores manual RF links with one
  set-based `INSERT..SELECT` instead of two queries per link.

### Fixed — watcher reindex failures now logged
- The debounced watcher worker swallowed every reindex exception
  (`except Exception: pass`). Still survives bad runs, but now logs the
  traceback under the `livespec.watcher` logger.

### Added — headless CLI subcommands
- **`livespec-mcp index <path> [--force] [--embed]`** and
  **`livespec-mcp status <path>`** run the exact same pipeline as the
  `index_project` tool / `project://index/status` resource and print
  JSON to stdout — index from cron, systemd timers, pre-commit hooks or
  CI without an MCP host in the middle. Errors go to stderr with exit
  code 1. `livespec-mcp` with no arguments (and the explicit
  `livespec-mcp serve`) remains the stdio MCP server, so existing
  mcp.json entries are untouched.

### Added — `.livespec.toml` per-repo config
- **Optional `.livespec.toml` at the workspace root** tunes indexing
  per-repo, loaded fresh on every `index_project` call (no restart).
  `[index]` table: `ignore` (gitignore-syntax patterns that **outrank**
  `.gitignore`, negations included), `languages` (extractor-label
  allow-list, e.g. `["python", "typescript"]`), `max_file_bytes`
  (default 2 MB). Malformed config fails the call with an actionable
  message instead of being silently ignored. `index_project` payload
  gains a `repo_config` echo key (`null` when no file). New dependency:
  `tomli>=2` on Python < 3.11 only (3.11+ uses stdlib `tomllib`).

### Added — gitignore-aware indexing
- **`index_project` now honours `.gitignore`** (root and nested files,
  `!negation` patterns included) via the `pathspec` library, layered on
  top of the existing hardcoded `DEFAULT_IGNORES` baseline. Git
  precedence semantics: the deepest `.gitignore` with an opinion wins;
  ignored directories are pruned (never descended), so re-includes
  inside an ignored directory don't apply — same limitation git
  documents. Workspaces without any `.gitignore` behave exactly as
  before. New dependency: `pathspec>=0.12`.

### Added — Hono framework support (call-style routing)
- **`find_endpoints(framework='hono')`**: scans indexed TS/JS files whose
  source mentions Hono for `app.get('/users', handler)` /
  `app.on('PURGE', '/cache', h)` / `app.route('/api', sub)` patterns.
  Each entry reports `hono_method` + `hono_path`; named handlers resolve
  to their symbol (`qualified_name`), inline arrows fall back to a
  `file:line` pseudo-qname. Opt-in only (reads files on demand, not part
  of the `framework=None` sweep).
- **Named-handler protection**: identifiers passed to registration-style
  calls (`get/post/put/delete/patch/options/all/route/use/on/once/
  subscribe/register/connect/addEventListener/addListener`) now emit
  `callback_arg` refs at extract time (inside symbol bodies → `who_calls`
  answers), and `find_dead_code(include_non_python=True)` runs a TS/JS
  module-level registration scan mirroring the Python
  `_runtime_registered_names` — the canonical Hono/Express pattern
  registers handlers at module top-level where no symbol owns the call.
  Also benefits Express-style apps and event emitters.

### Added — Spring Boot + Angular framework support
- **`find_endpoints(framework='spring')`**: surfaces `@RestController` /
  `@Controller` classes and `@GetMapping` / `@PostMapping` /
  `@PutMapping` / `@DeleteMapping` / `@PatchMapping` / `@RequestMapping` /
  `@ExceptionHandler` / `@EventListener` / `@Scheduled` methods.
- **`find_endpoints(framework='angular')`**: surfaces `@Component` /
  `@Injectable` / `@Directive` / `@Pipe` / `@NgModule` classes.
- **`find_dead_code` Spring/Angular awareness**: Spring stereotype +
  mapping annotations count as entry points (DI container invokes them).
  Angular template-bound classes (Component/Directive/Pipe) protect ALL
  their methods — HTML templates are invisible to the indexer
  (`(click)="save()"`); lifecycle hooks (`ngOnInit` & friends) are
  protected on any Angular-decorated class.

### Added — TS decorator + Java annotation extraction
- **`symbol.decorators` now populates for TS/JS/TSX and Java**, not just
  Python. TS/JS: `decorator` nodes on the declaration, on a wrapping
  `export_statement` (`@Component() export class Foo`), or as preceding
  siblings of class members (`@HostListener() onResize()`). Java:
  `annotation` / `marker_annotation` inside `modifiers`
  (`@RestController`, `@GetMapping("/x")`, dotted forms preserved).
  Foundation for Angular / Spring Boot framework detection.
- **Schema migration v8** (`ts_java_decorators_reextract`): no schema
  change; queues `needs_reextract` so existing DBs backfill decorators on
  the next `index_project` without `force=True`.

### Fixed — dual-decorator alias false positives in `find_dead_code`
- **Decorator aliases now recognized as entry points.** The plugin-framework
  pattern `agentic_tool = mcp.tool if X else _noop_decorator` hid the real
  decorator from the entry-point matcher — the stored decorator name is the
  alias, whose last segment is not in the known set. New cached per-file AST
  scan (`_entry_point_decorator_aliases`) collects assignment targets whose
  value (directly or through either branch of a conditional expression)
  resolves to an entry-point dotted name; works for assignments inside
  function bodies (`register()` pattern). Both IfExp branch names are also
  protected (`_noop_decorator` was only referenced inside the expression).
  Wire-validated: `find_dead_code` on livespec-mcp itself **22 → 0**.
- **Genuinely dead code removed** instead of suppressed: `graph.subgraph_edges`
  (orphaned by the v0.8 `get_call_graph` drop) and the watcher registry
  helpers `get_watcher` / `unregister_watcher` / `all_watchers` (orphaned by
  the v0.8 watcher-tool drops; `register_watcher` + `stop_all_watchers`
  remain — they serve `index_project(watch=True)` and atexit cleanup).

## [0.12.0] — 2026-05-29

### Added — multi-repo workspace support (require workspace on every call)
- **`workspace` is now required on every tool call.** No more
  `LIVESPEC_WORKSPACE` env var dependency or per-session state.
  Pass the absolute repo root on every invocation; the LRU cache
  (`get_state`) makes repeated calls to the same workspace cheap.
  One MCP server instance can now serve multiple repos concurrently
  by varying only the `workspace` argument — no restart required.
- **`index_project` docstring fixed** (B021). The docstring was an
  `f"""..."""` expression — Python treats it as a runtime expression,
  not `__doc__`, so MCP clients saw `None` as the tool description.
  Fixed by restoring a plain docstring and appending
  `WORKSPACE_DOCSTRING_NOTE` via `index_project.__doc__ =` after the
  function definition. Closes the "None description" report for the
  flagship tool.

### Fixed — JSDoc banner-with-text + manual-links data loss on force=True
- **`_is_separator_only` extended** to skip banner-style line comments
  with internal text wrapped in ≥2 separator chars (`// --- Token
  Management ---`, `// ============= Tool Execution Dispatcher
  =============`). Previously only pure separator lines (`// ---`)
  were dropped, so banner sections still won `docstring_lead` over
  the JSDoc immediately below. Locked in by
  `test_ts_jsdoc_skips_banner_with_internal_text`.
- **Data-loss fix on `index_project(force=True)`**: re-extraction
  cascade-deleted every `rf_symbol` row through the `symbol` FK,
  silently wiping links created by `bulk_link_rf_symbols` /
  `link_rf_symbol`. The indexer now snapshots non-annotation
  rf_symbol rows before re-extract and restores them by symbol qname
  after the new symbols are inserted. `index_project` payload gains
  `manual_links_restored`. Annotation-sourced links (`source =
  'annotation'`) intentionally NOT snapshotted — they are re-derived
  from fresh docstrings by `scan_annotations`, so preserving them
  would shadow legitimate edits to `@rf:` tags. Locked in by
  `test_manual_links_survive_force_reindex`.

### Added — JS/TS JSDoc support + agentic bulk-link + extractor-aware audit
- **JSDoc annotations now extracted on JS/TS.** `_ts_extract` reads the
  comment(s) immediately preceding a declaration (walking through wrapping
  `export` / `export default` statements) and stores the cleaned text as
  the symbol's `docstring`. Both `/** ... */` block comments and runs of
  `//` line comments are accepted, so any `@rf:RF-NNN` sitting above a
  TS/JS function/class/method now wires up automatically through the
  existing `scan_annotations` matcher — Python parity for the
  Deno/Node/React/Vue/Svelte stacks. Each comment is stripped
  individually before joining (so a `/** @rf:... */` adjacent to `//`
  line comments still has its tag at line-start where the matcher
  anchors), and pure ASCII separator lines (`// ---`, `// ===`) are
  dropped so they don't crowd out the meaningful JSDoc as
  `docstring_lead`. Locked in by `test_ts_jsdoc_docstring_populated`
  and `test_ts_jsdoc_wins_over_adjacent_separator_line_comment`.
- **`bulk_link_rf_symbols` promoted to the agentic surface.** Previously
  only registered by the `livespec-rf` plugin (which loads on DB-state
  signal), it is now part of the default tool tier so agents always
  have an escape hatch for languages or file types where the in-source
  annotation extractor can't reach (configs, SQL, YAML, languages
  without docstring extraction yet). Plugin no longer re-registers it.
- **`audit_coverage` distinguishes extractor gaps from real orphans.**
  New `modules_unsupported_language` bucket lists files whose language
  has no annotation extractor today (everything outside Python / JS /
  TS / TSX). They are removed from `modules_truly_orphan` so the
  actionable list is no longer drowned by false positives — the gap is
  in the extractor, not the project.

### Added — v0.12 P1 RAG layer wired end-to-end
- **`search` MCP tool** (new, default surface). Hybrid retrieval over
  AST-aware chunked symbols + RFs. FTS5 lane always live; vector lane
  fuses via Reciprocal Rank Fusion (k=60) when `[embeddings]` extra is
  installed and chunks are embedded. Reverses the v0.8 drop: this
  iteration the tool is wired to a real chunk pipeline and locked in
  by 9 tests, not the orphan stub from v0.7.
- **`embed_chunks` MCP tool** (new). Populates `chunk_vec_code` /
  `chunk_vec_text` vec0 tables for any chunks missing embeddings.
  No-ops cleanly when extras aren't installed (returns `mcp_error`
  with install hint).
- **`index_project` payload** gains `{chunks, embeddings}` fields and
  a new `embed=False` flag. Rebuilds chunks idempotently after the
  symbol/edge pass; skipped when no files changed and a chunk set
  already exists. With `embed=True`, also runs `embed_pending` so
  vector lane activates without a separate call.
- **9 tests** locking the contract: 6 default (chunk population, FTS
  keyword hit, scope filter, error shape, no-vec without embed,
  rebuild skip on no-change) + 3 `@pytest.mark.embeddings` (vec0
  populated, hybrid query lights up `lanes.vector`, `embed_chunks`
  idempotent). Suite total **243 default + 3 embeddings = 246**.
- **`.gitignore`** hardened for personal + ML artefacts: `.claude/`
  whole, model weight extensions (`.onnx|safetensors|gguf|bin|pt`),
  numpy dumps, fastembed/HF cache dirs, debug dumps, editor/OS files.

No schema migration (`chunk` + `chunk_fts` + `embedded_at` already
existed). No new dependencies (`fastembed` + `sqlite-vec` remain
opt-in via `pip install -e ".[embeddings]"`).

## [0.11.0] — 2026-05-01

The "TS framework readiness" release. Closes session-05 bugs #18-#20
(Deno Fresh / TS over-reporting) plus the last big bucket of Django
runtime-registration false-positives. Every win lands behind the same
default surface — no new tools, no breaking changes.

**Wire-validation against `SpeedRunners-landing` (217 files / 2532 symbols / 16567 edges, Deno Fresh + TS + TSX):**

| Tool | v0.10 | v0.11 | Delta |
|---|---:|---:|---:|
| `find_dead_code` (default) | 974 | **0** | −100% |
| `find_dead_code` (`include_non_python=True`) | 974 | **118** | −88% |
| `find_endpoints(framework="fresh")` | 0 (n/a) | **340** | new tool branch |
| `top_symbols` from `_fresh/` or `dist/` | 18/20 | **0/20** | clean |

The default-mode 974 → 0 reflects the combined effect of P0 (bundler dirs filtered), P1 (islands/routes recognised as entry points) and P2 (JSX edges connect islands to their renderers). The non-Python opt-in still drops 88% (974 → 118) because the bundler filter alone removes most of the noise even when entry-point heuristics are bypassed by `include_non_python=True`.

### Added — v0.11 P0 bundler/build output dir filter
- New module-level helper `_is_bundler_output_path(path)` recognises
  generated artefact dirs (`_fresh/`, `dist/`, `build/`, `.next/`,
  `out/`, `node_modules/`, `.svelte-kit/`, `target/`, `__pycache__/`,
  `.turbo/`, `.vite/`, `.cache/`, `.parcel-cache/`) plus minified
  artefacts (`*.min.js`, `*.min.mjs`, `*.min.css`, `*.bundle.js`).
- Applied in `find_dead_code` (skips bundler-generated symbols from
  the dead-code report) and `compute_project_overview` (filters
  `top_symbols` so generated noise no longer dominates project
  overview). Closes bug #18 surfaced by session 05 (Deno Fresh / TS).
- Tests: `tests/test_bundler_filter.py` covers the helper plus
  end-to-end behaviour for `find_dead_code` and `get_project_overview`.

### Added — v0.11 P1 TS framework entry-point detection
- New helpers `_ts_framework_entry_point_kind(path)` and
  `_is_ts_framework_entry_point(meta)` detect TS framework
  filesystem-routing files: Fresh `islands/`, Next.js `pages/` (pages
  router) and `app/{page,layout,loading,...}.tsx` (app router), SvelteKit
  `routes/+{page,layout,server,error}.*`, and Remix `app/routes/`.
- `find_dead_code`: symbols in those files are skipped from the dead-code
  report (guarded by `include_infrastructure=False` default). Closes
  bug #19 surfaced by session 05 (Deno Fresh / TS over-reporting).
- `find_endpoints`: extended `framework` literal to accept `"nextjs"`,
  `"fresh"`, `"sveltekit"`, `"remix"`. These frameworks are surfaced
  via a path-based scan (no decorators needed), returning symbols with a
  `ts_framework` field (`"fresh"`, `"nextjs_pages"`, `"nextjs_app"`,
  `"sveltekit"`, `"remix"`).
- Tests: `tests/test_ts_framework_entry_points.py` — 24 unit tests on
  path-matching helpers + 8 MCP integration tests (find_dead_code
  suppression + find_endpoints surfacing). 32 new tests total.

### Added — v0.11 P2 JSX element refs as call-graph edges
- `_ts_collect_calls` in `domain/extractors.py` now walks
  `jsx_opening_element` and `jsx_self_closing_element` nodes inside TSX
  function/component bodies and emits an `ExtractedRef` (kind `"jsx"`) for
  each component-typed JSX usage. Uppercase identifiers (`<Counter />`) and
  member-expression leftmost segments (`<Form.Field />` → `Form`) become
  ref targets; lowercase HTML elements (`<div>`, `<span>`) are skipped.
- The resolver's existing `_resolve_refs` path handles JSX refs identically
  to call refs — no schema change required.
- `find_dead_code` no longer over-reports React/Preact components that are
  only used as JSX elements (edge exists → not flagged as dead). Closes
  bug #20 surfaced by session 05 (Deno Fresh / TSX).
- Tests: `tests/test_tsx_jsx_refs.py` — 10 cases covering extractor-level
  ref emission (self-closing, paired, member-expression, HTML skip,
  multiple components) and integration-level call-graph edges + dead-code
  integration win.

### Added — v0.11 P3 runtime-registration name protection
- New `_runtime_registered_names(file_path_abs)` helper walks ALL function/
  method/class bodies for method calls whose attribute name is in a
  conservative set of registration verbs: `register`, `register_lookup`,
  `register_function`, `register_view`, `register_filter`, `register_tag`,
  `register_serializer`, `register_admin`, `connect`, `add_handler`,
  `subscribe`, `add_middleware`, `add_listener`, `on`, `use`.  Collects
  positional `Name` args and keyword `Name` values; ignores string args and
  lambdas to keep false-skip risk minimal.
- Wired into `find_dead_code`'s per-file loop alongside
  `_module_level_referenced_names` and `_publicly_exported_names`. Closes
  the last major bucket of Django false-positives from runtime registrations
  like `Field.register_lookup(MyLookup)` in `AppConfig.ready()`,
  `pre_save.connect(my_handler)` at module level, and
  `app.add_middleware(MyMiddleware)` in setup functions.
- Tests: `tests/test_runtime_registration.py` (13 cases: helper unit tests
  for all patterns + negative cases for non-registration verbs and string
  args + end-to-end `find_dead_code` integration tests).

## [0.10.0] — 2026-05-01

The "library codebase" release. v0.9 dropped Django `find_dead_code`
824 → 514 (−38%). v0.10 drops it further to **348** (−32% additional,
**−58% cumulative from v0.8**). Plus the language-coverage closeout:
session 05 against a Deno Fresh app validates the agentic flow on
TypeScript / TSX / JS, locking 5 profiles into the tier signal.

| Tool on Django (40K symbols) | v0.8 | v0.9 | v0.10 | Cumulative |
|---|---:|---:|---:|---:|
| `find_dead_code` count | 824 | 514 | **348** | **−58%** |
| `find_dead_code` classes | 293 | 251 | 164 | −44% |
| `find_dead_code` methods | 81 | 74 | 24 | −70% |
| `find_dead_code` functions | 450 | 189 | 160 | −64% |

### Added — v0.10 P1 publicly-exported names protect from dead-code
- New `_publicly_exported_names(file_path_abs)` walks each .py file's
  top-level for two patterns and adds them to `find_dead_code`'s
  `global_module_refs`:
  - **`from .impl import Foo, Bar as Baz`** in any module — the
    imported names (and their aliases) are recorded. Critical for
    library `__init__.py` re-exports: `django/contrib/auth/__init__.py`
    re-exporting `authenticate`, `Argon2PasswordHasher`, etc.
  - **`__all__ = ['Foo', 'Bar']`** module-level list/tuple — each
    string's trailing identifier is recorded.
  - `import x.y as z` recognized: bound name (or head segment for
    bare `import x.y`) recorded.
- Closes the largest remaining false-positive bucket on Django
  (~166 of the v0.9 514 candidates).

### Added — v0.10 P0 README lift
- v0.9 Django wins lifted above-the-fold to a four-row pull-out
  table (`find_dead_code`, `find_endpoints(django)`, `quick_orient`
  p95, partial reindex).
- New "30-second tour" section under the headline shows the agentic
  flow as runnable code with realistic JSON output sourced from
  Django session 04 logs.
- `docs/AGENT_QUICKSTART.md` now linked as a callout — existed
  since v0.8 P4 but was never surfaced.
- Plugin auto-detect framing tightened: "fresh repos get a 16-tool
  surface, RF-active repos get 27, with no config".

### Added — v0.10 P2 language coverage closeout (session 05)
- Battle-test session 05 against `SpeedRunners-landing` (Deno Fresh
  app, 217 files / 2532 symbols / 16,525 edges across TypeScript +
  TSX + JS). Validates the agentic flow on the most common non-Python
  stack. **5/5 profiles now covered**: exploration (jig), refactor
  (livespec-mcp), RF flow (url-shortener-demo), Django bugfix
  (Django), TS feature (SpeedRunners-landing).
- Confirmed working clean on TypeScript: `find_symbol`,
  `quick_orient`, `who_calls(max_depth=2)` (paginated to 10 of 27),
  `get_symbol_source`, `analyze_impact(summary_only=True)`,
  `audit_coverage(summary_only=True)` on a 0-RF TS repo.
- Three new TS-specific bugs surfaced (#18-#20):
  - **#18** `get_project_overview.top_symbols` polluted by bundler
    output (`_fresh/`, `dist/`, etc.) — top 18 of 20 symbols on a
    Fresh app live in minified bundles.
  - **#19** `find_dead_code` over-reports on Fresh apps (974
    candidates: 630 in `_fresh/`, 222 in `islands/` referenced via
    JSX from `routes/*.tsx`).
  - **#20** JSX element references not captured as call-graph
    edges. The TSX extractor would need to walk `JSXElement` nodes
    and emit refs.

### Tooling
- Default surface: **16 tools** (unchanged from v0.9). Plugin tier:
  14. Total max active: 30.
- Tests: 175 → **179** (+4 from `tests/test_exports_protect.py`).
- Schema: v7 (no migration in v0.10).

### Deferred to v0.11+
- Bug #18 — bundler-output dir filter on `top_symbols` and
  `find_dead_code` (`_fresh/`, `dist/`, `build/`, `.next/`, `out/`).
  Trivial fix, queue for next cycle.
- Bug #19 — TS framework entry-point detection (Fresh `islands/`,
  Next.js `pages/` + `app/`, SvelteKit `routes/`). Mirrors v0.9 P5
  Django CBV detection for the JS frameworks.
- Bug #20 — JSX element refs as edges in the TSX extractor.
- Out-of-tree runtime registration (Django `Field.register_lookup()`
  runtime calls). The remaining 348 Django candidates are largely
  this pattern.
- Closure-capture detection in non-Python languages.
- Optional LLM-assisted RF refinement on
  `propose_requirements_from_codebase`.

---

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
40 calls across 3 codebases (jig, livespec-mcp, url-shortener-demo)
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
- **3 sessions logged** across 3 codebases (jig 1173 syms, livespec-mcp
  itself 495 syms, url-shortener-demo 23 syms / 6 RFs), 40 calls total.
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
warp stress test surfaced.

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
  `summary_only`. Triggered by the warp stress test where
  `audit_coverage` produced 286K chars, `find_dead_code` 4.4M chars,
  `git_diff_impact` 7.3M chars — all over the MCP 25K-token budget.
- **`find_dead_code` skips Rust `pub` items** (B4) — symbols whose
  visibility is `pub` / `exported` / `public` are excluded by default
  (they have invisible callers from outside the indexed crate).
  `pub(crate)` and `pub(super)` are NOT skipped (scope-bounded).
  Override with `include_public=True`. The 23K dead-flagged symbols on
  warp dropped to a manageable list.
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
  actionable subset. Real bug surfaced on the url-shortener-demo run.
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

[0.12.0]: https://github.com/Rixmerz/livespec-mcp/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.11.0
[0.7.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.7.0
[0.6.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.3.0
