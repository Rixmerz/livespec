# Storage Specification

## Purpose

The `storage` capability of livespec (dogfood).

## Requirements

### Requirement: Persistence & schema migrations

<!-- livespec:id=SPEC-011 -->

Bootstrap the SQLite store from a single-file schema and apply an
append-only, ordered migration framework (monotonic version numbers,
never reused or reordered) so user databases upgrade safely across
releases.
