# Search (FTS) Specification

## Purpose

The `rag` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: FTS search (AST chunks)

<!-- livespec:id=SPEC-006 -->

The livespec MCP server SHALL ensure that the system SHALL provide AST-aware chunking and full-text search via SQLite FTS5 over symbols and Specs (no dense-vector lane).

#### Scenario: Keyword search

- **WHEN** chunks exist after `index_project`
- **THEN** `search` returns matching symbol or Spec chunks via FTS5
