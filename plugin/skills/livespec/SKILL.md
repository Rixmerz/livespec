---
name: livespec
description: >-
  Operate the livespec MCP server on any codebase: index, orient, trace the call
  graph, run impact/blast-radius analysis, find likely-unused HTTP flows across a
  polyrepo group_db, and maintain bidirectional Spec<->code traceability (FRs,
  ADRs, NFRs). Use whenever the user asks "what calls this?", "what breaks if I
  change X?", "what code implements auth-user-login?", "what Specs touch this file?",
  "which flows/routes look unused?", or mentions livespec, `livespec ui`,
  browsing indexed projects, or code-intelligence indexing.
---

# Livespec — code intelligence & Spec traceability

Livespec is a local-first MCP server that maintains a live call graph, Spec<->code
links, and on-demand docs for a repo. It speaks 9 languages (Python, Go, Java, JS,
TS, Rust, Ruby, PHP + scoped resolution) and is framework-aware (Flask, FastAPI,
Click, pytest, FastMCP, Celery, Django, Next.js, Deno Fresh, SvelteKit, Remix,
Spring Boot, Angular, Express, Hono).

## The one non-negotiable rule: `workspace`

Every tool takes a **required** `workspace` parameter — the absolute path of the
repo the user is editing *in this conversation*. There is **no** `LIVESPEC_WORKSPACE`
env var and **no** cwd fallback; omitting it returns a shaped `mcp_error`.

- Pass the single repo root, e.g. `workspace="/Users/me/sample-api"`.
- **Anti-pattern:** never index a parent folder that holds many repos (`/Users/me/my`).
- Switch repos by changing `workspace=` only.

State lives at `<repo>/.mcp-docs/docs.db` — safe to delete to reset; never commit secrets there.

## Polyrepo / `group_db` (cross-repo HTTP **and** Specs)

Several sibling repos can share one SQLite via `.livespec.toml`:

```toml
[workspace]
group_db = "../.livespec-group/flow-group.db"
```

**Discover how:** call tool `get_cross_repo_guide`, fetch prompt
`cross_repo_workflow`, or read resources `project://cross-repo` /
`project://group` (`guide://cross-repo` is an alias). Some hosts cache
`resources/list` — prefer the tool if resources look missing.

Call tools from **any** member workspace (usually the hub/composer). Then:

| Intent | Tool / resource |
|--------|-----------------|
| Client → server hops | `who_does_this_call` → `invokes_endpoints` |
| Server → client hops | `who_calls` → `route_callers` |
| Symbol in another repo | `find_symbol` / `get_symbol_source` / `quick_orient` (group-wide lookup) |
| Shared Specs across repos | Mirror Spec ids as `xrepo-*` in **each** repo + `@spec:xrepo-…`; `get_cross_repo_guide` / `list_specs` / `get_spec_implementation`; `project://group` |
| Likely-unused HTTP flows | `find_legacy_flows(summary_only=True)` — then full list |
| Do the repos' schemas of one entity agree? | `schema_drift(entity?)` — every `@schema: <entity>` declaration of the group, field by field; `project://schemas` |
| What breaks in the other repos if I change this field? | `schema_impact(entity, change, field)` — BEFORE the edit: each declaration's action + readers per repo (route callers included) |
| Make that field change in every repo | `plan_schema_change(entity, change, field)` — AFTER `schema_impact`: exact line edits per declaration; plan first, `apply=True` only once the person agreed |
| Human view of the group | `/livespec-ui` (or `livespec ui`) — group page lists repos, cross-repo Specs, route hops; read-only, writes nothing into any repo |

