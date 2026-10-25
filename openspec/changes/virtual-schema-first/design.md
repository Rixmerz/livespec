## Context

The same entity is declared once per layer: a Zod schema or TypeScript types in
the frontend, a Pydantic model, dataclass or TypeScript interface in the
backend, a `CREATE TABLE` in a migration. livespec indexes every one of those
files as symbols, but records no fields, so it cannot say that two
declarations describe the same thing or that they disagree. Specs are scoped by
`project_id` (`storage/schema.sql`); the one thing that already crosses
repositories is `_resolve_routes`, which joins HTTP routes of every project in
a shared `group_db`. Phase 1 adds fields, bindings, links and drift; phases 2
and 3 build on them.

## Decisions

- **Neutral types.** 13 types: `string`, `int`, `float`, `decimal`, `bool`,
  `datetime`, `date`, `uuid`, `json`, `enum`, `list`, `ref`, `unknown`.
  `unknown` means no rule maps the type: unions of two non-null types, tuples,
  callables, TypeScript function types, `z.union` / `z.discriminatedUnion` /
  `z.lazy`, SQL `BLOB` / `BYTEA` / `TIME` / `INTERVAL` and any other unlisted
  type. A field carries `type`, `item_type` (the element type when `type` is
  `list`), `ref_target` (the type name as written, last dotted segment,
  generics stripped, when `type` or `item_type` is `ref`), `raw_type` (as
  written), `optional` and `nullable`. Names match through a key that
  casefolds and drops `_`, `-`, `.` and whitespace, so `user_id`, `userId` and
  `UserID` are one field (`userid`); entity names use the same key.
- **Flavors.** `pydantic`, `dataclass`, `ts_interface`, `ts_type`, `zod`,
  `sql_table`. The wire flavors, which only ever see JSON, are `ts_interface`,
  `ts_type` and `zod`.
- **Pydantic v2 semantics.** Under Pydantic v1 an `Optional[X]` field without a
  default was optional (it defaulted to `None`); v2 makes it required and
  nullable. livespec follows v2: `Optional[X]`, `X | None` and
  `Union[X, None]` make a field nullable with the type of `X`, and only a
  default makes it optional. A default is any assigned value except `...`, and
  except a `Field(...)` call with no default (no positional default or `...`,
  and no `default=` / `default_factory=`). A dataclass `field()` without
  `default` / `default_factory` is required. `Annotated[X, ...]` maps as `X`.
- **Optional vs nullable, per language.** They are never derived from each
  other.

  | Language | optional (may be absent) | nullable (may hold null) |
  |---|---|---|
  | Python | has a default (rules above) | `Optional[X]`, `X \| None`, `Union[X, None]` |
  | TypeScript | `name?:` or `\| undefined` | `\| null` |
  | Zod | `.optional()`, `.nullish()`, `.default(..)` | `.nullable()`, `.nullish()` |
  | SQL | `DEFAULT`, a `SERIAL` type, `AUTOINCREMENT`, `IDENTITY`, `GENERATED`, `INTEGER PRIMARY KEY` | no `NOT NULL` and not `PRIMARY KEY` |

