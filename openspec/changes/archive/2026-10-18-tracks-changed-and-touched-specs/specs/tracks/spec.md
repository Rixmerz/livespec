## ADDED Requirements

### Requirement: Changed and touched Specs

The livespec MCP server SHALL mark a Spec of a track `changed` when the range carries an OpenSpec requirement delta for it or when it is the primary explicit annotation of a symbol the range added, modified or removed, and `touched` otherwise. The primary explicit annotation is the first Spec id of the first `@spec:` or `@implements:` line of the symbol's docstring, on the side the symbol lives on.

#### Scenario: Shared function linked to many Specs

- **WHEN** a range carries 7 requirement deltas and modifies a function annotated `@spec:a, b, c` plus a function only linked to other Specs, reaching 37 Specs in all
- **THEN** the 7 delta Specs and `a` are `changed` and the other 29 are `touched`

#### Scenario: Verb-anchored mention only

- **WHEN** a changed function's docstring only says `implements x`
- **THEN** `x` is `touched`, not `changed`

#### Scenario: Link table, tests and references

- **WHEN** a changed function reaches `z` only through the link table, an `@tests:` line or an `@see:` line
- **THEN** `z` is `touched`

#### Scenario: Removed symbol

- **WHEN** the range removes a function whose base-side docstring is `@spec:y`
- **THEN** `y` is `changed`

### Requirement: Touched Specs in CHANGESPEC.md

In `CHANGESPEC.md`, changed Specs SHALL get a `### <spec-id>` section and touched Specs SHALL share one `Also touched: <ids> <!-- livespec:touched -->` line, unless `include_touched_sections` is true or the Spec has a reason, which gives it a section whose heading carries the marker. Only a line carrying the marker SHALL be read as touched Specs, and an entry without markers SHALL read back as it did before tiers existed.

#### Scenario: Shared code of touched Specs

- **WHEN** a changed function reaches only touched Specs and none of them has a section
- **THEN** the entry has no section for them and lists no code for them

#### Scenario: Why with an "Also touched:" line

- **WHEN** an entry's Why has a line starting with `Also touched:` without the marker
- **THEN** it reads back as part of the Why, unchanged, and adds no touched Spec

#### Scenario: Reason of a touched Spec kept

- **WHEN** a touched Spec was recorded with a reason and the range is recorded again without restating it
- **THEN** the Spec keeps its section and its reason

## MODIFIED Requirements

### Requirement: Propose a track

The livespec MCP server SHALL expose `propose_track(base_ref, head_ref)`, which writes nothing and returns the Specs a range reached, split into paginated `changed_specs` and `touched_specs`, each with its deltas and added, modified and removed symbols, plus the code it changed with no Spec. Symbols map to Specs through annotations (base side for removed ones) and the link table, and in an OpenSpec repository only to Specs that existed at that commit or that the range adds.

#### Scenario: Draft writes nothing

- **WHEN** `propose_track` runs on a range
- **THEN** no file in the workspace is created or changed

#### Scenario: Unknown ref

- **WHEN** `base_ref` names no commit
- **THEN** the tool returns an `mcp_error` whose `did_you_mean` lists close branch or tag names

#### Scenario: Spec born after the range

- **WHEN** today's index links a changed function to a Spec that the OpenSpec tree at the range's head did not hold
- **THEN** that Spec is not reported as changed or touched

#### Scenario: Paginated tiers

- **WHEN** `propose_track` runs with `limit` 5 on a range with 29 touched Specs
- **THEN** `touched_specs` holds 5, `counts.specs_touched_only` is 29 and `next_cursor` is 5

### Requirement: Record a track in CHANGESPEC.md

The livespec MCP server SHALL expose `record_track(base_ref, head_ref, title, why, spec_reasons)`, which writes the track into `CHANGESPEC.md` at the workspace root, newest first, with a `livespec:track` marker carrying the full base and head SHAs, creating the file if absent; recording the same range again SHALL update its entry, never add a second one, and keep per-Spec reasons the call does not restate. It is the only write livespec makes into a project and happens only on that explicit call.

#### Scenario: Idempotent per range

- **WHEN** `record_track` runs twice for the same base and head
- **THEN** `CHANGESPEC.md` holds one entry for that range, with the latest title and why, and the same file when the arguments are the same

#### Scenario: Reason for a Spec the range did not touch

- **WHEN** `spec_reasons` names a Spec the range did not touch
- **THEN** the tool returns an `mcp_error` with `did_you_mean` and writes nothing

### Requirement: Spec and symbol history

The livespec MCP server SHALL expose `get_spec_history(spec_id)` and `get_symbol_history(symbol)`, paginated with exact counts, returning the tracks that touched a Spec (legacy ids accepted) or changed a symbol (removed symbols included), newest first, with their reasons; each track of a Spec's history SHALL carry `tier`, `changed` or `touched`.

#### Scenario: Two tracks on one Spec

- **WHEN** two recorded tracks touched Spec Beta
- **THEN** `get_spec_history("core-beta")` returns both, newest first, with `count` 2 and `next_cursor` set when `limit` is 1

#### Scenario: Touched only

- **WHEN** a recorded track lists a Spec on its `Also touched` line
- **THEN** that Spec's history lists the track with `tier` `touched`
