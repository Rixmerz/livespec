# Tracks Specification

## Purpose

The `tracks` capability of livespec (dogfood OpenSpec SSoT): the evolutionary
history of code ↔ spec. A track is a named git range plus why it exists,
recorded per Spec in `CHANGESPEC.md` at the workspace root.

## Requirements

### Requirement: Symbol-level range diff

The livespec MCP server SHALL compute, for a git range `base..head`, the symbols the range added (in head, not in base), modified (normalised `body_hash` differs) and removed (in base, not in head), reading both sides from git and running the regular extractors on them, and SHALL pair the symbols of a renamed file by their module-local name.

#### Scenario: Reformat-only edits do not count

- **WHEN** a function's only change in the range is whitespace or a comment
- **THEN** it is not reported as modified

#### Scenario: Renamed file

- **WHEN** a file is renamed and one of its functions changes body
- **THEN** that function is reported once as modified with `old_path`, and its unchanged siblings are not reported

#### Scenario: Member, not container

- **WHEN** only a method of a class changes
- **THEN** the method is reported as modified and the class is not

### Requirement: OpenSpec deltas in a track

The livespec MCP server SHALL attach to a track the requirement-level `ADDED`, `MODIFIED`, `REMOVED` and `RENAMED` deltas of the OpenSpec change files (`openspec/changes/**/specs/**/spec.md`, archive included) the range adds, moves or edits, each on the Spec id its requirement maps to.

#### Scenario: Change with added and modified requirements

- **WHEN** the range adds a change whose delta ADDs requirement Gamma and MODIFIEs requirement Beta
- **THEN** the track lists `ADDED "Gamma"` under Gamma's Spec id and `MODIFIED "Beta"` under Beta's

### Requirement: Propose a track

The livespec MCP server SHALL expose `propose_track(base_ref, head_ref)`, which returns the Specs a range touched with their deltas and their added, modified and removed symbols, plus the code it changed with no Spec, and writes nothing; added and modified symbols map to Specs through head-side annotations and the link table, removed symbols through base-side annotations, and in an OpenSpec repository only to Specs that existed at that commit or that the range adds.

#### Scenario: Draft writes nothing

- **WHEN** `propose_track` runs on a range
- **THEN** no file in the workspace is created or changed

#### Scenario: Unknown ref

- **WHEN** `base_ref` names no commit
- **THEN** the tool returns an `mcp_error` whose `did_you_mean` lists close branch or tag names

#### Scenario: Spec born after the range

- **WHEN** today's index links a changed function to a Spec that the OpenSpec tree at the range's head did not hold
- **THEN** that Spec is not reported as touched

### Requirement: Record a track in CHANGESPEC.md

The livespec MCP server SHALL expose `record_track(base_ref, head_ref, title, why, spec_reasons)`, which writes the track into `CHANGESPEC.md` at the workspace root, newest first, with a `livespec:track` marker carrying the full base and head SHAs, creating the file if absent; recording the same range again SHALL update its entry, never add a second one, and keep per-Spec reasons the call does not restate. It is the only write livespec makes into a project and happens only on that explicit call.

#### Scenario: Idempotent per range

- **WHEN** `record_track` runs twice for the same base and head
- **THEN** `CHANGESPEC.md` holds one entry for that range, with the latest title and why

#### Scenario: Reason for a Spec the range did not touch

- **WHEN** `spec_reasons` names a Spec the range did not touch
- **THEN** the tool returns an `mcp_error` with `did_you_mean` and writes nothing

### Requirement: Index rebuilt from CHANGESPEC.md

The livespec MCP server SHALL rebuild the `track`, `track_spec` and `track_symbol` rows from `CHANGESPEC.md` on every `index_project`, treat a missing file as no tracks, and report a block that does not parse without failing the index run.

#### Scenario: Round trip

- **WHEN** the track rows are deleted and `index_project` runs
- **THEN** `get_spec_history` returns the same timeline as before

#### Scenario: Malformed file

- **WHEN** `CHANGESPEC.md` has a block with no `livespec:track` marker
- **THEN** `index_project` succeeds and lists the block under `tracks.problems`

### Requirement: Spec and symbol history

The livespec MCP server SHALL expose `get_spec_history(spec_id)` and `get_symbol_history(symbol)`, paginated with exact counts, returning the tracks that touched a Spec (legacy ids accepted) or changed a symbol (removed symbols included), newest first, with their reasons.

#### Scenario: Two tracks on one Spec

- **WHEN** two recorded tracks touched Spec Beta
- **THEN** `get_spec_history("core-beta")` returns both, newest first, with `count` 2 and `next_cursor` set when `limit` is 1
