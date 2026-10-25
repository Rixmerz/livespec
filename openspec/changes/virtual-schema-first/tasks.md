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

## 6. Phase 2

- [x] 6.1 OpenSpec deltas: requirements `Cross-repo schema impact` and `Schema impact of a diff`, new scenarios on `Schema field extraction` and `Field-level schema drift`, phase 2 decisions in `design.md` (P5, `2ca5d55`); the change holds eight `schemas-*` requirements
- [x] 6.2 SQL string literals honour backslash escapes, a doubled `''` still escapes, an unclosed escape reading falls back to the standard one (`domain/sql_schema.py`, P6 `57cd00b`); verified by its three tests in `tests/test_schema_extraction_fixes.py`
- [x] 6.3 A Pydantic `Field(alias=...)` (value or `Annotated`) keys the field by its alias (`domain/extractors.py`, `domain/schema_types.py`, P6 `57cd00b` + `61d9d60`); verified by its two tests in `tests/test_schema_extraction_fixes.py`, the end-to-end `schema_drift` test included (it ran, no skip)
- [x] 6.4 `domain/schema_impact.py`: field matching, declaration actions, the three reader channels (`graph` over the group's union graph, `schema_binding`, `spec:<id>`), roles, `diff_schema_impact` (P7, `71e733b`)
- [x] 6.5 `schema_impact` core tool (`limit` / `cursor` / `summary_only`, exact counts, `mcp_error` with `did_you_mean`, `group_db` and no-graph-reader hints) in `tools/schemas.py` (P7)
- [x] 6.6 `git_diff_impact` gains `schema_impact` and `counts.schema_entities` only when a changed symbol is a bound schema declaration; any other payload is unchanged key for key (P7)
- [x] 6.7 Parameter descriptions, Skill, agent playbook (`docs/` = `templates/`), README, CLAUDE.md and the pagination lists: 40 core, 55 total (P7); cross-repo guide (`docs/` = `templates/`), CHANGELOG, HANDOFF and ROADMAP (P8)
- [x] 6.8 P7 tests: `tests/test_schema_impact.py` and `tests/test_git_diff_schema_impact.py`; full suite in the P7 tree 1085 collected, 1085 passed (1060 at `a44cf22`); `ruff check src tests` clean; `openspec validate --all --strict` 13 passed
- [x] 6.9 P6 tests: `tests/test_schema_extraction_fixes.py`, 5 collected, 5 passed
- [x] 6.10 Full suite with P6 and P7 in one tree: 1093 collected, 1093 passed
- [ ] 6.11 `ruff format --check src tests` clean: not met, `tests/test_schema_index.py:316` would be reformatted (pre-existing since `a44cf22`, the same on `main`)
- [x] 6.12 Dogfood: an archived copy validates strict and links `schemas-cross-repo-schema-impact` and `schemas-schema-impact-of-a-diff` to code; a two-repo group lists declarations with actions and per-project readers with file:line for remove, rename and retype, and `git_diff_impact` reports the same entity for a commit that drops a field; livespec itself answers with the expected hints
