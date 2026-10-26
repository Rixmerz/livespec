## Context

The same entity is declared once per layer: a Zod schema or TypeScript types in
the frontend, a Pydantic model, dataclass or TypeScript interface in the
backend, a `CREATE TABLE` in a migration. livespec indexes every one of those
files as symbols, but records no fields, so it cannot say that two
declarations describe the same thing or that they disagree. Specs are scoped by
`project_id` (`storage/schema.sql`); the one thing that already crosses
repositories is `_resolve_routes`, which joins HTTP routes of every project in
a shared `group_db`. Phase 1 adds fields, bindings, links and drift; phases 2
and 3 build on them. Phase 2 adds the cross-repo impact of changing one field
(`schema_impact` and a `schema_impact` section in `git_diff_impact`) and fixes
two extraction bugs (SQL backslash escapes, Pydantic aliases). Phase 3 turns
one field change into the exact edits of every declaration
(`plan_schema_change`), with an apply mode that writes them; the change is
archived at the end of phase 3, which makes its ten `schemas-*` requirement
ids real specs.

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

## Phase 2 decisions

- **Field matching.** A declaration's field matches the requested `field` when
  `name_key(field)` equals the field's `field_key` or `name_key` of its name as
  written, so both a Python attribute and its alias match.
- **Change kinds and actions.** `change` is `add`, `remove`, `rename` or
  `retype`; `rename` needs `new_name` and `retype` needs `new_type`, which must
  be one of the 13 neutral types. Each declaration of the entity gets one
  action:

  | Change | Actions |
  |---|---|
  | `add` | `add` (the declaration lacks the field), `present` |
  | `remove` | `remove`, `absent` |
  | `rename` | `rename` (holds the old name, lacks the new), `both_present`, `renamed` (holds the new, lacks the old), `absent` |
  | `retype` | `retype`, `compatible` (its current type already carries `new_type` under the drift comparator's rules for its flavor, e.g. a TypeScript `string` for `uuid`), `absent` |

  `add`, `remove`, `rename` and `retype` are the actions counted as
  `to_change`.
- **Scope.** `st.group_project_ids()`: the home project first, then the rest of
  the group by project id. A project's rank is its position in that list.
  Declarations come in `load_entities` order (rank, file, line), each with its
  matched field (name, key, types, flags, line) or `null`.
- **Three reader channels, tagged in `via`.** One row per symbol, never one of
  the entity's own declaration symbols:
  - `graph`: the backward cone over **all** edge types, `min_weight` 0.0, up to
    `max_depth` (default 3), on the union of `load_graph(conn, pid).g` for
    every project in scope. The graphs are composed, so `invokes_route` edges
    cross repositories: a frontend function that calls a backend route reaches
    the backend handler. The cone starts from every declaration whose action is
    `add`, `remove`, `rename`, `retype` or `both_present`.
  - `schema_binding`: symbols bound with `@schema: <entity>` that are not
    schemas (`load_entities` `not_schemas`).
  - `spec:<spec_id>`: symbols linked through `spec_symbol` (any relation) to a
    Spec whose `spec_id` equals that of a Spec linked to one of the
    declarations, within scope; mirrored `xrepo-*` ids join repositories.
- **Reader rows.** `via` is the sorted distinct channel tags; `depth` the
  fewest graph hops, or `null`; `reads` the sorted qualified names of the
  declarations the reader reaches (`[]` for a `schema_binding`-only reader).
  `role` is `endpoint` when the symbol has a `route_ref` row with role
  `server` (`http_method` / `http_path` from its first such row by line), else
  `test` when its file is a test path by `_is_test_file_path` (the rule
  `find_dead_code` uses), else `symbol`. Readers are ordered by rank, file,
  line and qualified name.
- **Readers are per declaration, not per field.** livespec records no
  field-level reads: a symbol that constructs `User(...)` is listed for every
  field of `User`. Narrowing to the field needs attribute-level references,
  which the extractors do not produce.
- **Pagination and payload.** `limit` / `cursor` / `summary_only` page the
  readers only, with exact counts and `next_cursor`; `summary_only` drops the
  reader rows and keeps everything else. The payload holds `entity`,
  `entity_key`, `change`, `scope`, `declarations`, `readers`, `by_project`
  (projects with a declaration or a reader, in rank order) and `counts`
  (`declarations`, `to_change`, `both_present`, `readers`, `endpoints`,
  `tests`, `symbols`). A hint is added when the workspace has no `group_db`,
  or when the graph channel found no reader.
- **Errors.** An unknown entity (with close entity names and the
  `schema_drift` hint), an unknown change kind (with the four kinds), `rename`
  without `new_name`, `retype` without `new_type`, a `new_type` outside the
  neutral types (with close neutral types), and a `remove`, `rename` or
  `retype` naming a field no declaration of the entity holds (with close field
  names as written) each return an `mcp_error`.
- **`analyze_impact` does not follow schema links.** This closes the open item
  phase 1 left. `schema_link` stays out of `symbol_edge`, for the graph-honesty
  reason above, and `schema_impact` is the explicit path from a schema change
  to its readers in other repositories. An agent that asks "what breaks if I
  change this function?" keeps the call-graph answer; one that asks "what
  breaks if I drop this field?" calls the tool built for it.
- **The `git_diff_impact` section.** Only when at least one changed symbol,
  after the symbol-precise filter, is a `schema_decl` symbol with a
  `schema_binding` row does the payload gain `schema_impact` (a list, one entry
  per entity, sorted by entity key) and `counts.schema_entities`. Otherwise the
  payload is unchanged key for key, the empty early return included. Each
  entry names the changed declarations with their field changes, read from the
  git blobs at base and head and matched by field key: `added`, `removed`,
  `retyped` (type or item type differ) or `modified` (only optional or nullable
  differ). A rename shows as `removed` + `added`. `added`, `removed` and
  `retyped` give the entity's other declarations `add`, `remove` and `retype`
  actions. An unresolvable ref or an unparsable blob gives `field_changes`
  `null`, a `field_changes_unknown` reason and no actions. Readers are the
  `schema_impact` readers over every declaration of the entity, at depth 3
  whatever `git_diff_impact`'s `max_depth` is, reported as counts per project
  plus a sample of the first 5 readers of each project; `summary_only` drops
  the sample and keeps the counts. Each entry carries `follow_up` calls of
  `schema_impact` for its field changes.
- **SQL string literals.** Inside a single-quoted literal a backslash escapes
  the next character (the MySQL / MariaDB default, Postgres `E'...'`), and a
  doubled `''` still escapes a quote. When the escape reading never closes the
  literal, the standard reading (backslash as a literal character) is used.
  `CREATE TABLE a (id INT, note TEXT DEFAULT 'it\'s');` followed by
  `CREATE TABLE b (id INT);` records both tables.
- **Pydantic aliases.** A field whose value is
  `Field(..., alias='<string literal>')`, or whose annotation is
  `Annotated[X, Field(..., alias='<string literal>')]`, gets
  `field_key = name_key(alias)`: the alias is the wire name, which is what
  other repositories see. `name` stays the Python attribute as
  written; type, optional and nullable are unchanged. No storage change and no
  migration: existing indexes pick it up on a re-extract
  (`index_project(force=True)`), and since migration 27 is unreleased, users
  get a full re-extract anyway.
- **Tool count.** `schema_impact` is core, since it answers "what breaks if I
  change this field?": 40 core + 12 Spec plugin + 3 docs plugin = 55 tools.

## Phase 3 decisions

- **One core tool, two new modules, nothing frozen touched.**
  `plan_schema_change` is registered by `tools/schemas.py` next to
  `schema_drift` and `schema_impact`: 41 core + 12 Spec plugin + 3 docs plugin
  = 56 tools. It is core because it answers the agent's next question after
  `schema_impact`, "what exactly do I write in each repository?". The text
  engine (`domain/schema_edit.py`) is pure: it takes a source string and
  returns edits, with no database and no filesystem, so every flavor and
  every change kind can be tested by applying the edits and re-extracting the
  result. The planner and the writer (`domain/schema_plan.py`) own the
  database, the file reads and the writes. `domain/schema_impact.py`,
  `schema_drift.py`, `schema_types.py`, `extractors.py`, `sql_schema.py`,
  `indexer.py` and `storage/` are unchanged, so phases 1 and 2 behave as
  before. `domain/` does not import `tools/`; the test-path rule is injected,
  as `schema_impact` does.
- **No migration 28.** The index stores a field's line, not its column span.
  Rather than store spans, the engine re-parses each declaration from its
  file (`ast` for Python, tree-sitter for TypeScript and Zod, the
  `sql_schema` lexer for SQL) and places every field by its own span, after
  checking that the file still hashes as indexed. The indexed line is
  verified against the parse, never trusted: a field not at its indexed line
  is `manual`. A stored span would add a migration and a re-extract for data
  that a hash-verified re-parse gives exactly.
- **Five change kinds; `optionality` is planner-only.** `add`, `remove`,
  `rename` and `retype` delegate to `schema_impact.declaration_action`, so a
  declaration gets the same action from both tools. `optionality` (with
  `optional`, `nullable` or both) exists only in the planner: it returns `set`
  when the side holds the field and a requested flag differs (a SQL table
  compares `nullable` only), `unchanged` or `absent`. `schema_impact` keeps
  its four kinds and its payload; its readers for `set` declarations are
  found with the same `find_readers`.
- **Nine renderable types.** A type is written only when every flavor has an
  exact spelling for it:

  | Neutral | Pydantic / dataclass | TypeScript | Zod | SQL |
  |---|---|---|---|---|
  | `string` | `str` | `string` | `z.string()` | `TEXT` |
  | `int` | `int` | `number` | `z.number().int()` | `INTEGER` |
  | `float` | `float` | `number` | `z.number()` | `DOUBLE PRECISION` |
  | `decimal` | `Decimal` | `string` | `z.string()` | `NUMERIC` |
  | `bool` | `bool` | `boolean` | `z.boolean()` | `BOOLEAN` |
  | `datetime` | `datetime` | `string` | `z.string().datetime()` | `TIMESTAMP` |
  | `date` | `date` | `string` | `z.string().date()` | `DATE` |
  | `uuid` | `UUID` | `string` | `z.string().uuid()` | `UUID` |
  | `json` | `dict[str, Any]` | `unknown` | `z.unknown()` | `JSON` |

  `list`, `ref`, `enum` and `unknown` need an item type, a target or a member
  list that one `new_type` word cannot carry, so `add` or `retype` to them is
  an `mcp_error` that points to a hand edit and to `schema_impact`. A Python
  name the module does not bind gets one import line after its last top-level
  import (`from uuid import UUID`, `from decimal import Decimal`, `from
  datetime import datetime` / `date`, `from typing import Any`). The
  invariant, tested for every flavor and type: re-extracting the edited source
  gives the field a type that carries the requested one under the side's
  flavor, by the drift comparator's wire rules. Optional and nullable follow
  each language: Python `X | None` (`Optional[X]` when the file already uses
  it and never `| None`), an optional Python field needs a default (`= None`
  when nullable); TypeScript `name?:` and `| null`; Zod `.optional()`,
  `.nullable()`, both `.nullish()`; SQL `NOT NULL`, and a column's
  "optional" is never edited, for the reason phase 1 gives.
- **Each side keeps its naming style.** A new or renamed field is spelled in
  the style of the side's matched field, else the most common style among its
  fields, else the flavor's default (snake for Pydantic, dataclass and SQL;
  camel for TypeScript and Zod). Python attributes and SQL columns are always
  snake. So one rename of `email` to `contact_email` writes `contact_email`
  in Python and SQL and `contactEmail` in TypeScript and Zod: the
  convention-free key stays the same on every side, and `schema_drift` stays
  clean after the apply.
