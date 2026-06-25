# livespec-mcp — Self-Requirements (RF-001 .. RF-012)

These are livespec-mcp's own functional requirements, dogfooded by the
project on itself. They live here (committed) so they are reproducible
from a fresh clone — the live `.mcp-docs/docs.db` is gitignored and only
exists locally.

## Regeneration flow (for a cloner)

1. **Index the project** — populate symbols, edges and the call graph:

   ```
   index_project(workspace="/abs/path/to/livespec-mcp")
   ```

2. **Recreate the 12 RF definitions** from this markdown file:

   ```
   import_requirements_from_markdown(path="docs/requirements/livespec-rfs.md")
   ```

   This re-parses RF-001..RF-012 (id, title, description) and upserts them
   into the local `docs.db`. Idempotent — safe to re-run.

3. **Recreate the implements/tests LINKS** from the committed seed
   `docs/requirements/livespec-rf-links.json`:

   ```
   python scripts/apply_rf_links.py --workspace /abs/path/to/livespec-mcp
   ```

   The RF↔symbol links (which functions implement / test each RF) are
   NOT encoded in the RF definitions above — they live in the JSON seed
   as a sorted list of `{"rf_id", "qname", "relation"}` objects. The
   script replays them through `bulk_link_rf_symbols` (one transaction,
   `INSERT OR IGNORE` — idempotent, safe to re-run). After this step a
   fresh clone reproduces both the RF definitions AND their code links
   exactly.

---

## RF-001: Indexing & workspace walk
**Prioridad:** alta · **Módulo:** indexing

Walk the workspace (honouring `.gitignore` and `.livespec.toml`
configuration), detect the languages present per file, and persist the
extracted symbol references into the SQLite store. This is the entry
point for every other capability — nothing downstream works without a
fresh index.

## RF-002: Symbol extraction (9 languages)
**Prioridad:** alta · **Módulo:** extractors

Extract functions, classes and methods — together with their decorators,
annotations and signatures — using tree-sitter for JS/TS/Go/Ruby/PHP/
Rust/Java and the Python `ast` module for Python precision. Supports the
9 languages with passing extractor tests.

## RF-003: Scoped reference resolution
**Prioridad:** alta · **Módulo:** indexer

Resolve call and usage references into `symbol_edge` rows with
import-scoped precision (qualifying names by the imports visible in each
file). Edges are written with `INSERT OR IGNORE` so refs from unchanged
files survive when the files they target change.

## RF-004: Call graph & PageRank
**Prioridad:** media · **Módulo:** graph

Build a NetworkX call graph from the resolved edges, cache it by
`(db_path, project_id, last_run_id)`, and expose PageRank centrality so
agents can rank symbols by structural importance.

## RF-005: RF↔code traceability
**Prioridad:** alta · **Módulo:** requirements

Parse `@rf:` annotations from code and docstrings, support full RF CRUD,
link RFs to symbols, and maintain an RF→RF dependency graph. This is the
differentiator: Functional Requirement ↔ code traceability for serious
software orgs.

## RF-006: Hybrid search & RAG
**Prioridad:** media · **Módulo:** rag

Provide AST-aware chunking of source, full-text search via SQLite FTS5,
and optional dense-vector search via sqlite-vec, fused with Reciprocal
Rank Fusion (RRF) for hybrid retrieval over symbols and requirements.

## RF-007: Dead-code & coverage analysis
**Prioridad:** media · **Módulo:** analysis

Detect unreachable / unused symbols (`find_dead_code`), audit RF coverage
of the codebase (`audit_coverage`), and surface tests that exercise no
linked symbol (`find_orphan_tests`).

## RF-008: Endpoint discovery (framework-aware)
**Prioridad:** media · **Módulo:** analysis

Discover HTTP/route endpoints across 14 frameworks (Flask, FastAPI,
Click, Django, Next.js, Deno Fresh, SvelteKit, Remix, Spring Boot,
Angular, Hono, etc.) via `find_endpoints`, including decorator and alias
detection.

## RF-009: Impact analysis
**Prioridad:** alta · **Módulo:** analysis

Answer "what breaks if I change this?" via `analyze_impact`,
`git_diff_impact`, and the `who_calls` / `who_does_this_call` traversals
over the call graph.

## RF-010: Documentation generation
**Prioridad:** baja · **Módulo:** docs

Generate on-demand documentation (`generate_docs`), list generated docs
(`list_docs`), and export documentation to markdown
(`export_documentation`). Plugin-tier surface, not part of the default
agent toolkit.

## RF-011: Persistence & schema migrations
**Prioridad:** alta · **Módulo:** storage

Bootstrap the SQLite store from a single-file schema and apply an
append-only, ordered migration framework (monotonic version numbers,
never reused or reordered) so user databases upgrade safely across
releases.

## RF-012: Headless CLI
**Prioridad:** baja · **Módulo:** cli

Provide a headless `livespec-mcp index` / `livespec-mcp status` entry
point that shares the same indexing pipeline as the MCP server, for use
in CI or scripted environments without an MCP host.
