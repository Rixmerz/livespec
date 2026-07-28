# Graph Specification

## Purpose

The `graph` capability of livespec (dogfood).

## Requirements

### Requirement: Call graph & PageRank

<!-- livespec:id=SPEC-004 -->

Build a NetworkX call graph from the resolved edges, cache it by
`(db_path, project_id, last_run_id)`, and expose PageRank centrality so
agents can rank symbols by structural importance.
