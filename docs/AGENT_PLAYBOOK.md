# Agent Playbook — livespec-mcp

**Audience:** AI agents (Cursor, Claude Code, etc.) with the livespec MCP server configured.

**Purpose:** How to *use* livespec tools efficiently, and how to *comment/link* code so Functional Requirements (RFs) stay traceable in the index.

This document is exposed as the MCP prompt `agent_playbook`. **Read or invoke that prompt at the start of a session** on any repo indexed by livespec.

---

## 1. What livespec gives you

| You get | You do *not* get |
|--------|-------------------|
| Call graph + impact (`who_calls`, `analyze_impact`, `git_diff_impact`) | Automatic prose docs while you sleep |
| RF ↔ code links (`@rf:` annotations, `get_requirement_implementation`) | Replacing `grep` for string search |
| Living index (re-run `index_project` after pulls) | A watcher you should rely on while editing (re-index on demand) |

**Workspace (required):** pass `workspace="/abs/path/to/project"` on **every** tool call. There is **no** `LIVESPEC_WORKSPACE` env var and no default in `mcp.json`. Omitting `workspace` returns an error.

**State on disk:** `<project>/.mcp-docs/docs.db` per workspace — safe to delete to reset; never commit secrets there.

**Anti-pattern:** indexing a parent folder (`/my` with 10 repos) — always pass the single repo path.

---

## 2. Cold open (every new session / after big pulls)

Replace `PROJECT` with the absolute path (e.g. `/Users/me/sample-api`):

```
index_project(workspace="PROJECT")
get_project_overview(workspace="PROJECT")
list_requirements(workspace="PROJECT")
```

Report: file/symbol/edge counts, languages, top symbols, RF totals.

**Plugins (v0.18):** RF/docs mutation tools appear in `tools/list` after a
`workspace=` call when that repo has `rf` rows and/or a `.mcp-docs/explorer/`
bundle. On a **fresh** repo with no index yet, set `LIVESPEC_PLUGINS=rf` or
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
| Blast radius + RF rollup | `analyze_impact(target_type, target, max_depth)` | `symbol` \| `file` \| `requirement` |
| PR / diff scope | `git_diff_impact(base_ref, head_ref)` | Inside a git repo only |
| Semantic grep | `search(query, scope)` | FTS5; optional vectors if embedded |
| RF list | `list_requirements(...)` | Filters: status, module, `has_implementation` |
| What implements RF-NNN? | `get_requirement_implementation(rf_id)` | One round-trip |
| Coverage gaps | `audit_coverage()` | Orphans, low-confidence links |
| Brownfield RF ideas | `propose_requirements_from_codebase()` | Heuristic; user approves before create |
| Docstring RF hints | `scan_docstrings_for_rf_hints()` | No writes |
| Dead code candidates | `find_dead_code()` | Respects entry points / `pub` / frameworks |
| HTTP/CLI entry points | `find_endpoints(framework?)` | Prefer `summary_only=True` if full JSON breaks |
| Batch link (escape hatch) | `bulk_link_rf_symbols(mappings)` | Configs, SQL, langs without `@rf:` extractor |

### `livespec-rf` plugin — *mutate RF state* (operator)

`create_requirement`, `update_requirement`, `delete_requirement`, `link_rf_symbol`, `link_rf_dependency`, `scan_rf_annotations`, `import_requirements_from_markdown`, …

Run `scan_rf_annotations()` after bulk doc edits; it also runs automatically at end of `index_project()`.

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

## 5. How to comment code for livespec (RF traceability)

The index **auto-links** symbols when their **docstring or leading comment block** contains recognized RF annotations (Python, JavaScript, TypeScript today). After editing annotations, run `index_project()` (or rely on post-index scan).

### 5.1 Level 1 — explicit (confidence 1.0) — **preferred**

Put on its **own line** at the top of the docstring/comment:

```python
def parse_sabre_xml(xml: str) -> PNRNormalized:
    """@rf:RF-003
    Validate PNR vs structural conditions from Sabre XML.
    """
```

Grammar (case-insensitive verb):

| Prefix | Relation |
|--------|----------|
| `@rf:RF-003` | implements |
| `@implements:RF-003` | implements |
| `@tests:RF-003` | tests |
| `@see:RF-003` / `@references:RF-003` | references |

**Multi-RF:** `@rf:RF-001, RF-002`  
**Confidence override:** `@rf:RF-001:0.85` (applies to all RFs on that line)  
**Negation (cancels hits in this block):** `@not_rf:RF-007` or `@!rf:RF-007`

### 5.2 Level 2 — verb-anchored (confidence 0.7)

Use when a full `@rf:` line is too heavy; must be a real verb phrase:

```python
"""Implements RF-003 for PNR structural validation."""
```

Recognized verbs: `implements`, `tests`, `references`, `covers`.  
**Ignored:** bare mentions (`see RF-003 in ticket`), negated context (`does not implement RF-003`), TODO lines.

### 5.3 Docstring style that helps discovery

