# livespec-mcp

Local-first MCP server for **living traceability + on-demand docs** with
bidirectional **Functional Requirement <-> code** linking.

Index a workspace once, then ask questions like:

- ¿Qué código implementa el RF-042?
- Si modifico la función `auth.verify`, ¿qué RFs y qué llamadores se ven afectados?
- ¿Qué módulos no tienen ningún RF asociado?
- ¿Qué RFs dependen de RF-042 transitivamente?

### What "living" actually means here

| Layer | Lives | How |
|---|---|---|
| Symbol index | ✅ | xxh3 content-hash incremental + watcher (`start_watcher`) |
| Call graph + edges | ✅ | re-resolved on every change; persistent `symbol_ref` |
| RF ↔ code links | ✅ | auto-scan of `@rf:` annotations after every `index_project` |
| RF ↔ RF graph | ✅ | explicit, but cycle-checked; `link_rf_dependency` tool |
| Drift detection | ✅ | body_hash + signature_hash on every symbol; `list_docs(only_stale=True)` |
| **Generated docs content** | ❌ on-demand | `generate_docs` requires either an LLM-capable caller (`content=...`) or an MCP host that supports sampling. There is no auto-regenerate-on-drift mode (deferred to v0.7+) — drift is only *detected*, not *fixed*. |

So: traceability is live, docs are not. If your workflow is "give me an
agent that always knows which code implements which requirement, and which
tests probably break when X changes" — this is exactly what the project is
good at. If you wanted "writes my doc comments while I sleep" — not yet.

## Stack

- **FastMCP 2.14** (stdio transport)
- **SQLite** (single `docs.db` file, ACID, WAL, explicit migration framework)
- **tree-sitter + tree-sitter-language-pack** for multi-language parsing
- **Python `ast`** for high-precision Python extraction
- **NetworkX** for call graph and topological impact analysis (cached per
  index run)
- BM25 keyword search out of the box; embeddings (Jina code + multilingual-e5)
  are an optional `[embeddings]` extra (Phase 4)

100% local, zero external services, zero API keys required.

## Language support

Honest table — only languages with a passing test suite are claimed.

| Language | Status | What's covered |
|----------|--------|----------------|
| **Python** | ✅ Tested | Functions, classes, methods, decorators, calls — uses `ast` for full precision. Imports drive scoped resolution (P0.4). |
| **Go** | ✅ Tested | Functions, struct types via `type_spec`, struct methods, calls. **Scoped resolution** via `import` + alias (P1.A2 v0.4). |
| **Java** | ✅ Tested | Classes, methods, calls (`method_invocation`) |
| **JavaScript** | ✅ Tested | Function declarations, **arrow functions** assigned to const/let, classes, methods. **Scoped resolution** via ES6 `import` and CommonJS `require` (P1.A1 v0.4). |
| **TypeScript** | ✅ Tested | Same as JS plus typed signatures (`.ts` and `.tsx`). **Scoped resolution** via ES6 `import` (P1.A1 v0.4). |
| **Rust** | ✅ Tested | Free functions, struct/enum types, **`impl` block methods** as `Type::method`, traits. **Scoped resolution** via `use` declarations (P4.A3 v0.5). |
| **Ruby** | ✅ Tested | `def`, `class`, `module`, `singleton_method`, calls. Best-effort scoped resolution via `require_relative` + receiver field (P1.A4 v0.4). |
| **PHP** | ✅ Tested | Classes, methods, function/method/scoped call expressions. Best-effort scoped resolution via `use Namespace\X` for `Class::method()` (P1.A4 v0.4); instance-method calls are not scoped. |
| C, C++, C#, Kotlin, Swift, Scala | ⚠️ Untested | The generic tree-sitter extractor will *attempt* to parse these (they're listed in `EXT_LANGUAGE`) but no test suite covers them. Symbol coverage may be partial — open an issue with a fixture if you need a specific language hardened. |

The extractor is a heuristic over hardcoded tree-sitter node types
(`_DEF_NODE_TYPES`, `_CALL_NODE_TYPES` in `extractors.py`); it intentionally
trades completeness for simplicity. Use the per-language tests in
`tests/test_extractors.py` as the contract.

## Install

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Run as MCP server

```bash
livespec-mcp
```

By default it picks the **current working directory** as workspace, or
`LIVESPEC_WORKSPACE` if set. Persistent state lives in `.mcp-docs/docs.db`.

### Claude Code / Cursor wiring

```json
{
  "mcpServers": {
    "livespec": {
      "command": "uv",
      "args": ["--directory", "/path/to/livespec-mcp", "run", "livespec-mcp"],
      "env": { "LIVESPEC_WORKSPACE": "/path/to/your/project" }
    }
  }
}
```

## Tools (39)

Every tool accepts an optional `workspace: str` argument. When omitted, the
server resolves to `LIVESPEC_WORKSPACE` env var or the current working
directory. The runtime caches one DB connection per workspace (LRU=8), so a
single MCP server instance can serve multiple repos in parallel.

