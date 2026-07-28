# Indexer Specification

## Purpose

The `indexer` capability of livespec (dogfood).

## Requirements

### Requirement: Scoped reference resolution

<!-- livespec:id=SPEC-003 -->

Resolve call and usage references into `symbol_edge` rows with
import-scoped precision (qualifying names by the imports visible in each
file). Edges are written with `INSERT OR IGNORE` so refs from unchanged
files survive when the files they target change.
