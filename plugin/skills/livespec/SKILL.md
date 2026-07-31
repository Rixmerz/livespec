---
name: livespec
description: >-
  Operate the livespec MCP server on any codebase: index, orient, trace the call
  graph, run impact/blast-radius analysis, find likely-unused HTTP flows across a
  polyrepo group_db, and maintain bidirectional Spec<->code traceability (FRs,
  ADRs, NFRs). Use whenever the user asks "what calls this?", "what breaks if I
  change X?", "what code implements SPEC-NNN?", "what Specs touch this file?",
  "which flows/routes look unused?", or mentions livespec, the Spec Explorer,
  Flow Explorer, or code-intelligence indexing.
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

## Polyrepo / `group_db` (cross-repo HTTP)

Several sibling repos can share one SQLite via `.livespec.toml`:

```toml
[workspace]
group_db = "../.livespec-group/flow-group.db"
```

Call tools from **any** member workspace (usually the hub/composer). Then:

| Intent | Tool |
|--------|------|
| Client → server hops | `who_does_this_call` → `invokes_endpoints` |
| Server → client hops | `who_calls` → `route_callers` |
| Symbol in another repo | `find_symbol` / `get_symbol_source` / `quick_orient` (group-wide lookup) |
| Likely-unused HTTP flows | `find_legacy_flows(summary_only=True)` — then full list |
| Flow UI bundle | `export_flow_explorer` (docs plugin) |

**`find_legacy_flows` traps (critical):**

- Evidence is the **static graph**, not production traffic. Payload
  `confidence` is usually `low`. **Never tell the user to delete code** from
  this tool alone — say "candidate; confirm with APM/logs".
- `legacy_server` = indexed server route with no client hop in this DB.
- `orphan_client` = client call with no matching server in this DB — often a
  **missing SA/repo** outside the group, not dead code.
- Infra paths (`/health`, `/metrics`, `/api-docs`, `/ui`, …) are filtered by
  default (`include_infra=False`).
- Spring endpoints: call with `workspace=<java-repo>` (or use the
  `group_java_projects` hint). Hub TS workspaces return Spring `count: 0`.

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
| Keyword search over chunks | `search(query, scope)` — FTS5 over AST-aware symbol + Spec chunks |
| Literal string search over indexed files | `grep_in_indexed_files(pattern)` — check `scope_fresh`, see the staleness trap below |
| Dead-code candidates | `find_dead_code()` — respects entry points / `pub` / frameworks; TS-only auto-enables `include_non_python` |
| Likely-unused HTTP flows | `find_legacy_flows()` — see polyrepo section; graph ≠ traffic |
| Orphan tests | `find_orphan_tests()` — check `test_files_count` / `test_function_symbols` + `hint` (Jest anonymous `test()` → honest zero) |
| HTTP/CLI entry points | `find_endpoints(framework=None)` — see the Hono trap below; prefer `summary_only=True` if JSON is huge |
| Project snapshot | `get_project_overview()` — test-file symbols are excluded from `top_symbols` |
| Static Spec Explorer bundle | `export_explorer(base?, head?, framework?)` — docs plugin; unlock with `LIVESPEC_PLUGINS=docs` or `index_project(explorer=True)` |

**`find_endpoints` — Hono / Express.** Call-style `router.get/post` routes
are included in the **default** sweep (`framework=None`). Pass
`framework="express"` or `"hono"` to filter to one framework. Receivers like
`app` / `router` / `*Router` count (axios/cache/headers `.get` are ignored).

**What counts as a test file.** `find_orphan_tests`, the auto-derived Spec test
coverage in `audit_coverage`, and the `top_symbols` filter in
`get_project_overview` all share one detector. It matches a `tests` / `test` /
`__tests__` / `spec` **path segment**, a `test_` basename prefix, a `_test.`
basename infix, and the `.test.` / `.spec.` / `_spec.` basename suffixes for
`.ts .tsx .js .jsx .mjs .cjs` plus `_spec.rb`. Matching is segment- and
suffix-anchored, so `src/contest/`, `src/latest.ts`, `src/protest.ts` are not
tests. `specs/` (plural — the OpenSpec/docs convention) is deliberately NOT a
test segment.

