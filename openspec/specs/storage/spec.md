# Storage Specification

## Purpose

The `storage` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Persistence & schema migrations

The livespec MCP server SHALL ensure that the system SHALL bootstrap SQLite from a single-file schema and apply an append-only ordered migration framework with monotonic versions.

#### Scenario: Idempotent migrate

- **WHEN** connect runs twice on the same DB file
- **THEN** migration versions are recorded once and the schema remains consistent