### Indexing
- `index_project(force=False, watch=False, workspace=None)` — walk, parse, persist; `watch=True` also starts the file watcher
- `get_index_status(workspace=None)`
- `list_files(path_glob, language, limit, cursor, workspace=None)`

### Analysis
- `find_symbol(query, kind, limit, workspace=None)`
- `get_symbol_info(identifier, detail, workspace=None)` — `summary` or `full`
- `get_symbol_source(qname, workspace=None)` — body slice only (lighter than `get_symbol_info(detail='full')`). v0.8.
- `who_calls(qname, max_depth=1, workspace=None)` — slim agentic alias of `analyze_impact` backward cone. Returns just the callers list. v0.8.
- `who_does_this_call(qname, max_depth=1, workspace=None)` — forward-direction counterpart of `who_calls`. v0.8.
- `quick_orient(qname, workspace=None)` — composite snapshot: metadata + docstring lead + top-5 callers/callees by PageRank + linked RFs. Replaces 3-4 calls with one when an agent first lands on an unfamiliar symbol. v0.8.
- `get_call_graph(identifier, direction, max_depth, workspace=None)`
- `analyze_impact(target_type, target, max_depth, workspace=None)` — symbol/file/requirement.
  Use `max_depth=1` for a "find references"-style direct callers list.
- `get_project_overview(include_infrastructure=False, workspace=None)` — top symbols by
  PageRank; infra noise (DI helpers, dunders, FastMCP `register` outers, 1-line wrappers)
  filtered by default.
- `git_diff_impact(base_ref="HEAD~1", head_ref="HEAD", max_depth=5, workspace=None)` —
  changed files → impacted callers → affected RFs → suggested test files. The CI/PR-review
  entry point.
- `find_dead_code(include_infrastructure=False, workspace=None)` — symbols with
  zero callers and zero RF links. Skips entry-point paths (`tests/`, `bin/`,
  `scripts/`, `__main__.py`, `manage.py`), implicit entry points (dunders,
  FastMCP `register`, DI helpers), and **framework-decorated handlers**
  (`@route`, `@command`, `@fixture`, `@task`, `@tool`, etc.) by default.
- `audit_coverage(workspace=None)` — RF coverage report: modules without
  direct RF, modules implicitly covered (transitively reached), modules
  truly orphan, RFs without implementation, RFs with avg confidence < 0.7.
- `find_orphan_tests(max_depth=10, workspace=None)` — test functions whose
  forward call cone never reaches a non-test symbol.
- `find_endpoints(framework=None, workspace=None)` — symbols decorated with
  framework entry-point markers. `framework` ∈ {flask, fastapi, click,
  pytest, fastmcp, celery, django, None}.

### Requirements
- `create_requirement(title, ...)`
- `update_requirement(rf_id, ...)`
- `delete_requirement(rf_id)` — cascade-removes rf_symbol links
- `list_requirements(status, module, priority, has_implementation)`
- `link_rf_symbol(rf_id, symbol_qname, relation, confidence, source, unlink)` — link an RF to a code symbol.
- `bulk_link_rf_symbols(mappings)` — batch-link N pairs in one transaction. Returns per-entry result; idempotent. v0.7.
- `link_rf_dependency(parent_rf_id, child_rf_id, kind='requires')` — declare an RF→RF dependency. `kind` ∈ {requires, extends, conflicts}. Cycles rejected at insert time.
- `unlink_rf_dependency(parent_rf_id, child_rf_id, kind=None)` — drop one specific edge or every edge between the pair.
- `get_rf_dependency_graph(rf_id, direction='both', max_depth=5)` — walk the RF dependency graph forward / backward / both.

### Brownfield onboarding (v0.7)
- `propose_requirements_from_codebase(module_depth=2, min_symbols_per_group=3, max_proposals=30, skip_already_covered=True)` — heuristic RF discovery. Groups symbols by qname prefix, ranks by PageRank-weighted score, proposes RF candidates with humanized title + description + suggested_symbols. Pair with `create_requirement` + `bulk_link_rf_symbols` to land accepted proposals.
- `scan_docstrings_for_rf_hints()` — surfaces RF candidates from existing docstrings (first sentence, leading verb). Returns `verb_histogram_top` for noticing dominant action verbs (e.g. 47 'Validates...').

> v0.6 renamed the four tools above for clarity. v0.8 removed the deprecated v0.6 aliases (`link_requirement_to_code`, `link_requirements`, `unlink_requirements`, `get_requirement_dependencies`) — call sites must use the canonical names.
- `get_requirement_implementation(rf_id)`
- `scan_rf_annotations()` — two-level matcher: `@rf:RF-NNN` (1.0) vs verb-anchored (0.7),
  with negation guard. See `domain/matcher.py`. **Auto-runs at the end of every
  `index_project`** so traceability stays fresh.
- `import_requirements_from_markdown(path)` — bulk-create RFs from `## RF-NNN: Title`
  format with `**Prioridad:** alta` / `**Módulo:** auth` metadata. Idempotent.

