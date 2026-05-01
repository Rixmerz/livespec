# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What this project is

**livespec-mcp** is a local-first MCP server that maintains a live call graph,
RF↔code traceability, and on-demand documentation for any codebase. It speaks
9 languages with passing extractor tests (Python, Go, Java, JS, TS, Rust,
Ruby, PHP) plus scoped resolution in 8 of them.

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
tools/          MCP-exposed surface (32 tools default, ~3 plugins eventually)
  analysis.py     find_symbol, get_symbol_info, analyze_impact, audit_coverage,
                  find_dead_code, find_orphan_tests, find_endpoints,
                  git_diff_impact, get_project_overview, get_call_graph
  requirements.py CRUD + RF-symbol linking (link_rf_symbol, bulk_link_rf_symbols)
                  + RF-RF graph (link_rf_dependency, get_rf_dependency_graph)
                  + brownfield (propose_requirements_from_codebase, scan_docstrings_for_rf_hints)
  indexing.py     index_project, get_index_status, list_files
  search.py       search (hybrid FTS5 + sqlite-vec) + rebuild_chunks
  docs.py         generate_docs, list_docs, export_documentation
  watcher.py      start_watcher, stop_watcher, watcher_status
  _errors.py      mcp_error() helper — every tool error returns
                  {error, isError, did_you_mean?, hint?}

domain/         Pure business logic, no MCP coupling
  extractors.py   tree-sitter dispatcher per language; _ts_extract handles
                  JS/TS/Go/Ruby/PHP/Rust; _py_extract uses ast for precision
  indexer.py     walks workspace, calls extractors, persists to symbol_ref,
                  resolves edges via _resolve_refs (INSERT OR IGNORE)
  graph.py       NetworkX wrapper + cache by (db_path, project_id, last_run_id)
                  -- v0.6 P3, ~4s -> µs on cache hit
  matcher.py     @rf: annotation parser (multi-RF, confidence override,
                  @not_rf negation, verb-anchored level-2 with negation guard)
  md_rfs.py      markdown spec importer
  rag.py         AST-aware chunking + FTS5 + optional sqlite-vec via RRF
  watcher.py     watchdog wrapper + per-workspace registry + atexit cleanup

storage/        SQLite persistence
  schema.sql      single-file schema; CREATE TABLE IF NOT EXISTS for everything
  db.py          connection bootstrap + ordered migration framework
                 (schema_migrations table; MIGRATIONS list is append-only)
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
pagination. Triggered by 4M-7M-char payloads on the a large Rust monorepo Rust monorepo.

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
`(qname, start_line)` because Django/a large Rust monorepo ship `if/else def x:` shims that
produce duplicate symbols at the same line. Keep the first occurrence
(source order).

### Tool tier vision (v0.8 target — not yet implemented)

Per ROADMAP.md, the ~35 tools today should be split:

- **Tier 1 default** (~12 tools): index_project, find_symbol,
  get_symbol_info, analyze_impact, git_diff_impact, audit_coverage,
  find_dead_code, propose_requirements_from_codebase, plus 3-4 quick wins
  to add (`get_symbol_source`, `who_calls`, `quick_orient`, `grep_in_indexed_files`)
- **Tier 2 plugin** (RF management, ~12 tools): create/update/delete RFs,
  linking tools, bulk_link_rf_symbols, RF-RF graph
- **Tier 3 plugin** (docs management, ~3 tools): generate_docs, list_docs,
  export_documentation

This curation is the headline of v0.8.

---

## Conventions

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
- **Don't reuse migration version numbers.** Append-only or you corrupt
  user databases.
- **Don't add a custom error shape.** Use `mcp_error()`. If the existing
  shape doesn't fit, propose extending it (new field), don't bypass it.
- **Don't bypass the pagination contract** when adding aggregator tools.
- **Don't commit without running the full suite** (`uv run pytest -q -m "not embeddings"`).
- **Don't write `--no-verify`** to skip pre-commit hooks. Fix the
  underlying issue.