**Cross-repo Specs (`xrepo-*`):** livespec does not invent one global Spec row.
Author the same OpenSpec slug `xrepo-<slug>` in each participating repo
(`sync_openspec` or `create_spec`), annotate code with `@spec:xrepo-…`,
re-index. The `livespec ui` group page unions mirrored ids. Annotation alone does
**not** create the Spec row.

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
| PR / diff scope | `git_diff_impact(base_ref, head_ref)` — git repos only; symbol-precise (an untouched function in a changed file is not listed); a `schema_impact` key appears when a `@schema:` declaration changed |
| Keyword search over chunks | `search(query, scope)` — FTS5 over AST-aware symbol + Spec chunks |
| Literal string search over indexed files | `grep_in_indexed_files(pattern)` — check `scope_fresh`, see the staleness trap below |
| Dead-code candidates | `find_dead_code()` — respects entry points / `pub` / frameworks; TS-only auto-enables `include_non_python` |
| Likely-unused HTTP flows | `find_legacy_flows()` — see polyrepo section; graph ≠ traffic |
| Orphan tests | `find_orphan_tests()` — check `test_files_count` / `test_function_symbols` + `hint` (Jest anonymous `test()` → honest zero) |
| HTTP/CLI entry points | `find_endpoints(framework=None)` — see the Hono trap below; prefer `summary_only=True` if JSON is huge |
| Project snapshot | `get_project_overview()` — test-file symbols are excluded from `top_symbols` |
| Everything needed to change one symbol | `read_unit(qname, depth=1, token_budget=2000)` — body + callee signatures + the types in them + what it raises + covering tests, in one payload |
| Stack trace / file:line → symbol | `resolve_location(path, line)` — the inverse of `read_unit` |
| "Did I just write this twice?" | `search_similar(code or qname)` — before a write; normalised-AST hash + k-gram winnowing, no embeddings |
| Frozen duplication debt | `debt_baseline_capture()` / `debt_baseline_status()` — accepted duplication stays quiet, new copies do not |
| A human wants to *see* the index | Not a tool: tell them to run `/livespec-ui` (Claude Code) or `livespec ui` — one read-only page over every indexed project. Never generate HTML into the repo |

### Tracks — the history of code ↔ spec (`CHANGESPEC.md`)

A **track** is a macro-commit: a named git range (`base..head` — a branch, a
PR, a run of commits) plus *why* it exists. `CHANGESPEC.md` at the repo root
holds them newest first, per Spec: which requirement deltas and which symbols
(added / modified / removed) the range brought, and why. It is versioned with
the code; the index is rebuilt from it on every `index_project`.

| Intent | Tool |
|--------|------|
| "What did this branch / PR change, per Spec?" | `propose_track(base_ref, head_ref)` — draft only, writes nothing; `changed_specs` (OpenSpec delta, or the first `@spec:` of changed code) vs `touched_specs` (reached only through shared code); also lists code changed with **no** Spec (`unlinked`) |
| Record it once the person agreed | `record_track(base_ref, head_ref, title, why, spec_reasons)` — writes `CHANGESPEC.md`; same range again updates, never duplicates |
| "How did this Spec evolve?" | `get_spec_history(spec_id)` — timeline of tracks, each `tier` changed or touched, whys, deltas, symbols |
| "Why does this function look like this?" | `get_symbol_history(symbol)` — tracks that changed it and the Specs involved (removed symbols too) |

The loop is **propose → draft whys → person confirms → record**. Never call
`record_track` with whys the person has not seen: the file is theirs, like a
CHANGELOG. When a PR is ready (or right after an OpenSpec change is archived),
run `propose_track(base_ref=<merge base>, head_ref="HEAD")`, write one line per
changed Spec from the diff and the change's proposal (touched Specs need none;
they go on one `Also touched:` line), show them, then record.

### Schema drift — one entity, many declarations

Schemas stay where they live (Zod / TS types in a frontend, Pydantic models,
dataclasses or TS interfaces in a backend, `CREATE TABLE` in a migration).
Bind each declaration of one entity with a `@schema: <entity>` line — any
spelling, `user-profile` = `UserProfile`:

- Python: a line in the class docstring (`@schema: user`).
- TypeScript / Zod: the leading comment (`/** @schema: user */` above the
  `interface`, `type` or `const X = z.object(...)`).
