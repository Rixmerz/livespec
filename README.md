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
- **tree-sitter + tree-sitter-language-pack** (Python, TS, JS, Go, Java, Rust, …)
- **Python `ast`** for high-precision Python extraction
- **NetworkX** for call graph and topological impact analysis
- BM25 keyword search out of the box; embeddings (Jina code + multilingual-e5)
  are an optional `[embeddings]` extra (Phase 4)

100% local, zero external services, zero API keys required.

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

## Tools

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
- `link_requirement_to_code(rf_id, symbol_qname, relation, confidence)`
- `get_requirement_implementation(rf_id)` — symbols + files + coverage
- `scan_rf_annotations()` — auto-links via `@rf:RF-NNN` in docstrings

### Search
- `search(query, scope, limit)` — BM25 over symbols + RFs

## Resources

- `project://overview`
- `project://index/status`
- `project://requirements`
- `project://requirements/{rf_id}`
- `project://files/{path*}`
- `project://symbols/{qname*}`

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
| 4 — RAG/Embeddings | scaffold | fastembed + sqlite-vec + cAST chunking |
| 5 — Doc generation | TODO | `generate_docs_for_symbol` via MCP sampling |
| 6 — Polish | TODO | LLM re-rank matcher, prompts, watcher |
