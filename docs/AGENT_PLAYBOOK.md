# Agent Playbook — livespec

**Audience:** AI agents (Cursor, Claude Code, etc.) with the livespec MCP server configured.

**Purpose:** How to *use* livespec tools efficiently, and how to *comment/link* code so Specs (Functional Requirements, ADRs, NFRs, ...) stay traceable in the index.

This document is exposed as the MCP prompt `agent_playbook`. **Read or invoke that prompt at the start of a session** on any repo indexed by livespec.

---

## 1. What livespec gives you

| You get | You do *not* get |
|--------|-------------------|
| Call graph + impact (`who_calls`, `analyze_impact`, `git_diff_impact`) | Automatic prose docs while you sleep |
| Spec ↔ code links (`@spec:` annotations, `get_spec_implementation`) | Replacing `grep` for string search |
| Living index (re-run `index_project` after pulls) | A watcher you should rely on while editing (re-index on demand) |

**Workspace (required):** pass `workspace="/abs/path/to/project"` on **every** tool call. There is **no** `LIVESPEC_WORKSPACE` env var and no default in `mcp.json`. Omitting `workspace` returns an error.

**State on disk:** `<project>/.mcp-docs/docs.db` per workspace — safe to delete to reset; never commit secrets there.

**Anti-pattern:** indexing a parent folder (`/my` with 10 repos) — always pass the single repo path.

---

## 2. Cold open (every new session / after big pulls)

Replace `PROJECT` with the absolute path (e.g. `/Users/me/over-validator`):

```
index_project(workspace="PROJECT")
get_project_overview(workspace="PROJECT")
list_specs(workspace="PROJECT")
```

Report: file/symbol/edge counts, languages, top symbols, Spec totals.

**Plugins (v0.18):** Spec/docs mutation tools appear in `tools/list` after a
`workspace=` call when that repo has `spec` rows and/or a `.mcp-docs/explorer/`
bundle. On a **fresh** repo with no index yet, set `LIVESPEC_PLUGINS=spec` or
`=all` in MCP config, or run `index_project` first and reconnect MCP if the
client cached the short tool list.

---

## 3. Tool tiers — what to call when

### Default surface (always on) — *ask questions*

| Intent | Tool | Notes |
|--------|------|--------|
| First contact on a symbol | `quick_orient(qname)` | Replaces 3–4 older calls; includes `is_entry_point` |
| Name lookup | `find_symbol(query)` | Separator-agnostic (`::`, `.`, `#`) |
| Read body only | `get_symbol_source(qname)` | Lighter than full orient |
| Who calls this? | `who_calls(qname, max_depth=1)` | Use `summary_only=True` on huge repos |
| What does this call? | `who_does_this_call(qname, max_depth=1)` | Forward cone |
| Blast radius + Spec rollup | `analyze_impact(target_type, target, max_depth)` | `symbol` \| `file` \| `spec` |
| PR / diff scope | `git_diff_impact(base_ref, head_ref)` | Inside a git repo only |
| Semantic grep | `search(query, scope)` | FTS5; optional vectors if embedded |
| Spec list | `list_specs(...)` | Filters: status, module, `kind`, `has_implementation` |
| What implements SPEC-NNN? | `get_spec_implementation(spec_id)` | One round-trip |
| Coverage gaps | `audit_coverage()` | Orphans, low-confidence links |
| Brownfield Spec ideas | `propose_specs_from_codebase()` | Heuristic; user approves before create |
| Dead code candidates | `find_dead_code()` | Respects entry points / `pub` / frameworks |
| HTTP/CLI entry points | `find_endpoints(framework?)` | Prefer `summary_only=True` if full JSON breaks |
| Batch link (escape hatch) | `bulk_link_spec_symbols(mappings)` | Configs, SQL, langs without `@spec:` extractor |
| Import a whole OpenSpec repo | `sync_openspec(openspec_dir?)` | Specs **+** changes from `openspec/`; reads `openspec.json` |
| Write Specs back to OpenSpec | `export_openspec(out_dir?)` | `specs/<capability>/spec.md` + `changes/` — closes the round-trip |
| OpenSpec structural check | `validate_openspec(strict?)` | Mirror of `openspec validate --strict` (every requirement needs ≥1 scenario) |
| Inspect change proposals | `list_spec_changes()` / `get_spec_change(name)` | proposal/design/tasks + ADD/MODIFY/REMOVE deltas |

> **livespec speaks OpenSpec (Fission-AI).** If the repo has an `openspec/`
> directory, livespec is the code-graph/traceability layer *beneath* it — it
> does not compete with OpenSpec authoring. See §5.8 for the loop.

### `livespec-spec` plugin — *mutate Spec state* (operator)

`create_spec`, `update_spec`, `delete_spec`, `link_spec_symbol`, `link_scenario_symbol`, `link_spec_dependency`, `scan_spec_annotations`, `scan_docstrings_for_spec_hints`, `apply_spec_change`, `archive_spec_change`, … (`import_specs_from_markdown` and `sync_openspec` are always-visible core, not gated).