- SQL: a `-- @schema: user` line right above the `CREATE TABLE`.

It is not a Spec annotation: it creates no Spec link and
`scan_annotation_verbs` does not flag it. Re-run `index_project` after adding
one. In a shared `[workspace] group_db` the declarations of every repository
of the group are compared; without one, only the repository's own.

| Intent | Tool |
|--------|------|
| "Do my frontend, backend and database schemas agree?" | `schema_drift()` — per entity: `missing_field`, `type_mismatch`, `nullability_mismatch`, `optional_mismatch`, each side with project, file, line; `summary_only=True` for counts |
| One entity only | `schema_drift(entity="user")` — unknown names return `did_you_mean` |
| Read drift without paging | resources `project://schemas` (counts) and `project://schemas/{entity}` (every finding) |
| "What breaks if I add / remove / rename / retype this field?" | `schema_impact(entity="user", change="rename", field="email", new_name="contact_email")` — call it BEFORE editing a shared field; `retype` takes `new_type` (a neutral type) |
| "Make this field change in every repo" | `plan_schema_change(entity="user", change="rename", field="email", new_name="contact_email")` — after `schema_impact`; plans by default, `apply=True` only once the person agreed |
| "Which schemas did this diff change, and who reads them?" | `git_diff_impact(base_ref, head_ref)` → `schema_impact` key |

`schema_impact` lists every declaration of the entity in the group with the
`action` the change asks of it: `add`/`present`, `remove`/`absent`,
`rename`/`both_present`/`renamed` (already renamed there)/`absent`,
`retype`/`compatible` (that side's wire type already carries it, e.g. a TS
`string` for `uuid`)/`absent`. Then the `readers` of the declarations that must
change, one row per symbol with project, file, line, `role` (`endpoint` with
`http_method`/`http_path`, `test`, `symbol`), `depth` and `via`: `graph` (the
call cone, up to `max_depth=3`, crossing repos through HTTP routes),
`schema_binding` (bound with `@schema:` but not a schema) or `spec:<id>`
(shares a Spec with a declaration). Readers are per declaration, not per
field. `analyze_impact` does not follow schema links: for a field, this is the
tool. A `hint` about type positions means no call edge reaches the schema (TS
types, Zod and SQL are used as types, not called): search their names with
`grep_in_indexed_files`.

To make the change, call `plan_schema_change` with the same `entity`,
`change`, `field` (`new_name`, `new_type`; `change="optionality"` takes
`optional` and/or `nullable`; `new_type` is one of string, int, float,
decimal, bool, datetime, date, uuid, json). It plans by default and writes
nothing. Every declaration gets a `status`: `edit` with exact `edits` (`file`,
`start_line`, `end_line`, `old_text`, `new_text`; whole lines, 1-based,
inclusive), `none`, `manual` with a `reason` (edit that one by hand), or
`stale` (changed since indexing, missing, or resolving outside its project
root: re-run `index_project`). Names follow each side's convention and a
Pydantic `Field(alias=...)` is renamed through its alias. An edited SQL table
carries a `migration_hint` (`ALTER TABLE ...`): hand it to the person, it is
never written. `readers` are `schema_impact`'s, paged. Show the plan to the
person; only once they agree, call it again with `apply=True` and
`apply_workspaces=[<repo roots>]` (default: the calling workspace). Apply
writes only those projects' files, lists the rest in `apply.not_applied`,
refuses the whole apply (nothing written) when any file it would write
changed since indexing, and does not re-index: run `index_project` on each
root in `apply.reindex`, then `schema_drift(entity)` to confirm.

`git_diff_impact`'s `schema_impact` holds, per entity whose bound schema the
range changed: `changed` (each declaration's `field_changes` base vs head:
`added`, `removed`, `retyped`, `modified`; a rename is removed + added;
`field_changes_unknown` when a side cannot be read), the other `declarations`
with the `actions` those changes ask of them, `readers` (counts per project
plus a 5-per-project `sample`, dropped by `summary_only`) and `follow_up`
`schema_impact(...)` calls. No bound schema changed → no key at all.

