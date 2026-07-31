# CLI Specification

## Purpose

The `cli` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Headless CLI

<!-- livespec:id=SPEC-012 -->

The livespec MCP server SHALL ensure that the system SHALL provide headless `livespec index` / `livespec status` (and `livespec-mcp` aliases) sharing the MCP indexing pipeline.

#### Scenario: Index then status

- **WHEN** an agent runs `livespec index <path>` then `livespec status <path>`
- **THEN** status JSON reports symbol/file counts for that workspace
