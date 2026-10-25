# Cross-repo Specs + HTTP (`group_db`)

**Audience:** AI agents with the livespec MCP. Read this when the user has
several sibling repos, asks about end-to-end flows, or mentions `xrepo-*`.

Also exposed as MCP tool `get_cross_repo_guide`, prompt `cross_repo_workflow`,
and resources `project://cross-repo` / `project://group` (alias
`guide://cross-repo`). Prefer the **tool** if a host's resource list looks
incomplete.

---

## What “cross-repo” means here

Two complementary layers share one SQLite when configured:

| Layer | How | Tools / UI |
|-------|-----|------------|
| **HTTP hops** | Client `requests`/`fetch` → server route | `who_does_this_call` → `invokes_endpoints`; `who_calls` → `route_callers`; `find_legacy_flows` |
| **Shared Specs** | Same Spec id `xrepo-*` in **each** participating repo | `list_specs` / `get_spec_implementation`; the `livespec ui` group page |
| **Shared schemas** | Same `@schema: <entity>` on each declaration of one entity | `schema_drift`, `schema_impact`; resources `project://schemas` / `project://schemas/{entity}` |

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
   extends) inside a repo; the `livespec ui` group page lists each shared Spec's dependencies.

Ids **should** use the `xrepo-` prefix so the `livespec ui` group page lists them as cross-repo Specs (it also lists any id mirrored in more than one repo)
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
| UI map | `livespec ui` (or `/livespec-ui` in Claude Code) → the group's page; read-only, nothing written into any repo |

`find_symbol` / `get_symbol_source` / `quick_orient` also search the whole
`group_db` when grouped.

---

## 4. Link schemas across the group (`@schema:`)

One entity is usually declared once per layer: a Zod schema or TS interface in
the frontend, a Pydantic model or dataclass in the backend, a `CREATE TABLE` in
a migration. Bind each declaration to the same entity name with an `@schema:`
line in its docstring, JSDoc, or the `--` comment right above the table:

```python
class User(BaseModel):
    """@schema: user"""
```

```ts
/** @schema: user */
export const UserSchema = z.object({ /* ... */ });
```

```sql
-- @schema: user
CREATE TABLE users ( /* ... */ );
```

Re-`index_project` each member: every run links all declarations bound to one
entity in the `group_db` (entity and field names match across conventions:
`user_profile` = `UserProfile`, `created_at` = `createdAt`). Then, from any
member:

| Intent | Call |
|--------|------|
| Does every declaration of an entity agree? | `schema_drift(entity="user")`: `missing_field`, `type_mismatch`, `nullability_mismatch`, `optional_mismatch`, file:line per side |
| Which entities drift | `schema_drift(summary_only=True)` or resource `project://schemas` |
| Every finding of one entity | resource `project://schemas/{entity}` |

`@schema:` is not a Spec: it needs no Spec row and creates no Spec link.
Without a `group_db`, links stay inside one repo. Not handled yet: `ALTER
TABLE` in later migrations, and a base class or interface from another file.

### Before changing a shared field: `schema_impact`

Call `schema_impact` **before** you add, remove, rename or retype a field of a
declaration bound with `@schema:`, not after the frontend breaks:

```
schema_impact(entity="user", change="remove", field="avatar_url", workspace=<repo>)
schema_impact(entity="user", change="rename", field="email", new_name="contact_email", workspace=<repo>)
schema_impact(entity="user", change="retype", field="id", new_type="uuid", workspace=<repo>)
```

- **`declarations`**: every declaration of the entity in the group (your repo
  first), with file:line, the matched field and the `action` that side needs:
  `remove` / `absent`, `rename` / `both_present` / `renamed` / `absent`,
  `retype` / `compatible` / `absent`, `add` / `present`. `compatible` means
  the side already carries the new type on its wire (a TS `string` for a
  `uuid`), so it needs no edit.
- **`readers`**: the symbols that read those declarations, with project,
  file:line and `role` (`endpoint` with method and path, `test`, `symbol`).
  They cross repos two ways, named in `via`:
  - `graph`: the call graphs of the whole group are walked as one, so a
    frontend `fetch('/api/users/42')` reaches the backend handler of
    `GET /api/users/{user_id}`, which builds the model, and is listed as a
    reader in the frontend repo.
  - `spec:<id>`: symbols linked to a Spec that a declaration is linked to.
    Mirror the same `xrepo-*` id in each repo (section 2) and annotate both
    the declaration and the code that depends on it; the shared id joins them.

  `schema_binding` lists symbols bound with `@schema:` that are not schemas.
  Readers are per declaration, not per field.
- **Type positions are not call edges.** A TS `function render(u: User)` gives
  the graph nothing to follow, and only a constructor call such as
  `User(**d)` reaches a Python model, so TS, Zod and SQL declarations usually
  have no reader of their own. The `hint` says so when
  the graph finds no reader; use `grep_in_indexed_files` on the declaration's
  name, or bind the dependent code with `@schema:` / a mirrored `xrepo-*` Spec.
- `summary_only=True` keeps `declarations`, `by_project` and `counts` and drops
  the reader rows; `limit` / `cursor` page them.

`git_diff_impact` does the same for a diff that already touches a bound
declaration: its `schema_impact` key reads the field changes from git at base
and head, lists the actions the other declarations need and the reader counts
per project, and gives the `schema_impact(...)` calls to follow up with.
`analyze_impact` does not follow schema links; for a shared field, call
`schema_impact`.

---

## 5. Traps

- **Annotation alone ≠ Spec row.** `@spec:xrepo-…` without a Spec in the store
  does not create the Spec. Sync OpenSpec or `create_spec` first.
- **Mirror in every repo** that should show up under that Spec in the `livespec ui` group page.
  A Spec only in the hub will not list SA symbols.
- **`orphan_client`** often means the SA is outside `group_db`, not dead code.
- **Spring / Java** endpoints: call tools with `workspace=<java-repo>` (or use
  group Java hints). Hub TS-only workspaces may show Spring count `0`.
- Do not treat `find_legacy_flows` as delete permission — confirm with APM/logs.

---

## 6. Cold-open checklist (polyrepo session)

```
1. Call get_cross_repo_guide (or read project://cross-repo / prompt cross_repo_workflow)
2. index_project on each member (or at least hub + SAs you care about)
3. Read project://group — confirm grouped + xrepo_* counts
4. list_specs / get_spec_implementation for the flow under discussion
5. who_does_this_call / find_legacy_flows as needed
6. schema_drift(summary_only=True) if declarations carry @schema:;
   schema_impact(entity=..., change=..., field=...) before changing a shared field
7. `livespec ui` (`/livespec-ui`) if the user wants to browse the map
```
