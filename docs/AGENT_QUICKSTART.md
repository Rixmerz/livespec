# Agent Quickstart — livespec-mcp

The brownfield-onboarding flow that 3 sessions of real agent battle-testing
converged on. If you are an AI agent dropped into an unfamiliar repo with
livespec-mcp configured, run this sequence top-down.

> **Full guide (tools + how to comment/link code):** invoke the MCP prompt
> **`agent_playbook`** or read [`AGENT_PLAYBOOK.md`](AGENT_PLAYBOOK.md).

## 1. Cold open

Pass the absolute project path on every call (`PROJECT` = e.g. `/Users/me/over-validator`):

```
index_project(workspace="PROJECT")
get_project_overview(workspace="PROJECT")
```

`index_project` walks the workspace, parses every supported file
(Python, Go, Java, JS, TS, Rust, Ruby, PHP), persists symbols + call
edges, and auto-runs the `@rf:` annotation matcher. It is incremental:
the second call on the same workspace reads `xxh3` content hashes and
re-extracts only changed files.

`get_project_overview` returns languages, top symbols by PageRank
(infrastructure noise filtered out by default), and RF totals. This is
your map of the codebase.

## 2. Find the symbol you care about

```
find_symbol(query="LoginService")
quick_orient(qname="auth.LoginService.authenticate")
```

`find_symbol` is separator-agnostic: it matches `Type::method`,
`Type.method`, and `Type#method` interchangeably. Use it for a quick
"does this name exist? where?".

`quick_orient` is the canonical first-contact composite. One call gets
you metadata, the docstring lead, top-5 callers + top-5 callees by
PageRank, linked RFs, and an `is_entry_point` flag (true for
`@mcp.tool` / `@app.route` / etc. — symbols with zero callers that
aren't dead). It replaces the 3-4 separate calls older agents used to
chain.

## 3. Read code without the metadata noise

```
get_symbol_source(qname="auth.LoginService.authenticate")
```

Body slice only. Lighter than `quick_orient` when you already have the
metadata and just want to read the function.

## 4. Trace impact before changing anything

```
who_calls(qname="auth.LoginService.authenticate", max_depth=2)
who_does_this_call(qname="auth.LoginService.authenticate", max_depth=2)
analyze_impact(target_type="symbol", target="auth.LoginService.authenticate")
```

`who_calls` is the slim backward cone (just callers).
`who_does_this_call` is the forward counterpart.
`analyze_impact` is the wider blast-radius tool: it follows the
transitive call graph and rolls up affected RFs in a single response —
useful when you want *"if I change this, which Functional Requirements
am I touching?"* in one call.

## 5. PR review / impact of a diff

```
git_diff_impact(base_ref="main", head_ref="HEAD")
```

Returns:
- changed files
- impacted callers (transitively)
- affected RFs
- suggested test files

Use this to decide test scope before opening a PR or as a check in CI.

## 6. RF flow on an RF-active codebase

```
list_requirements(has_implementation=True)
get_requirement_implementation(rf_id="RF-042")
audit_coverage()
```

`list_requirements` is the orientation surface — RFs with title,
status, priority, and link count. `get_requirement_implementation`
answers the README's headline question
*"¿qué código implementa el RF-042?"* in one round-trip.

`audit_coverage` is the macro view: which modules have RFs, which are
truly orphan, which RFs lack implementation, and which have low avg
confidence on their links.

## 7. RF flow on a fresh codebase (brownfield discovery)

```
propose_requirements_from_codebase()
```

Heuristic discovery: groups symbols by qname prefix (configurable
`module_depth`), ranks by PageRank-weighted score, and proposes RF
candidates with humanized title + description + suggested_symbols.

To accept a proposal you need the **`livespec-rf` plugin** visible in your
tool list. After `index_project(workspace=...)`, mutation tools appear when the
DB has RFs; on a **fresh** repo use `LIVESPEC_PLUGINS=rf` (or `=all`) or
reconnect MCP if the host cached ~19 core tools only.

```
create_requirement(title=..., description=..., rf_id=...)        # plugin
bulk_link_rf_symbols(mappings=[{rf_id, symbol_qname}, ...])      # plugin
```

## 8. When something looks wrong

```
find_dead_code()         # symbols with zero callers and zero RF links
find_orphan_tests()      # tests whose call cone never reaches non-test code
find_endpoints()         # framework-decorated handlers
```

`find_dead_code` filters out entry-point paths (`tests/`, `bin/`,
`scripts/`, `__main__.py`, `manage.py`), implicit entry points
(dunders, FastMCP `register`, DI helpers), and framework-decorated
handlers (`@route`, `@command`, `@fixture`, `@task`, `@tool`, etc.) by
default. `__main__` guards, list-stored callbacks (registries,
migration tables), and middleware lifecycle hooks are also recognized
as referenced.

## 9. Common follow-up patterns

Battle-test data shows these 2-call patterns dominate:

- `find_symbol → quick_orient` — first-contact on an unfamiliar name.
- `index_project → get_project_overview` — standard cold open.
- `quick_orient → who_does_this_call` — drill into what a function
  delegates to.
- `quick_orient → get_symbol_source` — read the body after seeing the
  metadata.

If you find yourself making four separate calls (`find_symbol →
get_symbol_info → who_calls → who_does_this_call`), you are on the old
v0.7 path; collapse to `find_symbol → quick_orient`.

## 10. Pagination on aggregator tools

For repos > 30K symbols, pass `summary_only=True` on `audit_coverage`,
`find_dead_code`, `find_orphan_tests`, `find_endpoints`, and
`git_diff_impact` to keep payloads under ~200 KB. `limit` (default
200) + `cursor` paginate; counts stay exact regardless. Triggered by
the Django and warp stress profiles in `bench/run.py --large`.
