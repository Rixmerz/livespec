---
name: livespec
description: >-
  Operate the livespec-mcp server on any codebase: index, orient, trace the call
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

- Pass the single repo root, e.g. `workspace="/Users/me/over-validator"`.
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
| HTTP/CLI entry points | `find_endpoints(framework?)` — prefer `summary_only=True` if JSON is huge |
| Project snapshot | `get_project_overview()` |

### Spec traceability (agentic, always available)

| Intent | Tool |
|--------|------|
| What Specs exist? | `list_specs(status?, module?, kind?, has_implementation?)` |
| What implements SPEC-NNN? | `get_spec_implementation(spec_id)` — one round-trip |
| Coverage gaps / orphans | `audit_coverage(summary_only=True)` |
| Brownfield Spec proposals | `propose_specs_from_codebase()` — heuristic; **user approves before create** |
| Batch link (configs, SQL, no-annotation langs) | `bulk_link_spec_symbols(mappings)` |
| Import specs from markdown | `import_specs_from_markdown(...)` |

### Spec mutation — `livespec-spec` plugin (operator; plugin-gated)

`create_spec`, `update_spec`, `delete_spec`, `link_spec_symbol`, `link_spec_dependency`,
`unlink_spec_dependency`, `get_spec_dependency_graph`, `scan_spec_annotations`,
`scan_docstrings_for_spec_hints`.

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
