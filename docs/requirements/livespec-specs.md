# livespec — Self-Specs (dogfood)

Canonical authoring is **OpenSpec** under [`openspec/specs/`](../../openspec/specs/).
Each requirement keeps a stable id via `<!-- livespec:id=SPEC-NNN -->` so the
committed link seed [`livespec-spec-links.json`](./livespec-spec-links.json)
keeps working.

This file is **legacy import-compat** only (the old `## SPEC-NNN:` catalog).
Prefer `sync_openspec` / the OpenSpec tree for new work.

## Regeneration flow (for a cloner)

1. **Index the project**

   ```
   index_project(workspace="/abs/path/to/livespec-mcp")
   ```

2. **Import Spec definitions** from the OpenSpec tree:

   ```
   sync_openspec(openspec_dir="openspec")
   ```

   Equivalent one-shot import:

   ```
   import_specs_from_markdown(path="openspec", fmt="openspec")
   ```

   Legacy fallback (same SPEC-001..SPEC-013 content):

   ```
   import_specs_from_markdown(path="docs/requirements/livespec-specs.md", fmt="livespec")
   ```

3. **Recreate Spec↔symbol links** from the seed:

   ```
   python scripts/apply_spec_links.py --workspace /abs/path/to/livespec-mcp
   ```

---

## SPEC-001: Indexing & workspace walk
**Prioridad:** alta · **Módulo:** indexing

Walk the workspace (honouring `.gitignore` and `.livespec.toml`
configuration), detect the languages present per file, and persist the
extracted symbol references into the SQLite store. This is the entry
point for every other capability — nothing downstream works without a
fresh index.

## SPEC-002: Symbol extraction (9 languages)
**Prioridad:** alta · **Módulo:** extractors

Extract functions, classes and methods — together with their decorators,
annotations and signatures — using tree-sitter for JS/TS/Go/Ruby/PHP/
Rust/Java and the Python `ast` module for Python precision. Supports the
9 languages with passing extractor tests.

## SPEC-003: Scoped reference resolution
**Prioridad:** alta · **Módulo:** indexer

Resolve call and usage references into `symbol_edge` rows with
import-scoped precision (qualifying names by the imports visible in each
file). Edges are written with `INSERT OR IGNORE` so refs from unchanged
files survive when the files they target change.

## SPEC-004: Call graph & PageRank
**Prioridad:** media · **Módulo:** graph

Build a NetworkX call graph from the resolved edges, cache it by
`(db_path, project_id, last_run_id)`, and expose PageRank centrality so
agents can rank symbols by structural importance.

## SPEC-005: Spec↔code traceability
**Prioridad:** alta · **Módulo:** specs

Parse `@spec:` annotations from code and docstrings, support full Spec
CRUD, link Specs to symbols, and maintain a Spec→Spec dependency graph.
This is the differentiator: Functional Requirement (and other spec kinds)
↔ code traceability for serious software orgs.

## SPEC-006: FTS search (AST chunks)
**Prioridad:** media · **Módulo:** rag

Provide AST-aware chunking of source and full-text search via SQLite
FTS5 over symbols and specs. (Dense-vector / sqlite-vec lane removed in v0.29.)

## SPEC-007: Dead-code & coverage analysis
**Prioridad:** media · **Módulo:** analysis

Detect unreachable / unused symbols (`find_dead_code`), audit Spec
coverage of the codebase (`audit_coverage`), and surface tests that
exercise no linked symbol (`find_orphan_tests`).

## SPEC-008: Endpoint discovery (framework-aware)
**Prioridad:** media · **Módulo:** analysis

Discover HTTP/route endpoints across Flask, FastAPI, Click, Django, Next.js,
Deno Fresh, SvelteKit, Remix, Spring Boot, Angular, Express, Hono, and related
call-style routers via `find_endpoints` (Express+Hono included in the default
sweep since v0.29).

## SPEC-009: Impact analysis
**Prioridad:** alta · **Módulo:** analysis

Answer "what breaks if I change this?" via `analyze_impact`,
`git_diff_impact`, and the `who_calls` / `who_does_this_call` traversals
over the call graph.

## SPEC-010: Documentation generation
**Prioridad:** baja · **Módulo:** docs

Generate on-demand documentation (`generate_docs`), list generated docs
(`list_docs`), and export documentation to markdown
(`export_documentation`). Plugin-tier surface, not part of the default
agent toolkit.

## SPEC-011: Persistence & schema migrations
**Prioridad:** alta · **Módulo:** storage

Bootstrap the SQLite store from a single-file schema and apply an
append-only, ordered migration framework (monotonic version numbers,
never reused or reordered) so user databases upgrade safely across
releases.

## SPEC-012: Headless CLI
**Prioridad:** baja · **Módulo:** cli

Provide a headless `livespec-mcp index` / `livespec-mcp status` entry
point that shares the same indexing pipeline as the MCP server, for use
in CI or scripted environments without an MCP host.

## SPEC-013: OpenSpec (Fission-AI) interoperability
**Prioridad:** media · **Módulo:** specs

Be a first-class citizen of an OpenSpec `openspec/` repo: import a whole
tree (canonical `specs/` requirements plus `changes/` and `archive/`
proposals) via `sync_openspec`, model scenarios (`#### Scenario:`) and
change deltas (ADDED/MODIFIED/REMOVED/RENAMED) as first-class rows,
validate structural rules (`validate_openspec`, mirroring
`openspec validate --strict`), apply the change lifecycle
(`apply_spec_change`/`archive_spec_change`, RENAMED migrating traceability),
trace code to individual scenarios (`link_scenario_symbol`), and write the
tree back out (`export_openspec`) — closing the round-trip.
