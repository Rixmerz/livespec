---
name: livespec
description: >-
  Operate the livespec MCP server on any codebase: index, orient, trace the call
  graph, run impact/blast-radius analysis, and maintain bidirectional Spec<->code
  traceability (FRs, ADRs, NFRs). Use whenever the user asks "what calls this?",
  "what breaks if I change X?", "what code implements SPEC-NNN?", "what Specs touch
  this file?", or mentions livespec, the Spec Explorer, or code-intelligence indexing.
---

# Livespec — code intelligence & Spec traceability

Livespec is a local-first MCP server that maintains a live call graph, Spec<->code
links, and on-demand docs for a repo. It speaks 9 languages (Python, Go, Java, JS,
TS, Rust, Ruby, PHP + scoped resolution) and is framework-aware (Flask, FastAPI,
Click, pytest, FastMCP, Celery, Django, Next.js, Deno Fresh, SvelteKit, Remix,
Spring Boot, Angular, Hono).

## The one non-negotiable rule: `workspace`

Every tool takes a **required** `workspace` parameter — the absolute path of the
repo the user is editing *in this conversation*. There is **no** `LIVESPEC_WORKSPACE`
env var and **no** cwd fallback; omitting it returns a shaped `mcp_error`.

- Pass the single repo root, e.g. `workspace="/Users/me/sample-api"`.
- **Anti-pattern:** never index a parent folder that holds many repos (`/Users/me/my`).
- Switch repos by changing `workspace=` only.

State lives at `<repo>/.mcp-docs/docs.db` — safe to delete to reset; never commit secrets there.

## Cold open — every new session / after big pulls

```
index_project(workspace="REPO")
get_project_overview(workspace="REPO")
list_specs(workspace="REPO")
```

Then report: file/symbol/edge counts, languages, top symbols, Spec totals.
`index_project` is content-hash incremental — re-run it on demand (after pulls/edits),
it is not a background watcher you should lean on while editing.

## Tool map — what to call when

### Code intelligence (always available)

| Intent | Tool |
|--------|------|
| First contact on a symbol | `quick_orient(qname)` — replaces 3-4 older calls, includes `is_entry_point` |
| Name lookup (separator-agnostic `::` `.` `#`) | `find_symbol(query, kind, limit)` |
| Read a body only | `get_symbol_source(qname)` |
| Who calls this? | `who_calls(qname, max_depth=1)` — `summary_only=True` on huge repos |
| What does this call? | `who_does_this_call(qname, max_depth=1)` |
| Blast radius + Spec rollup | `analyze_impact(target_type, target, max_depth)` — `symbol`\|`file`\|`spec` |
| PR / diff scope | `git_diff_impact(base_ref, head_ref)` — git repos only |
| Semantic + lexical grep | `search(query, scope)` — FTS5 + optional vectors |
| Literal string search over indexed files | `grep_in_indexed_files(pattern)` |
| Dead-code candidates | `find_dead_code()` — respects entry points / `pub` / frameworks |
| Orphan tests | `find_orphan_tests()` |
| HTTP/CLI entry points | `find_endpoints(framework=None)` — see the Hono trap below; prefer `summary_only=True` if JSON is huge |
| Project snapshot | `get_project_overview()` |
| Vector embeddings for `search` | `embed_chunks()` — backfills any unembedded chunks |
| Static Spec Explorer bundle | `export_explorer(base?, head?, framework?)` |
| Scratch note on a symbol | `agent_scratch(qname, note)` / `agent_scratch_get(qname)` / `agent_scratch_clear(qname?)` |

**`find_endpoints` — the Hono trap.** `framework` *is* optional, but Hono is
**explicit-opt-in** and is excluded from the `framework=None` sweep (it reads
files on demand). On a Hono repo the default call returns **`count: 0`**, which
reads as "no routes indexed" — pass `framework="hono"` to actually get them. When
the sweep returns 0 and Hono files are present, the payload carries
`not_swept: ["hono"]` plus a `hint` — read it before concluding a repo has no
routes. All other frameworks (flask, fastapi, click, pytest, fastmcp, celery,
django, nextjs, fresh, sveltekit, remix, spring, angular) *are* in the default sweep.

### Spec traceability (agentic, always available)

| Intent | Tool |
|--------|------|
| What Specs exist? | `list_specs(status?, module?, kind?, has_implementation?)` |
| What implements SPEC-NNN? | `get_spec_implementation(spec_id)` — one round-trip |
| Coverage gaps / orphans | `audit_coverage(summary_only=True)` |
| Brownfield Spec proposals | `propose_specs_from_codebase()` — heuristic; **user approves before create** |
| Batch link (configs, SQL, no-annotation langs) | `bulk_link_spec_symbols(mappings)` |
| Import specs from markdown | `import_specs_from_markdown(path, fmt?)` — `fmt` is `"livespec"` \| `"openspec"`, auto-detected |
| Which `@word:` comments the matcher ignores | `scan_annotation_verbs()` |

