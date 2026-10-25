## ADDED Requirements

### Requirement: Schema field extraction

The livespec MCP server SHALL record, for every schema declaration it indexes, its flavor and its fields in source order, each with its name as written, a convention-free key, a neutral type, whether it is optional, whether it is nullable, and its line. Schema declarations are Pydantic models and dataclasses, TypeScript interfaces and type aliases of object types, Zod `z.object(...)` declarations, and `CREATE TABLE` statements in `.sql` files, which SHALL be read without a tree-sitter grammar. The convention-free key of a Pydantic field declared with `Field(alias=...)` SHALL be the key of its alias, its wire name.

#### Scenario: Pydantic model

- **WHEN** a Python file declares `class User(BaseModel)` with `id: int`, `email: str | None` and `nickname: str = 'x'`
- **THEN** `User` is recorded with flavor `pydantic`; `id` is `int`, required and not nullable; `email` is `string`, nullable and required; `nickname` is `string`, optional and not nullable

#### Scenario: Dataclass default factory

- **WHEN** a dataclass declares `tags: list[str] = field(default_factory=list)`
- **THEN** `tags` is a `list` of `string`, optional and not nullable

#### Scenario: TypeScript interface

- **WHEN** a TypeScript file declares `interface User { id: number; nickname?: string; deletedAt: string | null }`
- **THEN** `nickname` is optional and not nullable, and `deletedAt` is nullable and not optional

#### Scenario: TypeScript object type alias

- **WHEN** a TypeScript file declares `type User = { id: number }` and `type Id = string`
- **THEN** `User` is a `type_alias` symbol with flavor `ts_type` and one field, and `Id` records no schema

#### Scenario: Zod object

- **WHEN** a TypeScript file declares `const User = z.object({ id: z.string().uuid(), age: z.number().int().optional() })`
- **THEN** the symbol `User` is recorded with flavor `zod`, `id` is `uuid` and `age` is `int` and optional

#### Scenario: SQL table

- **WHEN** a `.sql` file holds `CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, bio TEXT, UNIQUE(email))`
- **THEN** a `table` symbol `users` is recorded with flavor `sql_table` and three fields, `bio` is nullable, and the `UNIQUE` constraint is not a field

#### Scenario: Plain class

- **WHEN** a Python class is neither a Pydantic model nor a dataclass
- **THEN** no schema is recorded for it

#### Scenario: SQL without a grammar

- **WHEN** a workspace with `.sql` files is indexed
- **THEN** no tree-sitter grammar is downloaded or loaded for them

#### Scenario: Pydantic field alias

- **WHEN** a Pydantic model declares `display: str = Field(alias='fullName')`
- **THEN** the field's name is `display` and its key is `fullname`

#### Scenario: Alias inside Annotated

- **WHEN** a field is declared `nick: Annotated[str, Field(alias='nickName')]`
- **THEN** its key is `nickname`

#### Scenario: Backslash-escaped quote in SQL

- **WHEN** a `.sql` file holds `CREATE TABLE a (id INT, note TEXT DEFAULT 'it\'s');` followed by `CREATE TABLE b (id INT);`
- **THEN** both tables are recorded

### Requirement: Neutral field types

The livespec MCP server SHALL map every field to one neutral type out of `string`, `int`, `float`, `decimal`, `bool`, `datetime`, `date`, `uuid`, `json`, `enum`, `list`, `ref` and `unknown` using per-language rules, and SHALL keep optional (the field may be absent) and nullable (the field may hold null) as two separate flags. A list records its item type, a reference records the name it points to, and a type that no rule maps is `unknown`.

#### Scenario: Nullable is not optional

- **WHEN** a Pydantic model declares `x: int | None` with no default
- **THEN** `x` is nullable and not optional

#### Scenario: Optional is not nullable

- **WHEN** a TypeScript interface declares `x?: number`
- **THEN** `x` is optional and not nullable

#### Scenario: Zod nullish

- **WHEN** a Zod field ends in `.nullish()`
- **THEN** the field is both optional and nullable

#### Scenario: SQL default

- **WHEN** a table declares `created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`
- **THEN** `created_at` is `datetime`, optional and not nullable

#### Scenario: List of references

- **WHEN** a model declares `items: list[OrderItem]`
- **THEN** `items` is a `list` whose item type is `ref` pointing to `OrderItem`

#### Scenario: Unmapped type

- **WHEN** a field is typed `str | int`
- **THEN** its type is `unknown`

#### Scenario: Same-file enum

- **WHEN** a field is typed with an `Enum` subclass declared in the same file
- **THEN** its type is `enum`

### Requirement: Schema entity annotation

The livespec MCP server SHALL bind a symbol to a named entity when its docstring or leading comment has a line `@schema: <entity>`, matching entities by a key that ignores case, `_`, `-`, `.` and whitespace. An `@schema:` line SHALL NOT create a Spec link, and `scan_annotation_verbs` SHALL NOT report it as an unrecognized verb.

