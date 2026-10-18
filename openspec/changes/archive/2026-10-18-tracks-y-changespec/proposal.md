## Why

livespec answers "what implements this Spec *now*" but not "how did this Spec
get here". Commits are too small to read as a history of requirements, and a
PR description is lost once it merges. Teams that run spec-driven development
(OpenSpec changes, regulated traceability) need the evolution itself: which
range of commits changed which Spec, which code it touched at symbol level,
and why — versioned next to the code, not locked in a database.

`git_diff_impact` had the same blind spot at a smaller scale: it marked every
symbol of a changed file as changed, so a one-line fix in a 40-function module
reported 40 changed symbols and pulled in every caller and Spec of the file.

## What Changes

- A **track** is a macro-commit: a named `base..head` git range plus the reason
  it exists. livespec computes, per Spec the range touched, the symbols it
  added, modified (normalised `body_hash` differs) and removed, plus the
  OpenSpec requirement deltas of change files the range adds or edits.
- `CHANGESPEC.md` at the workspace root holds the tracks, newest first, per
  Spec. It is the source of truth; new tables `track`, `track_spec` and
  `track_symbol` (migration 25) are derived from it and rebuilt on every
  `index_project`. It is written only by an explicit `record_track` call — the
  one sanctioned write into a project (a human-owned document like a
  CHANGELOG, not a UI artifact).
- Four core tools: `propose_track` (draft, writes nothing), `record_track`
  (writes the file, idempotent per range), `get_spec_history`,
  `get_symbol_history`.
- `git_diff_impact` reports only the symbols the range really changed in
  `changed_symbols`; payload shape and pagination are unchanged.
- `livespec ui` shows a Spec's tracks on its detail page (read-only).

## Capabilities

### New Capabilities

- `tracks`: symbol-level range diff, OpenSpec deltas in a track, propose,
  record into `CHANGESPEC.md`, index rebuilt from the file, Spec and symbol
  history.

### Modified Capabilities

- `analysis`: `git_diff_impact` is symbol-precise.
- `ui`: the Spec detail page shows the Spec's history.

## Impact

`domain/git_range.py` (new, the git core `git_diff_impact` already used, moved
out of `tools/`), `domain/tracks.py`, `domain/changespec.py`,
`tools/tracks.py`, `tools/analysis.py`, `tools/indexing.py`,
`storage/db.py` + `schema.sql` (migration 25), `ui/data.py`,
`ui/static/app.js`, `param_descriptions.py`, the Skill and the agent playbook.