- **Per-language type rules** live in `domain/schema_types.py`, one mapper per
  language:
  - Python: `str` -> `string`; `int` -> `int`; `float` -> `float`; `Decimal`
    -> `decimal`; `bool` -> `bool`; `datetime` -> `datetime`; `date` ->
    `date`; `UUID` -> `uuid`; `dict` / `Dict` / `Mapping` / `Any` / `object` /
    `Json` / `JsonValue` -> `json`; `list` / `List` / `Sequence` / `set` /
    `Set` / `frozenset` / `tuple[X, ...]` -> `list` of `X`; `Literal[...]` ->
    `enum`; an `Enum` / `StrEnum` / `IntEnum` subclass declared in the same
    file -> `enum`; `EmailStr` / `HttpUrl` / `AnyUrl` / `SecretStr` / `constr`
    -> `string`; `conint` -> `int`; `confloat` -> `float`; `condecimal` ->
    `decimal`; `bytes` -> `string`; any other name -> `ref`.
  - TypeScript: `string` -> `string`; `number` -> `float`; `bigint` -> `int`;
    `boolean` -> `bool`; `Date` -> `datetime`; `any` / `unknown` / `object` /
    `Record<..>` / an index signature / an inline object type -> `json`; `T[]`
    / `Array<T>` / `ReadonlyArray<T>` -> `list`; a union of string or number
    literals -> `enum`; a same-file `enum X` or a same-file alias of a literal
    union -> `enum`; any other type identifier -> `ref`; tuples, function
    types and other unions -> `unknown`.
  - Zod: `z.string()` -> `string` (`.datetime()` -> `datetime`, `.date()` ->
    `date`, `.uuid()` -> `uuid`); `z.number()` -> `float` (`.int()` -> `int`);
    `z.bigint()` -> `int`; `z.boolean()` -> `bool`; `z.date()` -> `datetime`;
    `z.enum([...])` / `z.nativeEnum(X)` / `z.literal(..)` -> `enum`;
    `z.array(X)` or `X.array()` -> `list`; `z.record` / `z.any` / `z.unknown` /
    an inline `z.object` -> `json`; the identifier of another schema const ->
    `ref`; `z.coerce.X()` maps as `X`.
  - SQL: `TEXT` / `VARCHAR(n)` / `CHAR(n)` / `CHARACTER VARYING` / `CITEXT` /
    `CLOB` -> `string`; `INTEGER` / `INT` / `SMALLINT` / `BIGINT` / `TINYINT` /
    `MEDIUMINT` / `INT2` / `INT4` / `INT8` / `SERIAL` / `BIGSERIAL` /
    `SMALLSERIAL` -> `int`; `REAL` / `FLOAT` / `FLOAT4` / `FLOAT8` / `DOUBLE` /
    `DOUBLE PRECISION` -> `float`; `NUMERIC` / `DECIMAL` / `MONEY` ->
    `decimal`; `BOOLEAN` / `BOOL` -> `bool`; `TIMESTAMP` / `TIMESTAMPTZ` /
    `TIMESTAMP WITH[OUT] TIME ZONE` / `DATETIME` -> `datetime`; `DATE` ->
    `date`; `UUID` / `UNIQUEIDENTIFIER` -> `uuid`; `JSON` / `JSONB` -> `json`;
    `X[]` or `ARRAY` -> `list`; `ENUM(..)` or a same-file
    `CREATE TYPE x AS ENUM` -> `enum`.
- **Same-file inheritance only.** Pydantic and dataclass bases declared in the
  same file, TypeScript `extends` of a same-file interface and Zod
  `X.extend({...})` of a same-file schema are resolved: base fields first, then
  the declaration's own. A base from another file is not followed; resolving it
  needs the import graph at extraction time, which extraction does not have.
- **Symbols.** Pydantic models, dataclasses and TypeScript interfaces stay
  `class` symbols. A TypeScript object type alias is a new `type_alias` kind; a
  Zod `const X = z.object(...)` is a new `variable` kind; a SQL `CREATE TABLE`
  is a new `table` kind named `<module>.<table>`, whose docstring is the run of
  `--` or `/* */` comment lines right above it and whose body hash is seeded
  from the statement without comments, whitespace collapsed, so a reformat does
  not drift it. A class that is neither a Pydantic model nor a dataclass gets
  no schema.
- **Extraction output.** `ExtractResult.schemas` is a list of
  `ExtractedSchema(src_qname, flavor, fields)`, where `src_qname` is the
  qualified name of an `ExtractedSymbol` in the same result and each field is
  an `ExtractedField(name, field_key, type, item_type, ref_target, raw_type,
  optional, nullable, line)`. The indexer persists them through
  `_insert_schemas`, so every language feeds one storage path.
- **SQL in pure Python, outside the grammar prefetch.** `.sql` maps to the
  language `sql`, which is in `SCHEMA_ONLY_LANGUAGES` and in
  `ANNOTATION_SUPPORTED_LANGUAGES` but not in `EXTRACTOR_SUPPORTED`, the set
  that drives the grammar prefetch. `domain/sql_schema.py` reads `CREATE TABLE`
  without tree-sitter and never calls `get_parser`: SQL dialects are many, a
  grammar download can fail behind a proxy, and only the column list is
  needed. `CREATE VIRTUAL TABLE`, `CREATE INDEX`, `CREATE TRIGGER`,
  `ALTER TABLE`, `PRAGMA` and table-level constraints are skipped. The file
  walk (`indexer._iter_files`) takes `EXTRACTOR_SUPPORTED |
  SCHEMA_ONLY_LANGUAGES`, and `config.py` accepts `sql` in `[index].languages`,
  keeping the `unsupported languages` error prefix for anything else.
- **`@schema` stays out of the Spec verb vocabulary.** `@schema: <entity>`
  follows the line-start rules of `@spec:` (optional `#` or `*` leader; entity
  token `[A-Za-z][A-Za-z0-9_.-]{0,79}`, first token only; several lines give
  several bindings), but `schema` never joins `RECOGNIZED_PREFIX_VERBS` or
  `_PREFIX_HEAD_RE`. An entity is not a Spec: a Spec named like an entity must
  not gain a `spec_symbol` row, and an entity must not be reported as an
  unknown Spec id. `matcher.NON_SPEC_ANNOTATION_VERBS` names it so that
  `scan_annotation_verbs` skips it instead of flagging it. A binding on a
  symbol that is not a schema is kept and shown under `not_schemas`. A
  binding's line is the symbol's start line.
