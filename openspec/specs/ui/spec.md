# UI Specification

## Purpose

The `ui` capability of livespec (dogfood OpenSpec SSoT): one local, read-only
web view over every indexed project, kept outside every repository.

## Requirements

### Requirement: Cross-project read-only UI

The livespec MCP server SHALL record every indexed workspace in a user-level registry outside any repository, and `livespec ui` SHALL serve one local web UI over all registered projects, grouped by polyrepo group, reading each project's index read-only and writing nothing into any project directory; a Spec's page SHALL show the tracks that touched it.

#### Scenario: Index then browse

- **WHEN** two repos sharing a `group_db` and one standalone repo are indexed and `livespec ui` is started
- **THEN** the home lists the two repos under one group and the third as ungrouped, with languages, symbols, Specs and coverage per project

#### Scenario: Serving is read-only

- **WHEN** every page and JSON endpoint of `livespec ui` is requested
- **THEN** no file is created or modified in any indexed repository or group database directory

#### Scenario: Missing index is shown, not pruned

- **WHEN** a registered project's database no longer exists
- **THEN** the registry still lists it as missing until the user forgets it

#### Scenario: Spec history

- **WHEN** a recorded track touched a Spec
- **THEN** that Spec's page lists the track with its why, deltas and changed symbols, and `CHANGESPEC.md` is unchanged