**`bulk_link_spec_symbols` parameter trap.** Each mapping entry is
`{"spec_id": ..., "symbol_qname": ...}` — **`spec_id`, not `rf_id`**, and
`symbol_qname` is **singular**. Passing `rf_id` fails per-entry with the cryptic
`"spec_id and symbol_qname are required"`. Optional per entry: `relation`
(`implements`\|`tests`\|`references`), `confidence`, `source`. `symbol_qname` must
name an indexed *function/method* — a test *module* is not a symbol and will fail lookup.

### OpenSpec round-trip (Fission-AI interop, always available)

| Intent | Tool |
|--------|------|
| Store → markdown | `export_openspec(out_dir="openspec", include_changes=True)` |
| Markdown → store (whole tree) | `sync_openspec(openspec_dir?)` — **read the warning below** |
| Markdown → store (one file) | `import_specs_from_markdown(path, fmt="livespec")` |
| Structural check (`openspec validate`) | `validate_openspec(strict=False)` — every requirement needs ≥1 scenario |
| List change proposals | `list_spec_changes(status?)` — `proposed`\|`applied`\|`archived` |
| Read one change package | `get_spec_change(name)` — proposal/design/tasks + delta requirements |

> **`sync_openspec` can duplicate your whole store — do not run it on a tree you
> exported.** The openspec dialect derives `spec_id` by **slugifying the
> requirement title**, so it never matches the ids `export_openspec` wrote; and
> `export_openspec` dumps every module-less spec into a single `general`
> capability file. Re-syncing an exported tree therefore re-ingests each spec
> under a fresh slug id — one real run took a store from **176 → 358 specs**. To
> ingest hand-written specs, point at **one file** with
> `import_specs_from_markdown(path="openspec/specs/<cap>/spec.md", fmt="livespec")`
> instead. `sync_openspec` is for a tree you authored by hand, never a round-tripped export.

### Spec mutation — `livespec-spec` plugin (operator; plugin-gated)

`create_spec`, `update_spec`, `delete_spec`, `link_spec_symbol`, `link_scenario_symbol`,
`link_spec_dependency`, `unlink_spec_dependency`, `get_spec_dependency_graph`,
`scan_spec_annotations`, `scan_docstrings_for_spec_hints`, `apply_spec_change`,
`archive_spec_change` (12 — the authoritative list is `SPEC_MUTATION_TOOL_NAMES`
in `livespec_mcp/tools/plugins/__init__.py`).

These appear only once the workspace has spec rows, or `LIVESPEC_PLUGINS=spec` (or `=all`)
is set. On a spec-less repo, run `index_project` first (and reconnect MCP if the client
cached a short tool list) or set the override.

### Docs — `livespec-docs` plugin

`generate_docs`, `list_docs`, `export_documentation` — human-facing Markdown docs.

## Pagination contract

Aggregators (`find_dead_code`, `audit_coverage`, `find_orphan_tests`, `find_endpoints`,
`git_diff_impact`) accept `limit` (default 200) + `cursor` + `summary_only=False`. Counts
are always exact regardless of pagination. If a payload comes back with `payload_warning`,
switch to `summary_only=True` and paginate.

## `@spec:` annotations (keeping links live)

Comment code so links survive re-indexing. The matcher supports multi-Spec, confidence
override, `@not_spec` negation, and verb-anchored level-2 matches. After bulk doc edits,
run `scan_spec_annotations()` (also runs automatically at the end of `index_project()`).

For the full annotation grammar and examples, fetch the MCP prompt `agent_playbook`.

## Typical task loop

1. **Orient** — `get_project_overview`; index if the DB is stale/missing.
2. **Locate** — `find_symbol` → `quick_orient`.
3. **Assess** — `analyze_impact` / `who_calls` before touching anything risky.
4. **Specs** (if adopted) — `list_specs`, `get_spec_implementation`, `audit_coverage(summary_only=True)`.
5. **After edits** — `git_diff_impact(summary_only=True)`; re-`index_project` to refresh.

## Do not

- Do not index a parent-of-many-repos directory.
- Do not create Specs without user approval (`propose_specs_from_codebase` is a suggestion).
- Do not rely on the watcher while actively editing — re-index on demand.
- Do not expect spec-mutation tools on a spec-less repo without `LIVESPEC_PLUGINS`.
- Do not run `sync_openspec` on a tree produced by `export_openspec` — it duplicates
  every spec. Use `import_specs_from_markdown(path=<one file>, fmt="livespec")`.
- Do not read `find_endpoints()` returning 0 as "no routes" on a Hono repo — pass
  `framework="hono"`.
