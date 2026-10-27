# Storage Specification

## Purpose

The `storage` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Persistence & schema migrations

The livespec MCP server SHALL ensure that the system SHALL bootstrap SQLite from a single-file schema and apply an append-only ordered migration framework with monotonic versions.

#### Scenario: Idempotent migrate

- **WHEN** connect runs twice on the same DB file
- **THEN** migration versions are recorded once and the schema remains consistent

### Requirement: Workspace state follows its configured database

The livespec MCP server SHALL open each workspace against the database that its `.livespec.toml` names now: the `[workspace] group_db` when set, otherwise the workspace's own `.mcp-docs/docs.db`. When that path differs from the one a cached workspace state was opened with, the server SHALL stop the workspace's watcher, close the old connection and open the new database before serving the call, and SHALL keep the old state if the new database cannot be opened.

#### Scenario: group_db added after the workspace was first used

- **WHEN** a workspace was used without a `.livespec.toml`, then a `[workspace] group_db` is written and `index_project` runs again
- **THEN** the index is written to the group database and the workspace reports `grouped: true`

#### Scenario: group_db removed after the workspace was first used

- **WHEN** a workspace was opened on a `group_db`, then the `group_db` setting is removed
- **THEN** the next call opens the workspace's own `.mcp-docs/docs.db` and reports `grouped: false`