> The spec-mutation tools above are **plugin-gated**: they appear only once
> the workspace has spec rows or `LIVESPEC_PLUGINS=spec` (or `=all`) is set.
> `scan_docstrings_for_spec_hints()` is one of them — do not expect it on a
> spec-less repo without the override.

Run `scan_spec_annotations()` after bulk doc edits; it also runs automatically at end of `index_project()`.

### `livespec-docs` plugin — *generated Markdown docs*

`generate_docs`, `list_docs`, `export_documentation` — optional; drift detection is live, generation needs LLM/sampling.

---

## 4. Canonical call patterns (battle-tested)

Avoid the v0.7 chain `find_symbol → get_symbol_info → who_calls → …`.

| Pattern | Calls |
|---------|--------|
| Cold open | `index_project` → `get_project_overview` |
| Land on a name | `find_symbol` → `quick_orient` |
| Read then edit | `quick_orient` → `get_symbol_source` |
| Delegate drill-down | `quick_orient` → `who_does_this_call` |
| Before refactor | `analyze_impact` or `git_diff_impact` |

**Large repos (>30k symbols):** `summary_only=True`, `limit` + `cursor` on aggregators; `min_weight=0.6` on traversals (default) drops resolver fan-out noise.

---

## 5. How to comment code for livespec (Spec traceability)

The index **auto-links** symbols when their **docstring or leading comment block** contains recognized Spec annotations (Python, JavaScript, TypeScript today). After editing annotations, run `index_project()` (or rely on post-index scan).

### 5.1 Level 1 — explicit (confidence 1.0) — **preferred**

Put on its **own line** at the top of the docstring/comment:

```python
def parse_sabre_xml(xml: str) -> PNRNormalized:
    """@spec:SPEC-003
    Validate PNR vs structural conditions from Sabre XML.
    """
```

Grammar (case-insensitive verb):

| Prefix | Relation |
|--------|----------|
| `@spec:SPEC-003` | implements |
| `@implements:SPEC-003` | implements |
| `@tests:SPEC-003` | tests |
| `@see:SPEC-003` / `@references:SPEC-003` | references |

**Multi-Spec:** `@spec:SPEC-001, SPEC-002`  
**Confidence override:** `@spec:SPEC-001:0.85` (applies to all Specs on that line)  
**Negation (cancels hits in this block):** `@not_spec:SPEC-007` or `@!spec:SPEC-007`

### 5.2 Level 2 — verb-anchored (confidence 0.7)

Use when a full `@spec:` line is too heavy; must be a real verb phrase:

```python
"""Implements SPEC-003 for PNR structural validation."""
```

Recognized verbs: `implements`, `tests`, `references`, `covers`.  
**Ignored:** bare mentions (`see SPEC-003 in ticket`), negated context (`does not implement SPEC-003`), TODO lines.

### 5.3 Docstring style that helps discovery

`scan_docstrings_for_spec_hints` and `propose_specs_from_codebase` use the **first sentence** and **leading action verb**:

```python
def match_overs_with_insights(...):
    """Match candidate OVER policies; emit SPEC-2 market insights when no ticket applies."""
```

- **Good:** verb-first, one clear behavior per function/class.
- **Weak:** empty docstrings, only `@param`, changelog prose without `@spec:`.

### 5.4 Where to annotate

| Location | Auto-scan |
|----------|-----------|
| Function / method / class docstring (Python) | Yes |
| JS/TS leading block comments on declarations | Yes |
| Go, Rust, Java, Ruby, PHP | Graph yes; `@spec:` in-source not wired — use `bulk_link_spec_symbols` |
| YAML, SQL, Markdown specs | Use `import_specs_from_markdown` + bulk link |

Annotate **behavior-bearing** symbols (handlers, domain services, matchers). Skip `__init__.py` package markers unless they contain real logic.

### 5.5 Markdown Spec catalog (bulk create)

Create `docs/REQUISITOS_FUNCIONALES.md` (or project convention):

```markdown
## SPEC-001: Detect OVER in XML

**Prioridad:** alta
**Módulo:** overs

Expose OVER codes and presence flags from incoming XML.

## SPEC-002: Cross-airline insights

**Prioridad:** media
**Módulo:** matcher

Market policies that are insight-only when plate filters exclude them.
```

Then (plugin): `import_specs_from_markdown(path="docs/REQUISITOS_FUNCIONALES.md")` — idempotent.

### 5.6 Linking without editing source

```json
bulk_link_spec_symbols(mappings=[
  {"spec_id": "SPEC-003", "symbol_qname": "src.over_validator.services.pnr_parser.parse_sabre_xml",
   "relation": "implements", "confidence": 1.0, "source": "manual"}
])
```

Use `find_symbol` to get exact `qualified_name` from the index.

### 5.7 Spec dependency graph (plugin)

```text
link_spec_dependency(parent_spec_id="SPEC-004", child_spec_id="SPEC-003", kind="requires")
```

Kinds: `requires` | `extends` | `conflicts`. Cycles are rejected.

### 5.8 OpenSpec (Fission-AI) interop — you are the layer *beneath* it