**`audit_coverage` — two test counts, two mechanisms.** `counts` carries
`specs_with_linked_tests` (Specs with ≥1 explicit `relation='tests'` link) and
`specs_with_derived_test_coverage` (Specs whose auto-derived call-graph ratio is
> 0). They measure different things and can legitimately disagree — that is not
corrupt data. They were formerly `specs_with_test_coverage` /
`specs_with_any_test_coverage`, whose names read as contradictory.

**`get_project_overview` — test scaffolding is filtered.** Symbols in test files
(`createMockDb`, `signTestToken`, `fakeAuthMiddleware`) rank high by PageRank but
answer the wrong question. They are dropped from `top_symbols` and reported by
qualified name in `test_symbols_filtered` (those that outranked the last returned
entry — not an exhaustive census of test symbols).

**`grep_in_indexed_files` — the stale-index trap.** It only reads files the
index knows about, so an edit or a brand-new file since the last
`index_project` can hide a real match behind a clean `count: 0`. Every response
now carries **`scope_fresh`**:

- `scope_fresh: true` → the covered files are byte-identical to what was
  indexed and no unindexed file falls in scope. An empty `matches` genuinely
  means "no matches".
- `scope_fresh: false` → the payload adds `stale_files` / `stale_files_count`
  (indexed files whose bytes changed — still searched, but the index no longer
  describes them) and/or `unindexed_files` / `unindexed_files_count` (files
  present on disk that were **never searched at all**), plus a `hint`. Path
  lists are capped at 20; the `_count` fields carry the true magnitude.

