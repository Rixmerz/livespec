## Why

A product that ships a frontend, a backend and a database describes each of its
shared entities three or more times: a Zod schema or TypeScript types in the
frontend, a Pydantic model, dataclass or TypeScript interface in the backend,
and a `CREATE TABLE` in a migration. Each copy is edited by a different person
in a different repository, and nothing tells you they disagree. A field renamed
in the backend, a column made nullable in a migration, or an optional property
the API still sends as required shows up as a runtime bug, never as an index
finding. livespec already indexes all of these files and already joins HTTP
routes across the repositories of a `group_db`; it records none of the fields.

Schema-first tooling fixes this by making one file the source and generating
the others. This change keeps every schema where it lives and makes livespec
know which declarations describe the same entity and where they drift, field by
field. The change carries all three phases.

## What Changes

Phase 1:

- **Field-level extraction.** Pydantic models, dataclasses, TypeScript
  interfaces, TypeScript type aliases of object types, Zod `z.object(...)`
  declarations and `CREATE TABLE` statements in `.sql` files are recorded as
  schema declarations with their flavor and their fields in source order:
  name, convention-free key, neutral type, item type, reference target, raw
  type, optional, nullable and line. `.sql` files are parsed in pure Python,
  without a tree-sitter grammar.
- **Neutral type model.** 13 neutral types (`string`, `int`, `float`,
  `decimal`, `bool`, `datetime`, `date`, `uuid`, `json`, `enum`, `list`, `ref`,
  `unknown`) with per-language mapping rules. Optional (may be absent) and
  nullable (may hold null) are two separate flags, never derived from each
  other.
- **`@schema: <entity>` annotation.** A docstring or leading-comment line binds
  a symbol to a named entity. It is not a Spec annotation: it creates no Spec
  link and `scan_annotation_verbs` does not flag it.
- **Cross-repo linking.** Every index run links every pair of symbols bound to
  the same entity in the database, so a shared `group_db` links schemas across
  repositories, modelled on how `_resolve_routes` joins HTTP routes. Links live
  in a dedicated `schema_link` table, not in `symbol_edge`.
- **`schema_drift` core tool** (paginated, exact counts) reporting
  `missing_field`, `type_mismatch`, `nullability_mismatch` and
  `optional_mismatch` per entity, with project, file and line for every side;
  `snake_case` and `camelCase` names match as the same field.
- **Resources** `project://schemas` and `project://schemas/{entity}`.
- **Migration 27** `schema_symbols`: tables `schema_decl`, `schema_field`,
  `schema_binding` and `schema_link`. Existing indexes are re-extracted on the
  next `index_project` so their schemas appear.
- Core tool count goes from 38 to 39 (54 in all with the Spec and docs
  plugins).

Phase 2 answers the question that comes after "do these declarations still
agree?": what does changing one field of an entity touch, in every repository
of the group? It also fixes two extraction bugs phase 1 left: a
backslash-escaped quote in a SQL string literal hid every later table, and a
Pydantic `Field(alias=...)` was matched by its Python attribute instead of its
wire name, so `schema_drift` reported a false `missing_field`.

- **`schema_impact` core tool** (paginated, exact counts). For a proposed
  `add`, `remove`, `rename` or `retype` of one field of an entity bound with
  `@schema:`, it lists every declaration of that entity in the workspace's
  projects (every project of a `group_db`) with the action the change asks of
  it and the matched field's file and line, and every symbol, endpoint and test
  that reads those declarations through the call graph, an `@schema:` binding
  or a shared Spec, with project, file and line, ordered by project with the
  home project first.
- **`schema_impact` section of `git_diff_impact`.** When the range changes a
  schema declaration bound to an entity, the payload gains `schema_impact`: the
  fields the range added, removed or retyped in that declaration (base against
  head), the entity's other declarations with the action each field change
  asks of them, and the readers' counts per project with a bounded sample. A
  range that changes no bound declaration leaves the payload unchanged.
- **SQL string literals honour backslash escapes.** Inside a single-quoted
  literal a backslash escapes the next character, and a doubled `''` still
  escapes a quote, so `DEFAULT 'it\'s'` no longer hides the tables after it.
- **A Pydantic `Field(alias=...)` is matched by its alias.** The field's
  convention-free key is the key of its alias, its wire name, so a Pydantic
  `display` aliased `fullName` matches a TypeScript `fullName`.
- Core tool count goes from 39 to 40 (55 in all with the Spec and docs
  plugins).

Phase 3 turns the impact into the edit: once `schema_impact` has said which
declarations a field change touches, the same change should come back as the
exact text to write in each of them, in every repository of the group.

