# Cross-repo Specs + HTTP (`group_db`)

**Audience:** AI agents with the livespec MCP. Read this when the user has
several sibling repos, asks about end-to-end flows, or mentions `xrepo-*`.

Also exposed as MCP prompt `cross_repo_workflow` and resource `guide://cross-repo`.
Live membership for the MRU workspace: resource `project://group`.

---

## What “cross-repo” means here

Two complementary layers share one SQLite when configured:

| Layer | How | Tools / UI |
|-------|-----|------------|
| **HTTP hops** | Client `requests`/`fetch` → server route | `who_does_this_call` → `invokes_endpoints`; `who_calls` → `route_callers`; `find_legacy_flows` |
| **Shared Specs** | Same Spec id `xrepo-*` in **each** participating repo | `list_specs` / `get_spec_implementation`; Flow Explorer; `export_flow_explorer` |

livespec does **not** invent a single global Spec row. You **mirror** the same
`xrepo-<slug>` id (and usually the same OpenSpec capability) in each repo that
implements a slice of the flow, then annotate code with `@spec:xrepo-…`.

---

## 1. Wire the group (once per polyrepo)

In **each** sibling repo’s `.livespec.toml`:

```toml
[workspace]
group_db = "../.livespec-group/flow-group.db"
```

Path is relative to that repo root (or absolute). Same file for every member.

Then index from any member (usually the hub/composer):

```
index_project(workspace="/abs/path/repo-a")
index_project(workspace="/abs/path/repo-b")
```

Always pass the **repo** path — never the parent folder that contains many repos.

Confirm with resource `project://group` (after any tool call with `workspace=`):
`grouped: true`, list of projects, and any `xrepo-*` Specs already in the DB.

---

## 2. Author a cross-repo Spec (`xrepo-*`)

1. Pick an OpenSpec slug that starts with `xrepo-`, e.g. `xrepo-hotel-search`.
2. In **each** repo that owns part of the behaviour, add the requirement under
   `openspec/specs/<capability>/spec.md` (same requirement title / slug), then
   `sync_openspec(workspace=<that-repo>)`.
   - Escape hatch: `create_spec(spec_id="xrepo-hotel-search", …)` per repo
     (needs Spec mutation plugin / `LIVESPEC_PLUGINS`).
3. Annotate implementing symbols:

```python
def search_hotels(...):
    """@spec:xrepo-hotel-search
    Orchestrates hotel search across SAs.
    """
```

4. Re-`index_project` so annotations become `spec_symbol` links.
5. Optional: `link_spec_dependency` between related `xrepo-*` Specs (requires /
   extends) inside a repo; Flow Explorer shows Spec↔Spec edges.

Ids **must** use the `xrepo-` prefix for Flow Explorer’s cross-repo Specs tab
and for `project://group` aggregation. In-repo Specs stay normal OpenSpec slugs
(`auth-user-login`).

---

## 3. Query the group

From **any** member workspace (same `group_db`):

| Intent | Call |
|--------|------|
| List shared Specs | `list_specs` — filter mentally / by id prefix `xrepo-`; or read `project://group` |
| What implements this Spec here + elsewhere | `get_spec_implementation(spec_id="xrepo-…")` per repo, or Flow bundle |
| Client → server hop | `who_does_this_call` → `invokes_endpoints` |
| Server → client hop | `who_calls` → `route_callers` |
| Likely-unused HTTP | `find_legacy_flows(summary_only=True)` — graph ≠ traffic |
| UI map | `export_flow_explorer` (docs plugin) or `livespec explorer serve --flow` |

`find_symbol` / `get_symbol_source` / `quick_orient` also search the whole
`group_db` when grouped.

---

## 4. Traps

- **Annotation alone ≠ Spec row.** `@spec:xrepo-…` without a Spec in the store
  does not create the Spec. Sync OpenSpec or `create_spec` first.
- **Mirror in every repo** that should show up under that Spec in Flow Explorer.
  A Spec only in the hub will not list SA symbols.
- **`orphan_client`** often means the SA is outside `group_db`, not dead code.
- **Spring / Java** endpoints: call tools with `workspace=<java-repo>` (or use
  group Java hints). Hub TS-only workspaces may show Spring count `0`.
- Do not treat `find_legacy_flows` as delete permission — confirm with APM/logs.

---

## 5. Cold-open checklist (polyrepo session)

```
1. Read guide://cross-repo (or prompt cross_repo_workflow)
2. index_project on each member (or at least hub + SAs you care about)
3. Read project://group — confirm grouped + xrepo_* counts
4. list_specs / get_spec_implementation for the flow under discussion
5. who_does_this_call / find_legacy_flows as needed
6. export_flow_explorer if the user wants the HTML map
```