`scan_docstrings_for_rf_hints` and `propose_requirements_from_codebase` use the **first sentence** and **leading action verb**:

```python
def match_overs_with_insights(...):
    """Match candidate OVER policies; emit RF-2 market insights when no ticket applies."""
```

- **Good:** verb-first, one clear behavior per function/class.
- **Weak:** empty docstrings, only `@param`, changelog prose without `@rf:`.

### 5.4 Where to annotate

| Location | Auto-scan |
|----------|-----------|
| Function / method / class docstring (Python) | Yes |
| JS/TS leading block comments on declarations | Yes |
| Go, Rust, Java, Ruby, PHP | Graph yes; `@rf:` in-source not wired — use `bulk_link_rf_symbols` |
| YAML, SQL, Markdown specs | Use `import_requirements_from_markdown` + bulk link |

Annotate **behavior-bearing** symbols (handlers, domain services, matchers). Skip `__init__.py` package markers unless they contain real logic.

### 5.5 Markdown RF catalog (bulk create)

Create `docs/REQUISITOS_FUNCIONALES.md` (or project convention):

```markdown
## RF-001: Detect OVER in XML

**Prioridad:** alta
**Módulo:** overs

Expose OVER codes and presence flags from incoming XML.

## RF-002: Cross-airline insights

**Prioridad:** media
**Módulo:** matcher

Market policies that are insight-only when plate filters exclude them.
```

Then (plugin): `import_requirements_from_markdown(path="docs/REQUISITOS_FUNCIONALES.md")` — idempotent.

### 5.6 Linking without editing source

```json
bulk_link_rf_symbols(mappings=[
  {"rf_id": "RF-003", "symbol_qname": "src.over_validator.services.pnr_parser.parse_sabre_xml",
   "relation": "implements", "confidence": 1.0, "source": "manual"}
])
```

Use `find_symbol` to get exact `qualified_name` from the index.

### 5.7 RF dependency graph (plugin)

```text
link_rf_dependency(parent_rf_id="RF-004", child_rf_id="RF-003", kind="requires")
```

Kinds: `requires` | `extends` | `conflicts`. Cycles are rejected.

---

## 6. Brownfield workflow (no RFs yet)

1. `index_project()` + `get_project_overview()`
2. `propose_requirements_from_codebase(max_proposals=30)` — review with human
3. `import_requirements_from_markdown` *or* `create_requirement` per approved RF
4. Add `@rf:RF-NNN` lines to implementing symbols (or `bulk_link_rf_symbols`)
5. `index_project()` → `scan_rf_annotations()` → `audit_coverage()`
6. Iterate: `get_requirement_implementation`, `analyze_impact(target_type="requirement", ...)`

---

## 7. While implementing features

1. **Before edit:** `quick_orient` + `analyze_impact` on touched symbols.
2. **While editing:** add/update `@rf:` in docstrings for new behavior.
3. **After edit:** `index_project()` on the workspace.
4. **Before PR:** `git_diff_impact(base_ref="main", head_ref="HEAD")` → run suggested tests.

When explaining code to the user, cite **RF ids** from `list_requirements` / annotations, not only file paths.

---

## 8. Anti-patterns

| Don't | Do instead |
|-------|------------|
| Index `/parent` with 10 repos | Pass `workspace="/abs/path/to/one-repo"` per call |
| Fix project in `mcp.json` and restart MCP | Pass `workspace=` on each tool call |
| Mention `RF-003` in comments without `@rf:` or verb anchor | Use `@rf:RF-003` on its own line |
| Assume zero callers = dead code | Check `quick_orient.is_entry_point` |
| Call removed tools (`get_symbol_info`, `find_references`) | `quick_orient`, `analyze_impact(max_depth=1)` |
| Rely on filesystem watcher during active edits | `index_project()` when done |
| Create RFs without linking code | `@rf:` + re-index or `bulk_link_rf_symbols` |

---

## 9. Other MCP prompts (slash commands)

| Prompt | Use when |
|--------|----------|
| `agent_playbook` | **This document** — usage + commenting |
| `onboard_project` | New repo, no context |
| `explain_symbol` | Deep dive one qname |
| `analyze_change_impact` | Planned change |
| `audit_requirement_coverage` | Traceability audit |
| `extract_requirements_from_module` | Module-scoped RF draft |

---

## 10. Quick reference — annotation examples

```python
# implements, full confidence
"""@rf:RF-008
Rank candidates by commission; apply cabin/family flags at apply time.
"""

# tests
"""@tests:RF-004"""

# negation in same docstring
"""@rf:RF-001
@not_rf:RF-007
Tour codes are out of scope for matcher (RF-7).
"""

# multi-RF
"""@implements:RF-002, RF-005"""
```

---

*Version: livespec-mcp 0.11+. Source: `docs/AGENT_PLAYBOOK.md`. Battle-test notes: `docs/AGENT_USAGE_DATA.md`, flow: `docs/AGENT_QUICKSTART.md`.*
