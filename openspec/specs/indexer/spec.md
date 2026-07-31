# Indexer Specification

## Purpose

The `indexer` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Scoped reference resolution

<!-- livespec:id=SPEC-003 -->

The livespec MCP server SHALL ensure that the system SHALL resolve call/usage references into `symbol_edge` rows with import-scoped precision and write edges with `INSERT OR IGNORE`.

#### Scenario: Partial reindex keeps edges

- **WHEN** a callee file changes while callers are unchanged
- **THEN** edges from unchanged callers to the updated callee remain after re-index