If a repo has an `openspec/` directory (`specs/`, `changes/`, optional
`openspec.json`), livespec ingests it and keeps code↔spec traceability on top.
livespec **reads and writes** the OpenSpec format — it is not a competitor to
OpenSpec authoring. The full loop:

1. **Ingest.** `sync_openspec()` imports the whole tree in one call — canonical
   requirements from `specs/` **and** every change under `changes/`
   (proposed) / `archive/` (archived). (Single file? `import_specs_from_markdown(path=...)`.)
2. **Understand.** `list_specs()` (each row has `scenario_count`),
   `get_spec_implementation(spec_id)` returns the requirement, its
   `#### Scenario:` blocks with per-scenario linked `symbols` + a `verified`
   flag, and `coverage.scenarios_verified`.
3. **Trace at scenario granularity.** OpenSpec reasons per scenario, so link
   code/tests to a *single* scenario, not just the requirement:
   `link_scenario_symbol(spec_id, scenario_name, symbol_qname, relation="tests")`.
4. **Change lifecycle.** `list_spec_changes()` / `get_spec_change(name)` inspect
   a proposal; `apply_spec_change(name)` folds its deltas into the canonical
   spec set (ADDED/MODIFIED/RENAMED activate, REMOVED deprecate);
   `archive_spec_change(name)` closes it.
5. **Validate.** `validate_openspec(strict=True)` mirrors
   `openspec validate --strict` — the load-bearing rule is *every requirement
   MUST have ≥1 scenario*.
6. **Write back.** `export_openspec(out_dir="openspec")` re-emits the canonical
   tree (`specs/<capability>/spec.md` with `## Purpose` / `### Requirement:` /
   `#### Scenario:`) + `changes/` + `archive/` — closing the round-trip.

Mapping: an OpenSpec **capability** == a livespec **module** (pass either as
`capability=` or `module=`); a requirement name slugs to the spec_id
(`auth` + "User Login" → `auth-user-login`). Everything is idempotent — re-sync
freely. Invoke the `openspec_workflow` MCP prompt for this as a slash command.

---

## 6. Brownfield workflow (no Specs yet)

1. `index_project()` + `get_project_overview()`
2. `propose_specs_from_codebase(max_proposals=30)` — review with human
3. `import_specs_from_markdown` *or* `create_spec` per approved Spec
4. Add `@spec:SPEC-NNN` lines to implementing symbols (or `bulk_link_spec_symbols`)
5. `index_project()` → `scan_spec_annotations()` → `audit_coverage()`
6. Iterate: `get_spec_implementation`, `analyze_impact(target_type="spec", ...)`

---

## 7. While implementing features

1. **Before edit:** `quick_orient` + `analyze_impact` on touched symbols.
2. **While editing:** add/update `@spec:` in docstrings for new behavior.
3. **After edit:** `index_project()` on the workspace.
4. **Before PR:** `git_diff_impact(base_ref="main", head_ref="HEAD")` → run suggested tests.

When explaining code to the user, cite **Spec ids** from `list_specs` / annotations, not only file paths.

---

## 8. Anti-patterns

| Don't | Do instead |
|-------|------------|
| Index `/parent` with 10 repos | Pass `workspace="/abs/path/to/one-repo"` per call |
| Fix project in `mcp.json` and restart MCP | Pass `workspace=` on each tool call |
| Mention `SPEC-003` in comments without `@spec:` or verb anchor | Use `@spec:SPEC-003` on its own line |
| Assume zero callers = dead code | Check `quick_orient.is_entry_point` |
| Call removed tools (`get_symbol_info`, `find_references`) | `quick_orient`, `analyze_impact(max_depth=1)` |
| Rely on filesystem watcher during active edits | `index_project()` when done |
| Create Specs without linking code | `@spec:` + re-index or `bulk_link_spec_symbols` |

---

## 9. Other MCP prompts (slash commands)

| Prompt | Use when |
|--------|----------|
| `agent_playbook` | **This document** — usage + commenting |
| `onboard_project` | New repo, no context |
| `explain_symbol` | Deep dive one qname |
| `analyze_change_impact` | Planned change |
| `audit_spec_coverage` | Traceability audit |
| `extract_specs_from_module` | Module-scoped Spec draft |
| `openspec_workflow` | Repo has an `openspec/` dir — sync/trace/validate/export loop |

---

## 10. Quick reference — annotation examples

```python
# implements, full confidence
"""@spec:SPEC-008
Rank candidates by commission; apply cabin/family flags at apply time.
"""

# tests
"""@tests:SPEC-004"""

# negation in same docstring
"""@spec:SPEC-001
@not_spec:SPEC-007
Tour codes are out of scope for matcher (SPEC-7).
"""

# multi-Spec
"""@implements:SPEC-002, SPEC-005"""
```

---

*Version: livespec 0.22+. Source: `docs/AGENT_PLAYBOOK.md`. Battle-test notes: `docs/AGENT_USAGE_DATA.md`, flow: `docs/AGENT_QUICKSTART.md`.*