#### Scenario: Binding across conventions

- **WHEN** a Pydantic model says `@schema: user-profile` and a TypeScript interface says `@schema: UserProfile`
- **THEN** both are bound to the same entity

#### Scenario: SQL comment

- **WHEN** the line `-- @schema: user` sits right above a `CREATE TABLE` statement
- **THEN** the table is bound to the entity `user`

#### Scenario: Not a Spec link

- **WHEN** a Spec with id `user` exists and a symbol says `@schema: user`
- **THEN** no `spec_symbol` row links that symbol to the Spec

#### Scenario: Recognized verb

- **WHEN** `scan_annotation_verbs` runs over a file holding `@schema: user`
- **THEN** it reports nothing for `@schema`

#### Scenario: Binding on a non-schema

- **WHEN** a function says `@schema: user`
- **THEN** the binding is kept and `schema_drift` lists the function under `not_schemas`, outside the comparison

### Requirement: Cross-repo schema linking

The livespec MCP server SHALL link, on every index run, every pair of schema symbols bound to the same entity anywhere in the database, so that in a shared `[workspace] group_db` the schemas of different repositories are linked and in a per-repository database the schemas of that repository are linked. Linking SHALL only insert links, and a link SHALL disappear when one of its symbols is re-extracted without the binding.

#### Scenario: Two repositories in a group

- **WHEN** a backend Pydantic model and a frontend TypeScript interface both say `@schema: user` and both repositories share one `group_db`
- **THEN** the two schemas are linked

#### Scenario: Separate databases

- **WHEN** the same two repositories are indexed without a `group_db`
- **THEN** no link joins the backend schema to the frontend schema

#### Scenario: Binding removed

- **WHEN** the `@schema:` line is removed from one of two linked symbols and its repository is indexed again
- **THEN** the link between them is gone

#### Scenario: Index order

- **WHEN** the frontend repository is indexed before the backend repository
- **THEN** their `user` schemas are linked once both are indexed

### Requirement: Field-level schema drift

The livespec MCP server SHALL expose `schema_drift(entity?)`, which, for each entity bound in the workspace's projects (every project of a `group_db`), compares the entity's schemas field by field, matching fields by their convention-free key, and reports `missing_field`, `type_mismatch`, `nullability_mismatch` and `optional_mismatch` findings naming every side with its project, file and line. Optional mismatches SHALL be judged only among non-SQL schemas; the wire-compatible type pairs SHALL NOT be type mismatches; an `unknown` type is never compared. The tool SHALL follow the aggregator pagination contract (`limit`, `cursor`, `summary_only`, exact counts) and SHALL answer an unknown entity with an `mcp_error` whose `did_you_mean` lists close entity names.

#### Scenario: Missing field

- **WHEN** the Pydantic `user` schema has `avatar_url` and the TypeScript `user` schema does not
- **THEN** a `missing_field` finding gives the Pydantic field's file:line as present and the TypeScript schema's file:line as missing

#### Scenario: Naming conventions

- **WHEN** one `user` schema has `created_at` and another has `createdAt`
- **THEN** they are compared as one field and no `missing_field` is reported for it

#### Scenario: Aliased field

- **WHEN** a Pydantic `user` schema declares `display: str = Field(alias='fullName')` and a TypeScript `user` schema declares `fullName: string`
- **THEN** no `missing_field` is reported for either

#### Scenario: Type mismatch

- **WHEN** a Pydantic `user` schema declares `age: int` and the SQL `user` table declares `age TEXT`
- **THEN** a `type_mismatch` finding lists both types

#### Scenario: Wire-compatible types

- **WHEN** a Pydantic `user` schema declares `id: UUID` and a TypeScript `user` schema declares `id: string`
- **THEN** no `type_mismatch` is reported for `id`

#### Scenario: Nullability mismatch

- **WHEN** the SQL `user` table declares `email TEXT NOT NULL` and the Pydantic `user` schema declares `email: str | None`
- **THEN** a `nullability_mismatch` finding is reported for `email`

#### Scenario: Optional mismatch

- **WHEN** a TypeScript `user` schema declares `nickname?: string` and a Pydantic `user` schema declares `nickname: str` without a default
- **THEN** an `optional_mismatch` finding is reported for `nickname`

#### Scenario: No optional mismatch against SQL

- **WHEN** a TypeScript `user` schema declares `nickname?: string` and a SQL `user` table declares `nickname TEXT NOT NULL`
- **THEN** no `optional_mismatch` is reported for `nickname`

#### Scenario: Unknown entity

- **WHEN** `schema_drift(entity='usr')` runs and only `user` is bound
- **THEN** the tool returns an `mcp_error` whose `did_you_mean` contains `user`

#### Scenario: Pagination

- **WHEN** an entity has more findings than `limit`
- **THEN** the page holds `limit` findings, `next_cursor` resumes after them, and the counts are exact

### Requirement: Schema drift resource