Not mismatches by design: `snake_case` vs `camelCase` names, types JSON
carries as strings or one number type (TS `string` vs `UUID` / `datetime`,
`number` vs `int`), a SQL JSON column vs a nested model, and optional against a
SQL column (a DB default is not an omittable field). `uncomparable` counts
fields whose type no rule maps — they were not judged, not found equal. A
binding on a non-schema symbol shows under `not_schemas`. A `hint` about
`group_db` means the entity has one schema in scope: the other repo is not in
the group, or its declaration is not annotated.

### A second extractor (Graphify) — optional, zero LLM

livespec's blind spots are known and specific: **type-position use**
(`def f(x: Base)`), **inheritance** (`class A extends B`), and cross-file calls
the resolver could not disambiguate. None of them produce an edge, so a base
class or an interface used only as a type reads as referenced by nothing.

A second AST extractor has *different* blind spots, which is what makes it
worth reading. [Graphify](https://github.com/Graphify-Labs/graphify)'s code
pass is tree-sitter with **no LLM and no API key** (`graphify update <repo>`
writes `graphify-out/graph.json`; measured `input_tokens: 0`).

| Intent | Tool |
|--------|------|
| Fewer false "dead" / "orphan" verdicts, writing nothing | `find_dead_code(corroborate_with="graphify-out/graph.json")`, same on `find_orphan_tests` |
| Put the missing edges in the call graph for real | `ingest_external_graph(graph_path=..., dry_run=True)` then `dry_run=False` |
| Take them back out | `ingest_external_graph(remove=True)` |

Rules that matter when you use it:

- **The symbol table stays livespec's.** An edge is ingested only when *both*
  endpoints already resolve to livespec symbols. Nothing else is created.
- **`dry_run=True` is the default.** Read `edges_to_add`, `already_known` (the
  cross-validation number) and `sample` before applying.
- **Ingested edges are labelled.** `who_calls` marks depth-1 neighbours with
  `via_external_edge`, and every graph-reading tool carries an
  `external_edges` block naming the origins in play.
- **A type reference is not a caller.** `who_calls` counts invocation edges
  only; ingested `references` / `inherits` rows are reported under
  `excluded_by_edge_type`. Pass `edge_types=["calls","references"]` to include
  them, or use `analyze_impact`, which counts every dependency.
- **Re-run after an `index_project` that changed files.** A re-extract deletes
  the symbols those edges pointed at. The payload says so under
  `external_edges.stale`; `[graph] auto_ingest = true` refreshes automatically.
- **Never treat it as traffic.** Two extractors agreeing is still static
  analysis.

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
| What implements a Spec? | `get_spec_implementation(spec_id)` — one round-trip |
| Coverage gaps / orphans | `audit_coverage(summary_only=True)` |
| Brownfield Spec proposals | `propose_specs_from_codebase()` — skips groups with **any** Spec link by default (`skipped_covered_count`); **user approves before create** |
| Batch link (configs, SQL, no-annotation langs) | `bulk_link_spec_symbols(mappings)` |
| Import specs from markdown | `import_specs_from_markdown(path, fmt?)` — OpenSpec only (`"openspec"` \| `"auto"`) |
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
| Ingest one markdown file | `import_specs_from_markdown(path, fmt="openspec")` — OpenSpec only |
| Structural check (`openspec validate`) | `validate_openspec(strict=False)` — every requirement needs ≥1 scenario |
| Store → markdown (engine dump) | `export_openspec(out_dir="openspec", include_changes=True)` |
| List change proposals | `list_spec_changes(status?)` — `proposed`\|`applied`\|`archived` |
| Read one change package | `get_spec_change(name)` — proposal/design/tasks + delta requirements |

> **`sync_openspec` can duplicate your whole store — do not run it on a tree you
> just `export_openspec`'d.** Export rewrites titles into OpenSpec shape; re-sync
> then slugifies those titles into **new** `spec_id`s (one real run: 176 → 358).
> Use `sync_openspec` only on trees **authored** as OpenSpec. For a single
> hand-written OpenSpec file use
> `import_specs_from_markdown(path="openspec/specs/<cap>/spec.md", fmt="openspec")`.
> The native `## SPEC-NNN:` catalog dialect is removed.

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
Unlock with doc rows or `LIVESPEC_PLUGINS=docs|all`. (The old `export_explorer` /
`export_flow_explorer` bundles are gone; the browsable view is `livespec ui`.)

### Slash commands (plugin)

| Command | What it does |
|---------|--------------|
| `/livespec-onboard [path]` | cold open via the `livespec` subagent: `index_project` → `get_project_overview` → `list_specs` |
| `/livespec-ui` | starts `livespec ui` in the background (one local, read-only web UI over every indexed project, grouped by polyrepo group) and reports its URL and how to stop it |

## Pagination contract

Aggregators (`find_dead_code`, `audit_coverage`, `find_orphan_tests`, `find_endpoints`,
`find_legacy_flows`, `git_diff_impact`, `schema_drift`, `schema_impact`, `plan_schema_change`) accept `limit` (default 200) + `cursor` +
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
3. **Assess** — `analyze_impact` / `who_calls` before touching anything risky;
   `read_unit` when you need the whole contract in one payload.
4. **Cross-repo flows** (if `group_db`) — `find_legacy_flows(summary_only=True)`;
   classify orphan clients as missing-SA vs candidate-dead before recommending work.
5. **Specs** (if adopted) — `list_specs`, `get_spec_implementation`, `audit_coverage(summary_only=True)`.
6. **Before writing a new helper** — `search_similar` on the body you are about
   to add; a hit means edit the existing one instead.
7. **After edits** — `git_diff_impact(summary_only=True)`; re-`index_project` if
   `hint` says unindexed/non-code paths or results look empty.
8. **Before the PR / after archiving a change** — `propose_track` → draft whys →
   person confirms → `record_track`.

## Do not

- Do not index a parent-of-many-repos directory.
- Do not create Specs without user approval (`propose_specs_from_codebase` is a suggestion).
- Do not rely on the watcher while actively editing — re-index on demand.
- Do not expect spec-mutation tools on a spec-less repo without `LIVESPEC_PLUGINS`.
- Do not run `sync_openspec` on a tree produced by `export_openspec` — it duplicates
  every spec. Ingest hand-authored OpenSpec with `sync_openspec` or
  `import_specs_from_markdown(..., fmt="auto"|"openspec")`.
- Do not author Specs as `## SPEC-NNN:` — that dialect is removed; use OpenSpec
  under `openspec/specs/<capability>/spec.md`.
- Do not treat `find_legacy_flows` / `find_dead_code` as proof code is unused in
  production — graph evidence only; confirm with traffic before delete.
- Do not treat `orphan_client` as dead code when the SA/repo is simply outside
  the indexed `group_db`.
- Do not call `embed_chunks` / `agent_scratch*` — removed (FTS5-only search;
  scratch dropped from the surface).
- Do not read `who_calls` as complete on a repo whose `index_project` reported
  `languages_failed` — those files were never parsed, so their callers do not
  exist in the graph at all. Follow `parser_errors[].hint` (a proxy's CA in
  `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`, or `livespec grammars` once with
  network access) and re-index. A top-level `warning` on `index_project`
  means a language had files but 0 symbols — say so, never report it clean.
- Do not report a `who_calls` count as "callers" when the payload carries
  `excluded_by_edge_type` without saying what was excluded.
- Do not run `ingest_external_graph(dry_run=False)` on someone's index without
  telling them: it writes rows into `symbol_edge`. `remove=True` undoes it
  exactly.
