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
field. This is phase 1 of 3.

## What Changes

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

## Capabilities

### New Capabilities

- `schemas`: schema declarations with typed fields, `@schema:` entity
  bindings, links between schemas of one entity across repositories, and
  field-level drift between them.

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
