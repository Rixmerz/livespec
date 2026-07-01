# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What this project is

**livespec-mcp** is a local-first MCP server that maintains a live call graph,
RF↔code traceability, and on-demand documentation for any codebase. It speaks
9 languages with passing extractor tests (Python, Go, Java, JS, TS, Rust,
Ruby, PHP) plus scoped resolution in 8 of them, and is framework-aware for
Flask, FastAPI, Click, pytest, FastMCP, Celery, Django, Next.js, Deno Fresh,
SvelteKit, Remix, Spring Boot, Angular and Hono (v0.13).

## Stakeholder posture (not co-architect — owner-with-skin)

Future instances of Claude Code reading this should treat livespec-mcp as
a **first-class deliverable for the AI-agent ecosystem**, not as one of many
side projects. The project is intentionally agent-shaped: it produces signal
that other agents (including future-you) consume to understand unfamiliar
codebases faster. Ship work here as if you'll personally use the result on
the next 50 unknown repos.

Two non-negotiable beliefs to maintain across sessions:

1. **RFs are first-class, not legacy ceremony.** Functional Requirement
   ↔ code traceability is how serious software orgs actually run agile/SAFe/
   Scrum-at-Scale at company scale. Regulated industries (finance,
   healthcare, automotive, aerospace) require it. Anytime the temptation
   appears to demote RF features as "nicho", remember: the niche is
   serious-software-shops, which is also where the long-term users live.
   Code intelligence is the universal layer; **RF traceability is the
   differentiator that makes livespec defensible**.

