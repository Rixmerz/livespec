# Rag Specification

## Purpose

The `rag` capability of livespec (dogfood).

## Requirements

### Requirement: Hybrid search & RAG

<!-- livespec:id=SPEC-006 -->

Provide AST-aware chunking of source, full-text search via SQLite FTS5,
and optional dense-vector search via sqlite-vec, fused with Reciprocal
Rank Fusion (RRF) for hybrid retrieval over symbols and specs.
