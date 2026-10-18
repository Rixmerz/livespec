## Why

Measured on a real repository: one tracked range with 7 OpenSpec requirement
deltas listed 37 Specs in `CHANGESPEC.md`, because a changed function linked to
many Specs marks every one of them as touched. The record is correct but noisy:
the Specs whose requirements actually changed drown among the ones only reached
through shared code.

## What Changes

- Each track splits its Specs into two tiers. **changed**: the range carries an
  OpenSpec delta for the Spec, or the Spec is the *primary explicit annotation*
  of a symbol the range added, modified or removed. **touched**: every other
  Spec, reached only through shared code (a secondary id, a verb-anchored
  mention, a link-table row).
- `CHANGESPEC.md`: changed Specs keep their `### <spec-id>` sections; touched
  Specs go on one line per entry, `Also touched: a, b <!-- livespec:touched -->`.
  Touched Specs have no section and no code of their own. The marker
  makes the line unambiguous against the free-text Why; entries written before
  this change read back exactly as before (every section changed).
- `propose_track` returns `changed_specs` and `touched_specs` (paginated, exact
  counts) instead of `specs`; `record_track` returns both id lists and takes
  `include_touched_sections` to write sections for touched Specs too. A touched
  Spec with a reason (given, or kept from an earlier recording) keeps a section.
- `get_spec_history` marks each track `tier: changed | touched`; the
  `livespec ui` Spec page shows the tier.
- Migration 26: `track_spec.tier` (TEXT, default `changed`).

## Capabilities

### New Capabilities

### Modified Capabilities

- `tracks`: changed vs touched Specs; propose, record and history carry the
  tier.
- `ui`: a Spec's history shows whether each track changed or only touched it.

## Impact

`domain/matcher.py` (hits say whether they are explicit), `domain/tracks.py`,
`domain/changespec.py`, `tools/tracks.py`, `storage/db.py` + `schema.sql`
(migration 26), `ui/data.py`, `ui/static/app.js`, `ui/static/app.css`, the
Skill and the agent playbook.