### Search / RAG
- `search(query, scope, limit)` — hybrid FTS5 + vector (RRF when embeddings present)
- `rebuild_chunks(embed='auto')` — AST-aware chunking; `embed='yes'/'no'/'auto'` controls
  whether vectors are generated when `[embeddings]` extras are installed

### Docs
- `generate_docs(target_type, identifier, content?, max_tokens?)` — three modes:
  caller_supplied / sampling / needs_caller_content. Works in Claude Code
  (caller mode) and Cursor/Desktop (sampling mode).
- `list_docs(target_type, only_stale=False)` — list or surface drifted docs
  (drift triggers on body_hash OR signature_hash mismatch).
- `export_documentation(format, out_subdir)` — markdown or JSON

### Watcher (P2.3 — "living" docs)
- `start_watcher(debounce_seconds=2.0)` — listen for filesystem changes and
  auto-run `index_project` after a quiet window. One watcher per workspace.
- `stop_watcher()`
- `watcher_status()` — events received, reindex runs, last run time

### Migrating from v0.1
| Removed | Use instead |
|---------|-------------|
| `find_references(identifier)` | `analyze_impact(target_type='symbol', target=qname, max_depth=1)` then read `impacted_callers` |
| `suggest_rf_links(rf_id)` | `search(query=<rf.title + rf.description>, scope='code')` and post-filter |
| `embed_pending()` | `rebuild_chunks(embed='yes')` |
| `generate_docs_for_symbol(identifier)` | `generate_docs(target_type='symbol', identifier=...)` |
| `generate_docs_for_requirement(rf_id)` | `generate_docs(target_type='requirement', identifier=rf_id)` |
| `detect_stale_docs(target_type)` | `list_docs(target_type, only_stale=True)` |

## Resources

- `project://overview`
- `project://index/status`
- `project://requirements`
- `project://requirements/{rf_id}`
- `project://files/{path*}`
- `project://symbols/{qname*}`
- `doc://symbol/{qname*}`
- `doc://requirement/{rf_id}`

## Prompts (slash commands)

- `onboard_project`
- `analyze_change_impact`
- `audit_requirement_coverage`
- `extract_requirements_from_module`
- `document_undocumented_symbols`
- `refresh_stale_docs`
- `explain_symbol`

## Tests

```bash
uv run pytest -q
```

In-memory FastMCP `Client(mcp)` so tests run without subprocess or network.

## Roadmap

| Fase | Estado | Contenido |
|------|--------|-----------|
| 0 — Bootstrap | ✅ | FastMCP server, project structure |
| 1 — Indexing | ✅ | tree-sitter + Python AST, file-incremental, call graph |
| 2 — Analysis | ✅ | NetworkX, impact, PageRank |
| 3 — Requirements | ✅ | CRUD + linking + annotation matcher |
| 4 — RAG/Embeddings | ✅ | AST chunking, FTS5, fastembed + sqlite-vec optional, RRF |
| 5 — Doc generation | ✅ | `generate_docs` (dual-mode), drift detect (body+signature), export |
| 6 — Polish | ✅ | 7 prompts, doc:// resources, two-level @rf: matcher with negation guard |
| 7 — v0.2 | ✅ | Multi-tenant state, tool consolidation 25→23, persistent refs, watcher, bench suite |
| 8 — v0.3 | ✅ | Auto-scan post-index, PageRank infra filter, scoped resolution by imports, `git_diff_impact`, embeddings smoke real, Ruby+PHP fixtures, hypothesis property tests, memory bench, GitHub Actions CI, `code://` resource, `delete_requirement`, markdown RF importer |
| 9 — v0.4 | ✅ | Scoped resolution for TS/JS/Go/Ruby/PHP, `find_dead_code` / `audit_coverage` / `find_orphan_tests`, `did_you_mean` on Symbol-not-found errors, watcher `atexit` cleanup, CI venv fix |
| 10 — v0.5 | ✅ | Bug fixes from real-repo demo, decorators as first-class field + `find_endpoints`, RF dependency graph (requires/extends/conflicts) with `analyze_impact` cascade, matcher multi-RF + confidence override + `@not_rf:` negation + golden dataset, Rust `use` scoped resolution |
| 11 — v0.6 | ✅ | Hardening: explicit migration framework, unified error shape, RF link tools renamed, deprecated `use_workspace` removed, Django stress test (40K symbols), graph cache, README pitch reframe |
| 12 — v0.7 | 🚧 | Brownfield onboarding: `propose_requirements_from_codebase`, `bulk_link_rf_symbols`, `scan_docstrings_for_rf_hints`. Pagination on aggregator tools. Rust `pub` visibility-aware dead-code filter. `find_symbol` separator-agnostic |

## Optional: Embeddings

```bash
uv pip install -e ".[embeddings]"
```

Enables `fastembed` (Jina code + multilingual-e5-base) and `sqlite-vec` for the
vector lane in `search`. First run downloads ~600MB of models into
`.mcp-docs/models/`. Without these extras, search still works via FTS5.