- **A dedicated `schema_link` table, not `symbol_edge` rows.** Graph honesty:
  `graph.py:160-174` records that ingested `references` / `inherits` rows in
  `symbol_edge` turned `who_calls` for one class from 2 callers into 40, and
  `INVOCATION_EDGE_TYPES` had to be added to undo it. A schema link is not a
  call and not a dependency of code on code. Kept out of `symbol_edge`, it has
  no effect on `who_calls`, `who_does_this_call`, PageRank or dead-code
  results. Whether `analyze_impact` should follow schema links across
  repositories is a phase-2 decision, made with data.
- **Linking.** Inside `index_project`'s transaction, after `_resolve_refs`,
  `_resolve_routes` and `scan_annotations`, come `scan_schema_annotations` and
  then `_resolve_schema_links`, which is database-wide: every pair of distinct
  bound symbols per entity key gets one `INSERT OR IGNORE` row, smaller symbol
  id first. A `group_db` makes the links cross-repository; a per-repository
  database links within the repository. Links are only inserted; they go when
  a symbol is re-extracted, through `ON DELETE CASCADE`. `index_project`'s
  payload does not change.
- **Storage.** Migration 27 `schema_symbols` adds `schema_decl`,
  `schema_field`, `schema_binding` and `schema_link`, with the same DDL in
  `storage/schema.sql`. A re-extract is queued only when the database already
  holds symbols, as migration 14 does.
- **Wire compatibility.** Equal types are compatible. Beyond equality, these
  pairs are not type mismatches:

  | Side | Compatible pairs | Why |
  |---|---|---|
  | wire (`ts_interface`, `ts_type`, `zod`) | `float` ~ `int`, `decimal` | JSON has one number type |
  | wire (`ts_interface`, `ts_type`, `zod`) | `string` ~ `datetime`, `date`, `uuid`, `decimal` | these travel as strings in JSON |
  | `sql_table` | `json` ~ `ref`, `list` | nested objects and arrays are stored as JSON columns |
  | `sql_table` | `string` ~ `enum` | enums are commonly stored as text |
  | any | `ref` ~ `ref`, any targets | targets are not compared in phase 1 |
  | any | `list` ~ `list` when the item types are compatible by these rules | |

  A field typed `unknown` on any side is never type-compared and counts once
  as `uncomparable`.
- **Optional mismatches are never judged against SQL.** A column's
  "optional" means the database fills it in on insert; a model's means the
  payload may leave it out. The two answer different questions, so
  `optional_mismatch` compares only non-`sql_table` sides. Nullability is
  compared across all flavors.
- **Drift scope and output.** `schema_drift` compares every project of a
  `group_db` (else the home project). Sides are ordered by project (home
  first), file and line; fields match by key, the first one by position
  winning within a side. Findings come in the order `missing_field`,
  `type_mismatch`, `nullability_mismatch`, `optional_mismatch`, sorted by
  entity key, kind and field key, behind an integer-offset cursor with exact
  counts. A finding's `field` is the name as written on the first side holding
  it; a side's `line` is the field's line; a `missing_in` entry's `line` is the
  schema symbol's line. `summary_only` omits `findings` and keeps every count.
  An unknown entity returns an `mcp_error` with `did_you_mean` listing close
  entity names and the hint `annotate a schema with @schema: <entity> and
  re-run index_project`. The tool is core, since it answers "do these
  declarations still agree?"; the count becomes 39 core + 12 Spec plugin + 3
  docs plugin = 54 tools.
- **Resources.** `project://schemas` serves the `summary_only` payload of
  `schema_drift` for the most recently used workspace, and
  `project://schemas/{entity}` serves every finding of one entity,
  unpaginated, so a client can read drift without walking pages.

## Risks / Trade-offs

- **`ALTER TABLE` is not folded.** A column added or dropped by a later
  migration is not applied to its table, so a table spread across migrations
  shows the `CREATE TABLE` columns only and may report false missing fields.
- **Cross-file bases are unresolved.** A model whose base class or extended
  interface lives in another file shows only its own fields.
- **Reference targets are not compared.** `ref` ~ `ref` matches whatever each
  side points to; `Address` vs `Location` is not reported.
- **Heuristic type mapping.** A custom type that a rule maps to `ref` or
  `unknown` may hide a real mismatch; `unknown` is counted, never silently
  dropped.
- **Ids unknown until archive.** The change stays unarchived until phases 2-3
  finish, so the six `schemas-*` requirement ids exist only as change deltas.
  Until the archive, the store reports the `@spec:` annotations on the new code
  as unknown ids. This is expected.
- **Re-extract on upgrade.** Migration 27 queues a full re-extract of any
  index that already holds symbols, a one-time cost on the first
  `index_project` after the upgrade.
