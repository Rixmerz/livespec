## 1. Domain

- [x] 1.1 Annotation hits say whether they are explicit (level 1)
- [x] 1.2 Primary explicit annotation per changed symbol; changed vs touched tier per Spec of a draft
- [x] 1.3 `CHANGESPEC.md`: `Also touched` line with marker, touched section marker, old entries unchanged
- [x] 1.4 Re-recording keeps reasons, including those of touched Specs

## 2. Storage

- [x] 2.1 Migration 26: `track_spec.tier` (+ `schema.sql`), rollback tested

## 3. Tools

- [x] 3.1 `propose_track` / `record_track` return `changed_specs` and `touched_specs`; `include_touched_sections`
- [x] 3.2 `get_spec_history` and the UI Spec page show the tier
- [x] 3.3 Parameter descriptions, Skill and agent playbook

## 4. Verification

- [x] 4.1 Fixture with 7 deltas and 37 Specs on shared code ends with 8 changed
- [x] 4.2 Old-format entry, Why with an `Also touched:` line, idempotent re-record, pagination
