# Indexing Specification

## Purpose

The `indexing` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Indexing & workspace walk

The livespec MCP server SHALL ensure that the system SHALL walk the workspace (honouring `.gitignore` and `.livespec.toml`), detect languages per file, and persist extracted symbol references into the SQLite store.

#### Scenario: Fresh index

- **WHEN** an agent runs `index_project` on a repository root
- **THEN** files are hashed incrementally and symbols are available to downstream tools
