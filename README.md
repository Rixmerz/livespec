# livespec-mcp

Local-first MCP server for **living documentation** with bidirectional
**Functional Requirement <-> code** traceability.

Index a workspace once, then ask questions like:

- ¿Qué código implementa el RF-042?
- Si modifico la función `auth.verify`, ¿qué RFs y qué llamadores se ven afectados?
- ¿Qué módulos no tienen ningún RF asociado?

## Stack

- **FastMCP 2.14** (stdio transport)
- **SQLite** (single `docs.db` file, ACID, WAL)
- **tree-sitter + tree-sitter-language-pack** for multi-language parsing
- **Python `ast`** for high-precision Python extraction
- **NetworkX** for call graph and topological impact analysis
- BM25 keyword search out of the box; embeddings (Jina code + multilingual-e5)
  are an optional `[embeddings]` extra (Phase 4)

100% local, zero external services, zero API keys required.

## Language support

Honest table — only languages with a passing test suite are claimed.

| Language | Status | What's covered |
|----------|--------|----------------|
| **Python** | ✅ Tested | Functions, classes, methods, decorators, calls — uses `ast` for full precision |
| **Go** | ✅ Tested | Functions, struct types via `type_spec`, struct methods, calls |
| **Java** | ✅ Tested | Classes, methods, calls (`method_invocation`) |
| **JavaScript** | ✅ Tested | Function declarations, **arrow functions** assigned to const/let, classes, methods |
| **TypeScript** | ✅ Tested | Same as JS plus typed signatures (`.ts` and `.tsx`) |
| **Rust** | ✅ Tested | Free functions, struct/enum types, **`impl` block methods** as `Type::method`, traits |
| Ruby, PHP, C, C++, C#, Kotlin, Swift, Scala | ⚠️ Untested | The generic tree-sitter extractor will *attempt* to parse these (they're listed in `EXT_LANGUAGE`) but no test suite covers them. Symbol coverage may be partial — open an issue with a fixture if you need a specific language hardened. |

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

## Tools (24)

### Indexing
- `index_project(force=False)` — walk workspace, parse, persist
- `get_index_status()` — last run, totals, freshness
- `list_files(path_glob, language, limit, cursor)`

### Analysis
- `find_symbol(query, kind, limit)`
- `get_symbol_info(identifier, detail)` — `summary` or `full` (with source body)
- `get_call_graph(identifier, direction, max_depth)`
- `find_references(identifier, limit)`
- `analyze_impact(target_type, target, max_depth)` — symbol/file/requirement
- `get_project_overview()` — languages + top symbols by PageRank

### Requirements
- `create_requirement(title, ...)`
- `update_requirement(rf_id, ...)`
- `list_requirements(status, module, priority, has_implementation)`
- `link_requirement_to_code(rf_id, symbol_qname, relation, confidence, source, unlink)`
- `get_requirement_implementation(rf_id)` — symbols + files + coverage
- `suggest_rf_links(rf_id, limit, min_score)` — propose candidates from hybrid search
- `scan_rf_annotations()` — auto-links via `@rf:RF-NNN` in docstrings

### Search / RAG
- `search(query, scope, limit)` — hybrid FTS5 + vector (RRF when embeddings present)
- `rebuild_chunks()` — AST-aware chunking of symbols and RFs
- `embed_pending()` — fastembed dual-model (code + multilingual text), optional

### Docs
- `generate_docs_for_symbol(identifier, max_tokens)` — via MCP sampling
- `generate_docs_for_requirement(rf_id, max_tokens)` — via MCP sampling
- `detect_stale_docs(target_type)` — drift detection by `body_hash`
- `list_docs(target_type)`
- `export_documentation(format, out_subdir)` — markdown or JSON

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
| 5 — Doc generation | ✅ | `generate_docs_for_symbol/requirement` via MCP sampling, drift detect, export |
| 6 — Polish | ✅ | `suggest_rf_links`, 7 prompts, doc:// resources |
| 7 — Future | — | LanceDB scaling, more languages, watchdog filesystem watcher |

## Optional: Embeddings

```bash
uv pip install -e ".[embeddings]"
```

Enables `fastembed` (Jina code + multilingual-e5-base) and `sqlite-vec` for the
vector lane in `search`. First run downloads ~600MB of models into
`.mcp-docs/models/`. Without these extras, search still works via FTS5.