- **Aliases are renamed at the wire name.** A Pydantic field keyed by
  `Field(alias='...')` is renamed by rewriting the alias literal, in the
  alias's style, and keeps its Python attribute, since the alias is what the
  other repositories see and the attribute is what the Python code calls.
  `retype` and `optionality` edit only the annotation or the default. An
  alias keyed like its attribute moves with it, or the field would keep its
  old key.
- **Four statuses, and no guessing.** `edit` carries non-overlapping whole-line
  edits sorted by line: a 1-based inclusive range, the exact current text
  (line endings included) and the new text, `''` to delete; an insertion
  replaces its anchor line with the anchor plus the new lines. `none` means
  the side needs nothing (`present`, `absent`, `renamed`, `compatible`,
  `unchanged`). `manual` carries a one-sentence reason whenever an exact edit
  cannot be placed: a field inherited from a same-file base (editing the base
  changes every subclass), a field not at its indexed line, a field sharing
  its line with another statement, an optional non-nullable Python field (it
  needs a default value), a dataclass required field after a defaulted one, a
  SQL column a table constraint names, a file that is not UTF-8, a missing
  grammar, anything else that cannot be placed exactly, and a rename whose
  side already holds both names (`both_present`). `stale` means
  the file changed since indexing, is missing or resolves outside its project
  root, and no edit is computed for it. Declarations are never paged; the
  readers are, under the aggregator pagination contract.