2. **Agent UX is the actual product.** Tools must answer questions an
   agent will reasonably ask in a real task ("what calls this?", "what
   breaks if I change this?", "what RFs touch this file?"). If a tool
   exists primarily for human end-users (bulk doc generation, export to
   markdown), it belongs in a plugin tier, not the default toolkit.

The current strategic frame is in [ROADMAP.md](./ROADMAP.md) and should be
read before starting any feature work in v0.8+.

---

## Commands

### Tests

```bash
uv run pytest -q -m "not embeddings"      # default suite (no model downloads)
uv run pytest -m embeddings                # add the embeddings smoke (~30s first run)
uv run livespec-mcp explorer serve .       # RF Explorer at http://127.0.0.1:8765/explorer/
uv run pytest tests/test_rf_deps.py -v     # single file
uv run pytest tests/test_rf_deps.py::test_link_and_walk_dependencies -v   # single test
```

Tests are integration-style — most use FastMCP `Client(mcp)` for in-process
MCP calls without subprocess or network.

### Benchmark

```bash
uv run python bench/run.py                 # fastapi, requests, rich (~2 min)
uv run python bench/run.py --quick         # requests only (~30s)
uv run python bench/run.py --large         # Django 5.1.4 stress test (~3 min cold)
uv run python bench/run.py --json bench/results-latest.json
```

Baselines live in `bench/results-baseline.json` (small repos) and
`bench/results-large.json` (Django). The `--large` run is what surfaced
the v0.7 pagination + Rust visibility work.

### Release flow

```bash
# After a phase batch is done:
git add -A && git commit -m "v0.X PN: ..."
git push origin main

# When cutting a release:
# 1. Promote CHANGELOG [Unreleased] -> [0.X.0] dated YYYY-MM-DD
# 2. Bump pyproject.toml version
# 3. Update README tool count + roadmap row
# 4. Update HANDOFF.md "estado actual"
# 5. Tag + push + GitHub release
git tag -a v0.X.0 -m "..."
git push origin v0.X.0
gh release create v0.X.0 --title "..." --notes-file /tmp/v0.X.0-notes.md
```

### Reload on the connected client

When MCP tool code changes, the host's MCP server is still running the OLD
code. Tell the user to run `/mcp` (Claude Code) or equivalent reconnect to
pick up new tools. Schema-migration columns also need a re-extract — the
migration framework auto-flags `needs_reextract` so the next `index_project`
populates new fields.

---

## Architecture

### Layered stack

```
tools/          MCP-exposed surface (32 tools: 19 core + 10 RF + 3 docs)
  analysis.py     find_symbol, quick_orient, get_symbol_source, who_calls,
                  who_does_this_call, analyze_impact, audit_coverage,
                  find_dead_code, find_orphan_tests, find_endpoints,
                  git_diff_impact, get_project_overview
  requirements.py RF agentic queries (list_requirements,
                  get_requirement_implementation) + brownfield
                  (propose_requirements_from_codebase) + bulk_link_rf_symbols
  indexing.py     index_project (runs rebuild_chunks + optional embed inside)
  search.py       search (hybrid FTS5 + sqlite-vec RRF) + embed_chunks
  docs.py         shared doc helpers (doc tools live in plugins/docs.py)
  plugins/rf.py   RF mutation: create/update/delete_requirement,
                  link_rf_symbol, link/unlink_rf_dependency,
                  get_rf_dependency_graph, scan_rf_annotations,
                  scan_docstrings_for_rf_hints, import_requirements_from_markdown
  plugins/docs.py generate_docs, list_docs, export_documentation
  _errors.py      mcp_error() helper — every tool error returns
                  {error, isError, did_you_mean?, hint?}

domain/         Pure business logic, no MCP coupling
  extractors.py   tree-sitter dispatcher per language; _ts_extract handles
                  JS/TS/Go/Ruby/PHP/Rust/Java; _py_extract uses ast for
                  precision. Decorators/annotations extracted for Python +
                  TS/JS/TSX + Java (v0.13). Hono route scan +
                  registration-callback name scan (v0.13)
  indexer.py      walks workspace, calls extractors, persists to symbol_ref,
                  resolves edges via _resolve_refs (INSERT OR IGNORE)
  graph.py        NetworkX wrapper + cache by (db_path, project_id, last_run_id)
                  -- v0.6 P3, ~4s -> µs on cache hit
  matcher.py      @rf: annotation parser (multi-RF, confidence override,
                  @not_rf negation, verb-anchored level-2 with negation guard)
  md_rfs.py       markdown spec importer
  rag.py          AST-aware chunking + FTS5 + optional sqlite-vec via RRF
  watcher.py      watchdog wrapper used by index_project(watch=True) + atexit
                  cleanup (watcher MCP tools dropped in v0.8)

storage/        SQLite persistence
  schema.sql      single-file schema; CREATE TABLE IF NOT EXISTS for everything
  db.py           connection bootstrap + ordered migration framework
                  (schema_migrations table; MIGRATIONS list append-only, at v8)
```

### Critical contracts (don't break)

**Schema migrations are append-only.** Each migration is a `(version: int,
name: str, fn: Callable[[Connection], None])` tuple in
`storage/db.py:MIGRATIONS`. Never reuse a version number, never reorder.
Adding a column? New migration tuple, never edit an existing one. Tests in
`tests/test_migrations.py` enforce monotonic versions.

**Error shape contract** (v0.6 P4): every tool error must use
`tools/_errors.py:mcp_error(message, did_you_mean=None, hint=None)`. The
shape is `{error, isError: True, did_you_mean?: list, hint?: str}`. No
custom `warning` fields, no extra keys. Tested in `tests/test_error_shape.py`.

**Aggregator pagination contract** (v0.7 B3): tools that return potentially
unbounded collections (find_dead_code, audit_coverage, find_orphan_tests,
find_endpoints, git_diff_impact) must accept `limit` (default 200) +
`cursor` + `summary_only=False`. Counts are always exact regardless of
pagination. Triggered by 4M-7M-char payloads on the warp Rust monorepo.

**Ref resolver is INSERT OR IGNORE only.** Never DELETE from `symbol_edge`
in `_resolve_refs`. Refs from unchanged files must survive when files
they target change. `tests/test_properties.py` has a Hypothesis property
locking this in.

**body_hash invariance** (v0.6 P0.D2): tree-sitter languages seed
`body_hash` from `_normalize_ts_body` (leaf-token walk that ignores
whitespace + comments). Python uses `ast.dump(annotate_fields=False,
include_attributes=False)` which is already stable. Real semantic change
must drift; reformat must not. Tested in
`tests/test_body_hash_stability.py`.

**Symbol dedup before insert**: `_replace_symbols` deduplicates by
`(qname, start_line)` because Django/warp ship `if/else def x:` shims that
produce duplicate symbols at the same line. Keep the first occurrence
(source order).

### Tool tiers (landed v0.8, multi-tenant since v0.12)

The v0.8 curation pass shipped: battle-test data (3 sessions, 40 calls,
11 bug fixes) drove the tier split. Current state:

- **19 core tools** always registered: code intel (find_symbol,
  quick_orient, get_symbol_source, who_calls, who_does_this_call,
  analyze_impact, git_diff_impact, find_dead_code, find_orphan_tests,
  find_endpoints, get_project_overview, index_project, search,
  embed_chunks) + RF agentic queries (audit_coverage, list_requirements,
  get_requirement_implementation, propose_requirements_from_codebase)
  + bulk_link_rf_symbols (escape hatch, promoted v0.12).
- **Plugin `livespec-rf` (10 tools)** — RF mutation ceremony: CRUD,
  link/unlink, RF-RF graph, scans, markdown import.
- **Plugin `livespec-docs` (4 tools)** — generate_docs, list_docs,
  export_documentation, export_explorer.
- **Dropped (v0.8-v0.9)**: list_files, get_index_status (resource
  `project://index/status`), get_symbol_info, get_call_graph,
  rebuild_chunks (runs inside index_project), watcher trio
  (race-condition trap; `index_project(watch=True)` remains).

**Plugin loading (v0.18):** plugins register at boot (`register_all_plugins`);
`PluginVisibilityMiddleware` filters `tools/list` and gates `tools/call` per
workspace (rf/doc rows, explorer bundle on disk, or `LIVESPEC_PLUGINS` override).
Session caches the last `workspace=` from tool calls.

---

## Conventions

### Documentation sync rules (NON-NEGOTIABLE)

Every dev iteration that lands code on `main` MUST update the relevant
docs **in the same commit batch** (not "next session"). These are not
suggestions — they're the contract that keeps HANDOFF resumable and the
public surface honest. Apply each rule by triggering condition:

- **HANDOFF.md — ALWAYS.** Any code-changing commit on `main` updates
  Section 3 ("Estado actual") with: new HEAD short-sha, total test
  count, one-paragraph summary of what landed, commit shas. Demote the
  prior state to a `### vX.Y resumen (referencia)` subsection. If the
  iteration plans further work, refresh Section 3a.
- **CHANGELOG.md — when public behaviour changes.** New tool, removed
  tool, changed payload shape, new flag, new error code, dependency
  bump that affects users, schema migration, perf-visible refactor →
  add bullet under `[Unreleased]`. Pure internal refactors / test-only
  / docs-only commits skip this. When cutting a tag, promote
  `[Unreleased]` → `[X.Y.Z] - YYYY-MM-DD` (release-flow checklist
  earlier in this file).
- **README.md — when surface changes.** Tool count in headline, the
  tool list, install instructions, headline numbers (Django dead-code
  reduction, etc.), or "what this answers" lead questions. NOT for
  internal phase notes — those live in CHANGELOG.
- **ROADMAP.md — when strategy moves.** A pillar advances, a tier
  decision is made with battle-test data, a feature is added/dropped
  from the v1.0 cut. NOT for tactical phase work.
- **pyproject.toml version — only when cutting a tag.** Never bump
  mid-phase; phase work lands under `[Unreleased]`.

**Enforcement heuristic before `git commit`**: ask yourself "did I
change behaviour an agent or user would notice?" If yes → CHANGELOG +
README. "Did I land code at all on main?" → HANDOFF. Treat skipping
these as a blocker, same as a failing test. If unsure whether something
qualifies, update the doc anyway — false positives are cheap, missed
updates rot the resume contract.

### Direct push to main

User has saved preference: commit + push directly to `main` for this repo.
**Skip the branch + PR ceremony unless the sandbox blocks the push.**
That preference is stored in
`~/.claude/projects/-Users-juanpablodiaz-my-projects-livespec-mcp/memory/feedback_workflow_main_direct.md`.

### Commit style

```
v0.X PN: short summary (≤72 chars)

Multi-paragraph body explaining EACH subtask, what changed, why,
tradeoffs. Test counts at the bottom.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Pass commit message via HEREDOC. Pre-commit hook validates — never bypass
with `--no-verify` unless explicitly asked.

### Phase batching

Each session typically tackles a single batch (P0, P1, P2, ...) and lands
1 commit per phase. v0.5 had P0+P1+P2+P3+P4-A3+P3-docs. v0.7 had
B5+B3+B4+B1+B6+B2+P7. Phase boundaries are `git push` points so a `/clear`
mid-session can be picked up cleanly via HANDOFF.md.

### HANDOFF.md is the resume point

After `/clear`, the user expects "leé HANDOFF.md y continuá" to restore
context. Section 3 ("Estado actual") is updated at the end of each release.
Keep older release sections as `3a`, `3b` etc. for traceability.

### Workspace argument

Every tool accepts an optional `workspace: str` parameter. When omitted,
the server resolves to `LIVESPEC_WORKSPACE` env var or cwd. The state
module (`state.py`) caches one `AppState` per absolute workspace path
(LRU=8). The `use_workspace` tool was deprecated in v0.2 and removed in
v0.6 — pass `workspace=` directly.

### When MCP code changes

Tool changes don't auto-reload on the host's running MCP process. The
user must `/mcp` reconnect. Schema additions trigger
`_migration_state.needs_reextract=1`, so the next `index_project` will
do a full re-extract automatically — no `force=True` needed unless the
user wants to override.

---

## Where to look first

| When you want to... | Read |
|---------------------|------|
| Pick up after `/clear` | `HANDOFF.md` |
| Plan v0.8+ work | `ROADMAP.md` |
| Understand version history | `CHANGELOG.md` |
| See public surface | `README.md` |
| Add a new schema column | `storage/db.py` MIGRATIONS list |
| Add a new tool | `tools/<area>.py` (use `mcp_error` for errors) |
| Add a language | `domain/extractors.py` + `tests/fixtures/<lang>/` + `tests/test_extractors.py` |
| Stress-test scaling | `bench/run.py --large` |

---

## What NOT to do

- **Don't add features in v0.8.** The ROADMAP says curation pass.
  Anything new must be justified against the tier-1 toolkit goal.
- **Don't drop RF tools.** They feel nicho but they're the differentiator
  for the serious-software-org segment.
- **Don't demote `get_requirement_implementation` or `list_requirements`
  out of tier-1.** They answer the README's lead questions
  ("¿Qué código implementa el RF-042?", "qué RFs existen"). Demoting them
  recreates the discrepancy that v0.8 alignment fixed.
- **Don't curate before battle-test.** ROADMAP §6 self-admits the tier
  list is opinion-based. v0.8 lands instrumentation + 5-codebase logged
  sessions FIRST; tool drops happen with data, not intuition.
- **Don't reuse migration version numbers.** Append-only or you corrupt
  user databases.
- **Don't add a custom error shape.** Use `mcp_error()`. If the existing
  shape doesn't fit, propose extending it (new field), don't bypass it.
- **Don't bypass the pagination contract** when adding aggregator tools.
- **Don't commit without running the full suite** (`uv run pytest -q -m "not embeddings"`).
- **Don't write `--no-verify`** to skip pre-commit hooks. Fix the
  underlying issue.
