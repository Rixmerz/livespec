# Graph Specification

## Purpose

The `graph` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Call graph & PageRank

The livespec MCP server SHALL ensure that the system SHALL build a NetworkX call graph from resolved edges, cache it by `(db_path, project_id, last_run_id)`, and expose PageRank centrality.

#### Scenario: Cache hit

- **WHEN** the same index run loads the graph twice
- **THEN** the second load reuses the cached graph view

### Requirement: External graph edges are labelled, reversible and dated

The livespec MCP server SHALL record the provenance of every edge ingested from
another extractor, SHALL keep those edges removable by that provenance alone,
and SHALL report when they no longer describe the indexed code.

#### Scenario: An ingested edge is distinguishable from an extracted one

- **WHEN** a tool returns a symbol reached through an ingested edge
- **THEN** the payload names the origin of that edge, and the tool's response
  carries an `external_edges` block naming every origin in play

#### Scenario: A re-extract invalidates ingested edges

- **WHEN** an index run re-extracts files whose symbols an ingested edge
  pointed at
- **THEN** the next graph-reading tool reports `external_edges.stale` with the
  count written, the count surviving, and what to do about it

#### Scenario: Removal restores the livespec-only graph exactly

- **WHEN** `ingest_external_graph(remove=True)` runs
- **THEN** every row carrying that origin is deleted and no livespec-derived
  edge is touched

### Requirement: A caller is a symbol that invokes, not one that mentions

The livespec MCP server SHALL count only invocation edges as callers by
default, and SHALL report dependencies excluded by that rule rather than
dropping them silently.

#### Scenario: A type-position dependency is reported, not counted

- **WHEN** `who_calls` runs on a symbol reached only by a `references` edge
- **THEN** the caller count excludes it and `excluded_by_edge_type` names it,
  together with the argument that would include it

#### Scenario: Blast radius still counts every dependency

- **WHEN** `analyze_impact` runs on that same symbol
- **THEN** the dependent symbol appears, because changing a type does break the
  code that names it