- **The SQL migration hint is text.** A `CREATE TABLE` edit carries a
  PostgreSQL statement (`ADD COLUMN`, `DROP COLUMN`, `RENAME COLUMN`, `ALTER
  COLUMN ... TYPE`, `DROP` / `SET NOT NULL`) naming the table as written. It is
  never written: livespec does not know the project's migration tool,
  numbering or dialect, and creating a migration file is a decision for the
  person.
- **Freshness and containment.** A declaration's file is `Path(project.root)
  / file.path`, resolved, and it must lie inside the resolved project root, so
  a symlink or a `..` path cannot lead a write elsewhere (CWE-22). It is fresh
  when it exists and hashes (`indexer._hash_bytes`) to the indexed
  `content_hash`. Plan mode reads and never writes.
- **Apply is explicit and all-or-nothing on its checks.** `apply=True` with
  `apply_workspaces` (default: the calling workspace; each entry must be the
  root of a project in scope) writes only the files of those projects. Edits
  elsewhere, `manual` declarations and `both_present` go to
  `apply.not_applied` with their reason. Every check runs before the first
  byte: a `stale` declaration inside `apply_workspaces`, or a file that no
  longer takes its edits, refuses the whole apply with an `mcp_error` and
  nothing written. Each file is hashed again right before it is replaced
  through a temp file in its directory and `os.replace`, keeping its mode and
  line endings, under `st.lock()`. No file is created, the migration hint is
  not written, and nothing is re-indexed: `apply.reindex` names the roots to
  index again.
- **Why a second write path exists.** livespec writes into a project only
  what a person asked for by name: `CHANGESPEC.md` through `record_track` was
  the one such write. A coordinated field change across repositories is
  exactly the edit an agent would otherwise make by hand, file by file, from
  the plan, with no hash check and no containment; doing it in the tool keeps
  those guarantees. It stays opt-in (`apply=True`), scoped
  (`apply_workspaces`) and hash-checked, it only rewrites existing schema
  declaration files, and the single-write rule now names both writes
  (`CLAUDE.md`, `README.md`, `tools/tracks.py`, `domain/changespec.py`).

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
- **Ids unknown until archive.** The change stayed unarchived until phase 3
  finished, so until then its `schemas-*` requirement ids (eight after phase
  2, ten after phase 3) existed only as change deltas and the store reported
  the `@spec:` annotations on the new code as unknown ids. Archiving at the
  end of phase 3 moves all ten requirements into `openspec/specs/schemas/`,
  and the next `sync_openspec` makes them known.
- **Re-extract on upgrade.** Migration 27 queues a full re-extract of any
  index that already holds symbols, a one-time cost on the first
  `index_project` after the upgrade.
- **Type-position uses are not edges.** Verified: a TypeScript
  `function render(u: User)` yields no reference, Python
  `User.model_validate(d)` yields a reference to `model_validate`, and only a
  constructor call such as `User(...)` or `User(**d)` reaches `User`. So the
  graph readers of TypeScript, Zod and SQL declarations are mostly HTTP callers
  reached through routes, and the zero-reader hint says that type-position uses
  are not call edges and points to `grep_in_indexed_files`. If real
  repositories come back empty, revisit with type-position references kept out
  of `symbol_edge`.
- **An odd trailing backslash in standard SQL.** A standard-SQL literal ending
  in an odd backslash (`'C:\'`) and followed later in the file by another quote
  is misread under the escape reading.