- **`plan_schema_change` core tool** (paginated, exact counts). For an `add`,
  `remove`, `rename`, `retype` or `optionality` change of one field of an
  entity bound with `@schema:`, it returns every declaration of that entity
  across the group's repositories (Pydantic, dataclass, TypeScript interface
  and object type, Zod, SQL `CREATE TABLE`) with a status (`edit`, `none`,
  `manual` with a one-sentence reason, or `stale`) and, for an `edit`, the
  exact file, 1-based line range, current text and proposed new text. Each
  side is spelled in its own convention (`contact_email` in Python and SQL,
  `contactEmail` in TypeScript and Zod), a Pydantic `Field(alias=...)` is
  renamed by rewriting its alias, a missing Python import is added, and a
  `CREATE TABLE` edit carries a PostgreSQL migration hint (`ALTER TABLE ...`),
  which is text and is never written. The readers are the ones
  `schema_impact` finds. Types that cannot be written exactly (`list`, `ref`,
  `enum`, `unknown`) are an `mcp_error`.
- **Read-only by default, with an explicit apply mode.** `apply=True` writes
  the edits of the projects named in `apply_workspaces` (default: the calling
  workspace), lists everything else under `apply.not_applied`, and refuses
  the whole apply with nothing written when any file it would write changed
  since indexing, is missing, resolves outside its project root, or no longer
  takes its edits. An `apply_workspaces` entry that is not a project root of
  the group is refused. It writes through a temp file and `os.replace`, keeps
  the file mode and line endings, never creates a file and never re-indexes:
  `apply.reindex` names the roots to index again.
- **A second sanctioned write path.** Until now `CHANGESPEC.md`, written by
  `record_track`, was the one file livespec writes into a project.
  `plan_schema_change(apply=True)` is the second: it rewrites existing schema
  declaration files, only inside `apply_workspaces` and only when each still
  hashes as indexed.
- Core tool count goes from 40 to 41 (56 in all with the Spec and docs
  plugins).

## Capabilities

### New Capabilities

- `schemas`: schema declarations with typed fields, `@schema:` entity
  bindings, links between schemas of one entity across repositories,
  field-level drift between them, the cross-repo impact of changing one of
  their fields, and the plan (and, on request, the write) of that change in
  every declaration.

### Modified Capabilities

None.

## Impact

`domain/schema_types.py` (new: neutral types, per-language mappers, name keys),
`domain/sql_schema.py` (new: pure-Python `CREATE TABLE` reader),
`domain/extractors.py` (`ExtractedField`, `ExtractedSchema`,
`ExtractResult.schemas`, new kinds `type_alias`, `variable`, `table`),
`domain/languages.py` (`.sql` extension, `SCHEMA_ONLY_LANGUAGES`),
`domain/matcher.py` (`parse_schema_annotations`, `scan_schema_annotations`,
`NON_SPEC_ANNOTATION_VERBS`), `domain/indexer.py` (`_insert_schemas`,
`_resolve_schema_links`, `.sql` files walked), `domain/schema_drift.py` (new:
comparator), `config.py` (`sql` accepted in `[index].languages`),
`storage/db.py` + `storage/schema.sql` (migration 27), `tools/schemas.py`
(new: `schema_drift`), `tools/specs.py` (`scan_annotation_verbs` skips
`@schema`), `resources.py` (two resources), `server.py` (tool
registration), the Skill (`plugin/skills/livespec/SKILL.md`), the agent
playbook (`templates/AGENT_PLAYBOOK.md`), `README.md` and `CHANGELOG.md`.

Phase 2: `domain/schema_impact.py` (new: field matching, declaration actions
and the three reader channels, shared by both tools), `domain/schema_drift.py`,
`tools/schemas.py` (`schema_impact`), `tools/analysis.py` (the
`git_diff_impact` section), `param_descriptions.py`, `domain/sql_schema.py`
(backslash escapes), `domain/extractors.py` and `domain/schema_types.py`
(Pydantic aliases), and the docs: the Skill, the agent playbook, `README.md`,
`CLAUDE.md` and `CHANGELOG.md`.

Phase 3: `domain/schema_edit.py` (new: the pure text engine that renders one
field change as line edits per flavor, the name styles, the renderable types
and the SQL migration hint), `domain/schema_plan.py` (new: the planner, the
freshness check by content hash and the apply writer), `tools/schemas.py`
(`plan_schema_change`), `param_descriptions.py`, the record_track docstring
in `tools/tracks.py` and `domain/changespec.py` (the single-write rule now
names two sanctioned writes), the fixtures of a three-repository group in
`tests/fixtures/schema_group/`, and the docs: the Skill, the agent playbook,
`README.md`, `CLAUDE.md`, `docs/AGENT_QUICKSTART.md`,
`docs/BETA_CHECKLIST.md`, the cross-repo guide, `CHANGELOG.md`, `HANDOFF.md`
and `ROADMAP.md`. No storage change: there is no migration 28.