The livespec MCP server SHALL serve `project://schemas`, listing the bound entities of the most recently used workspace with their schemas and drift counts, and `project://schemas/{entity}`, holding every drift finding of one entity.

#### Scenario: Entity list

- **WHEN** a group with a drifting `user` entity has been indexed and `project://schemas` is read
- **THEN** it lists `user` with its schemas and non-zero drift counts

#### Scenario: One entity

- **WHEN** `project://schemas/user` is read
- **THEN** it returns the same findings as `schema_drift(entity='user')`, without pagination

### Requirement: Cross-repo schema impact

The livespec MCP server SHALL expose `schema_impact(entity, change, field, new_name?, new_type?)`, which, for a proposed `add`, `remove`, `rename` or `retype` of one field of an entity bound with `@schema:`, lists every schema declaration of that entity in the workspace's projects (every project of a `group_db`) with the action the change asks of it and the matched field's file and line, and every symbol, endpoint and test that reads those declarations through the call graph, an `@schema:` binding or a shared Spec, each with project, file and line, ordered by project with the home project first. Readers SHALL be listed per declaration, not per field. The tool SHALL follow the aggregator pagination contract and SHALL answer an unknown entity, an unknown field, an unknown change kind or a type outside the neutral types with an `mcp_error`.

#### Scenario: Remove across repositories

- **WHEN** a backend Pydantic `user` schema declares `avatar_url`, a frontend TypeScript `user` schema declares `avatarUrl`, a SQL `user` table has no such column, the repositories share one `group_db`, and `schema_impact(entity='user', change='remove', field='avatar_url')` runs from the backend
- **THEN** the Pydantic and TypeScript declarations have action `remove` with the field's file and line, the SQL table has action `absent`, and the backend declarations come first

#### Scenario: Reader through the call graph

- **WHEN** a backend function constructs `User(...)` and an HTTP route handler calls that function
- **THEN** both are readers via `graph`, the function at depth 1, and the handler has role `endpoint` with its HTTP method and path

#### Scenario: Reader across a route

- **WHEN** a frontend function calls the backend route that handler serves
- **THEN** the frontend function is a reader listed under the frontend project

#### Scenario: Test reader

- **WHEN** a test function constructs `User(...)`
- **THEN** it is a reader with role `test`

#### Scenario: Binding and Spec readers

- **WHEN** a function says `@schema: user` and another symbol shares a Spec with the backend `user` schema
- **THEN** they are readers via `schema_binding` and via `spec:<id>`

#### Scenario: Rename already applied on one side

- **WHEN** a rename of `email` to `contact_email` runs, the Pydantic schema declares `email` and the TypeScript schema declares `contactEmail` only
- **THEN** the Pydantic action is `rename` and the TypeScript action is `renamed`

#### Scenario: Retype carried by the wire type

- **WHEN** a retype of `id` to `uuid` runs, the Pydantic schema declares `id: int` and the TypeScript schema declares `id: string`
- **THEN** the Pydantic action is `retype` and the TypeScript action is `compatible`

#### Scenario: Unknown field

- **WHEN** `remove` names a field no declaration of the entity holds
- **THEN** the tool returns an `mcp_error` whose `did_you_mean` lists close field names

#### Scenario: Type outside the neutral types

- **WHEN** `retype` passes `new_type='varchar'`
- **THEN** the tool returns an `mcp_error`

#### Scenario: No call-graph reader

- **WHEN** no symbol reaches the entity's declarations through the call graph
- **THEN** the payload has a hint that type-position uses are not call edges

#### Scenario: Pagination

- **WHEN** there are more readers than `limit`
- **THEN** the page holds `limit` readers, `next_cursor` resumes after them, and the counts are exact

### Requirement: Schema impact of a diff

`git_diff_impact` SHALL, when the range changes a schema declaration bound to an entity, report under `schema_impact` the entity, the fields the range added, removed or retyped in that declaration (base against head), the entity's other declarations in the workspace's projects with the action each field change asks of them, and its readers' counts per project with a bounded sample. A range that changes no bound schema declaration SHALL leave the payload unchanged.

#### Scenario: Field removed in a commit

- **WHEN** a commit removes `avatar_url` from the backend Pydantic `user` schema and the frontend TypeScript `user` schema in the same `group_db` still declares `avatarUrl`
- **THEN** `git_diff_impact` reports `schema_impact` for `user` with a `removed` field change for `avatar_url` and the TypeScript declaration with action `remove`

#### Scenario: Rename in a commit

- **WHEN** a commit renames `email` to `contact_email`
- **THEN** the field changes are `removed` `email` and `added` `contact_email`

#### Scenario: No schema touched

- **WHEN** the range changes no schema declaration
- **THEN** the payload has no `schema_impact` key

#### Scenario: Summary only

- **WHEN** `git_diff_impact` runs with `summary_only=True`
- **THEN** each `schema_impact` entry keeps its counts and drops the reader sample
