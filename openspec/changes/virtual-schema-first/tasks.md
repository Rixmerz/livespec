## 1. Domain

- [x] 1.1 Neutral type model in `domain/schema_types.py`: 13 neutral types, `FLAVORS`, `WIRE_FLAVORS`, `name_key`, and the Python / TypeScript / Zod / SQL mappers (optional and nullable kept apart)
- [x] 1.2 `ExtractedField`, `ExtractedSchema` and `ExtractResult.schemas` in `domain/extractors.py` for Pydantic models, dataclasses, TypeScript interfaces, TypeScript object type aliases (`type_alias`) and Zod `z.object` / `.extend` consts (`variable`), same-file bases resolved
- [x] 1.3 Pure-Python `CREATE TABLE` reader in `domain/sql_schema.py` (`table` symbols, docstring from the comments above, reformat-stable body hash); `.sql` maps to the schema-only language `sql`, outside the grammar prefetch
- [x] 1.4 `@schema: <entity>` in `domain/matcher.py`: `parse_schema_annotations`, `scan_schema_annotations`, `NON_SPEC_ANNOTATION_VERBS` (no Spec links, no unknown ids)
- [x] 1.5 `indexer._resolve_schema_links`: database-wide, `INSERT OR IGNORE` only, after `scan_annotations` and `scan_schema_annotations`
- [x] 1.6 Field-level comparator in `domain/schema_drift.py` (`missing_field`, `type_mismatch`, `nullability_mismatch`, `optional_mismatch`, wire-compatible pairs, `uncomparable`)

## 2. Storage

- [x] 2.1 Migration 27 `schema_symbols`: `schema_decl`, `schema_field`, `schema_binding`, `schema_link` (+ `schema.sql`), re-extract queued only when the database already holds symbols
- [x] 2.2 `indexer._insert_schemas` persists `ExtractResult.schemas`; `.sql` files are walked; `[index].languages` accepts `sql`

## 3. Tools

- [x] 3.1 `schema_drift` core tool (`limit` / `cursor` / `summary_only`, exact counts, unknown entity as `mcp_error` with `did_you_mean`)
- [x] 3.2 Resources `project://schemas` and `project://schemas/{entity}`
- [x] 3.3 `scan_annotation_verbs` skips `@schema`
- [x] 3.4 Parameter description, Skill, agent playbook, README and CLAUDE.md counts (39 core, 54 total)

## 4. Verification

- [x] 4.1 Extraction tests over Python, TypeScript, Zod and SQL fixtures, including livespec's own `storage/schema.sql` (`tests/test_schema_extraction.py`)
- [x] 4.2 Persistence, binding and cross-repo link tests over a two-repo `group_db` (`tests/test_schema_index.py`); migration 27 forward and rollback (`tests/test_migrations.py`)
- [x] 4.3 Comparator units and a two-repo end-to-end drift run with one planted finding per kind, pagination and both resources (`tests/test_schema_drift.py`)
- [x] 4.4 Full suite green (965 -> 1057 passed), `ruff check`, `ruff format --check`, `openspec validate --all --strict`
- [x] 4.5 Dogfood: an archived copy of this repo validates strict and links all six requirement ids; a two-repo group reports every drift kind; livespec's own `schema.sql` indexes without a parse error

## 5. Release

- [x] 5.1 `CHANGELOG.md` `[Unreleased]`, `HANDOFF.md`, `ROADMAP.md` and the cross-repo guide (`docs/CROSS_REPO.md` = `templates/CROSS_REPO.md`)
