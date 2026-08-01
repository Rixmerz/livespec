# Docs Specification

## Purpose

The `docs` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Documentation generation

The livespec MCP server SHALL ensure that the system SHALL generate, list, and export on-demand documentation via the docs plugin tools.

#### Scenario: List docs

- **WHEN** docs rows exist in the project DB
- **THEN** `list_docs` returns those rows with stale flags when body hashes drift