Do **not** conclude "the pattern does not occur" from a `scope_fresh: false`
result — run `index_project(workspace=..., force=false)` and re-grep. The
verdict is bounded by `path_glob`/`kind`: it describes the searched scope, not
the whole index (files outside the scope can't affect the result). The
changed-file half is free (it re-hashes bytes already read); the never-indexed
half costs one workspace walk per call, so a narrow `path_glob` grep pays a
fixed cost it doesn't otherwise need.

### Spec traceability (agentic, always available)

| Intent | Tool |
|--------|------|
| What Specs exist? | `list_specs(status?, module?, kind?, has_implementation?)` |
| What implements SPEC-NNN? | `get_spec_implementation(spec_id)` — one round-trip |
| Coverage gaps / orphans | `audit_coverage(summary_only=True)` |
| Brownfield Spec proposals | `propose_specs_from_codebase()` — skips groups with **any** Spec link by default (`skipped_covered_count`); **user approves before create** |
| Batch link (configs, SQL, no-annotation langs) | `bulk_link_spec_symbols(mappings)` |
| Import specs from markdown | `import_specs_from_markdown(path, fmt?)` — `fmt` is `"livespec"` \| `"openspec"`, auto-detected |
| Which `@word:` comments the matcher ignores | `scan_annotation_verbs()` |

**`bulk_link_spec_symbols` parameter trap.** Each mapping entry is
`{"spec_id": ..., "symbol_qname": ...}` — **`spec_id`, not `rf_id`**, and
`symbol_qname` is **singular**. Passing `rf_id` fails per-entry with the cryptic
`"spec_id and symbol_qname are required"`. Optional per entry: `relation`
(`implements`\|`tests`\|`references`), `confidence`, `source`. `symbol_qname` must
name an indexed *function/method* — a test *module* is not a symbol and will fail lookup.

### OpenSpec authoring (preferred SSoT) + engine round-trip

**Author in OpenSpec** (`openspec/specs/<capability>/spec.md` with
`### Requirement:` + `#### Scenario:` + SHALL/MUST). Livespec is the
code-graph / Spec↔code engine **beneath** that markdown — it does not invent a
competing authoring dialect for new work.

| Intent | Tool |
|--------|------|
| Ingest hand-authored `openspec/` tree | `sync_openspec(openspec_dir?)` — **read the warning below** |
| Ingest one markdown file | `import_specs_from_markdown(path, fmt="auto")` — sniffs OpenSpec vs legacy |
| Structural check (`openspec validate`) | `validate_openspec(strict=False)` — every requirement needs ≥1 scenario |
| Store → markdown (engine dump) | `export_openspec(out_dir="openspec", include_changes=True)` |
| List change proposals | `list_spec_changes(status?)` — `proposed`\|`applied`\|`archived` |
| Read one change package | `get_spec_change(name)` — proposal/design/tasks + delta requirements |

Legacy catalogs (`## SPEC-NNN:` headers) still import via
`import_specs_from_markdown(..., fmt="livespec")` or `fmt="auto"`. Prefer migrating
those files to OpenSpec rather than keeping two dialects in one repo.

> **`sync_openspec` can duplicate your whole store — do not run it on a tree you
> just `export_openspec`'d.** Export rewrites titles into OpenSpec shape; re-sync
> then slugifies those titles into **new** `spec_id`s (one real run: 176 → 358).
> Use `sync_openspec` only on trees **authored** as OpenSpec. For a single
> hand-written OpenSpec file use
> `import_specs_from_markdown(path="openspec/specs/<cap>/spec.md", fmt="auto")`
> (or `fmt="openspec"`). Never force `fmt="livespec"` on `### Requirement:` files.

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

`generate_docs`, `list_docs`, `export_documentation`, `export_explorer`,
`export_flow_explorer` — human-facing Markdown + static Explorer bundles.
Unlock with Spec/docs rows, on-disk explorer bundle, or `LIVESPEC_PLUGINS=docs|all`.

## Pagination contract

Aggregators (`find_dead_code`, `audit_coverage`, `find_orphan_tests`, `find_endpoints`,
`find_legacy_flows`, `git_diff_impact`) accept `limit` (default 200) + `cursor` +
`summary_only=False`. Counts are always exact regardless of pagination. If a
payload comes back with `payload_warning`, switch to `summary_only=True` and paginate.

## `@spec:` annotations (keeping links live)

Comment code so links survive re-indexing. The matcher supports multi-Spec, confidence
override, `@not_spec` negation, and verb-anchored level-2 matches. After bulk doc edits,
run `scan_spec_annotations()` (also runs automatically at the end of `index_project()`).

For the full annotation grammar and examples, fetch the MCP prompt `agent_playbook`.

## Typical task loop

1. **Orient** — `get_project_overview`; index if the DB is stale/missing.
2. **Locate** — `find_symbol` → `quick_orient`.
3. **Assess** — `analyze_impact` / `who_calls` before touching anything risky.
4. **Cross-repo flows** (if `group_db`) — `find_legacy_flows(summary_only=True)`;
   classify orphan clients as missing-SA vs candidate-dead before recommending work.
5. **Specs** (if adopted) — `list_specs`, `get_spec_implementation`, `audit_coverage(summary_only=True)`.
6. **After edits** — `git_diff_impact(summary_only=True)`; re-`index_project` if
   `hint` says unindexed/non-code paths or results look empty.

## Do not

- Do not index a parent-of-many-repos directory.
- Do not create Specs without user approval (`propose_specs_from_codebase` is a suggestion).
- Do not rely on the watcher while actively editing — re-index on demand.
- Do not expect spec-mutation tools on a spec-less repo without `LIVESPEC_PLUGINS`.
- Do not run `sync_openspec` on a tree produced by `export_openspec` — it duplicates
  every spec. Ingest hand-authored OpenSpec with `sync_openspec` or
  `import_specs_from_markdown(..., fmt="auto"|"openspec")`.
- Do not author new Specs as `## SPEC-NNN:` when the repo can use OpenSpec —
  OpenSpec markdown is the preferred authoring SSoT; native headers are legacy import.
- Do not mix OpenSpec `### Requirement:` and native `## SPEC-NNN:` catalogs in the
  same repo (duplicate ids / noisy validate).
- Do not treat `find_legacy_flows` / `find_dead_code` as proof code is unused in
  production — graph evidence only; confirm with traffic before delete.
- Do not treat `orphan_client` as dead code when the SA/repo is simply outside
  the indexed `group_db`.
- Do not call `embed_chunks` / `agent_scratch*` — removed (FTS5-only search;
  scratch dropped from the surface).
