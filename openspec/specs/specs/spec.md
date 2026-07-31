# Specs Specification

## Purpose

The `specs` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Spec↔code traceability

<!-- livespec:id=SPEC-005 -->

The livespec MCP server SHALL ensure that the system SHALL parse `@spec:` annotations, support Spec CRUD and Spec↔symbol links, and maintain a Spec→Spec dependency graph.

#### Scenario: Annotation scan

- **WHEN** code contains `@spec:SPEC-001` and Specs exist
- **THEN** `scan_spec_annotations` creates or refreshes Spec↔symbol links

### Requirement: OpenSpec (Fission-AI) interoperability

<!-- livespec:id=SPEC-013 -->

The livespec MCP server SHALL ensure that the system SHALL import/export OpenSpec trees (`sync_openspec` / `export_openspec`), model scenarios and change deltas, validate with `validate_openspec`, and support apply/archive change lifecycle.

#### Scenario: Strict validate on dogfood tree

- **WHEN** the repo has an `openspec/` directory with requirements and scenarios
- **THEN** `validate_openspec(strict=True)` reports valid with zero requirement/scenario errors
