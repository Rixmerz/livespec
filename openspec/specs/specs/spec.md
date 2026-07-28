# Specs Specification

## Purpose

The `specs` capability of livespec (dogfood).

## Requirements

### Requirement: Spec↔code traceability

<!-- livespec:id=SPEC-005 -->

Parse `@spec:` annotations from code and docstrings, support full Spec
CRUD, link Specs to symbols, and maintain a Spec→Spec dependency graph.
This is the differentiator: Functional Requirement (and other spec kinds)
↔ code traceability for serious software orgs.

### Requirement: OpenSpec (Fission-AI) interoperability

<!-- livespec:id=SPEC-013 -->

Be a first-class citizen of an OpenSpec `openspec/` repo: import a whole
tree (canonical `specs/` requirements plus `changes/` and `archive/`
proposals) via `sync_openspec`, model scenarios (`#### Scenario:`) and
change deltas (ADDED/MODIFIED/REMOVED/RENAMED) as first-class rows,
validate structural rules (`validate_openspec`, mirroring
`openspec validate --strict`), apply the change lifecycle
(`apply_spec_change`/`archive_spec_change`, RENAMED migrating traceability),
trace code to individual scenarios (`link_scenario_symbol`), and write the
tree back out (`export_openspec`) — closing the round-trip.
