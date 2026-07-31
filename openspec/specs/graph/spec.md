# Graph Specification

## Purpose

The `graph` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Call graph & PageRank

<!-- livespec:id=SPEC-004 -->

The livespec MCP server SHALL ensure that the system SHALL build a NetworkX call graph from resolved edges, cache it by `(db_path, project_id, last_run_id)`, and expose PageRank centrality.

#### Scenario: Cache hit

- **WHEN** the same index run loads the graph twice
- **THEN** the second load reuses the cached graph view
