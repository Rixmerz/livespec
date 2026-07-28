# Indexing Specification

## Purpose

The `indexing` capability of livespec (dogfood).

## Requirements

### Requirement: Indexing & workspace walk

<!-- livespec:id=SPEC-001 -->

Walk the workspace (honouring `.gitignore` and `.livespec.toml`
configuration), detect the languages present per file, and persist the
extracted symbol references into the SQLite store. This is the entry
point for every other capability — nothing downstream works without a
fresh index.