- **Only literal `alias=` is followed.** `alias_generator`, `validation_alias`,
  `serialization_alias` and non-literal aliases are not, so such a field still
  matches by its attribute name.
- **A removed declaration gets no section.** A schema declaration the range
  removes entirely has no `schema_impact` entry in `git_diff_impact`; it stays
  in `changed_symbols`.
- **Spec-linked readers can be broad.** A Spec linked to a schema may also be
  linked to many symbols that never touch the entity; every one of them is
  listed with `via` `spec:<id>`, so an agent can tell them from graph readers.
- **A `CREATE TABLE` edit may rewrite an applied migration.** When the table
  lives in a migration that has already run, editing its `CREATE TABLE`
  changes history: a migration tool that checksums applied files (Flyway and
  Liquibase do) will refuse it, and an existing database never sees the
  change. Leave that repository out of `apply_workspaces` and
  use the migration hint to write a new migration. Revisit (for example, a
  flag that skips `sql_table` edits) if users report checksum breaks.
- **`list`, `ref` and `enum` are not rendered.** An `add` or `retype` to
  them, or to `unknown`, is an `mcp_error`; the field is edited by hand and
  `schema_impact` still gives its impact.
- **An optional non-nullable Python field is manual.** Python has no way to
  say "may be absent" without a default, and livespec does not invent one, so
  such a declaration is `manual` with the reason `needs a default value`.
- **Apply is all-or-nothing on its checks, not atomic across files.** Every
  check runs before the first write and each file is replaced atomically, but
  an `OSError` on the third of four files leaves the first two written. The
  error names the files already written, so the person can finish or revert
  them; nothing rolls them back.
- **Readers are per declaration.** As in phase 2, a reader of `User` is listed
  for every field of `User`; the plan does not narrow its readers to the
  field it changes.
