## ADDED Requirements

### Requirement: Schema field extraction

The livespec MCP server SHALL record, for every schema declaration it indexes, its flavor and its fields in source order, each with its name as written, a convention-free key, a neutral type, whether it is optional, whether it is nullable, and its line. Schema declarations are Pydantic models and dataclasses, TypeScript interfaces and type aliases of object types, Zod `z.object(...)` declarations, and `CREATE TABLE` statements in `.sql` files, which SHALL be read without a tree-sitter grammar.

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
