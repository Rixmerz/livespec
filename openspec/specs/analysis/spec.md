# Analysis Specification

## Purpose

The `analysis` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Dead-code & coverage analysis

The livespec MCP server SHALL ensure that the system SHALL detect unreachable symbols (`find_dead_code`), audit Spec coverage (`audit_coverage`), and surface orphan tests (`find_orphan_tests`).

#### Scenario: Dead-code sweep

- **WHEN** an indexed project has unused private helpers
- **THEN** `find_dead_code` returns those helpers as candidates without claiming production traffic proof

### Requirement: Endpoint discovery (framework-aware)

The livespec MCP server SHALL ensure that the system SHALL discover HTTP/CLI entry points across supported frameworks via `find_endpoints`, including Express/Hono in the default sweep.

#### Scenario: Default sweep

- **WHEN** a repo defines Express or Hono `router.get` routes
- **THEN** `find_endpoints()` without framework filter includes those routes

### Requirement: Impact analysis

The livespec MCP server SHALL ensure that the system SHALL answer blast-radius questions via `analyze_impact`, `git_diff_impact`, `who_calls`, and `who_does_this_call`.

#### Scenario: Who calls

- **WHEN** symbol A calls symbol B in the index
- **THEN** `who_calls(B)` includes A in the caller set
