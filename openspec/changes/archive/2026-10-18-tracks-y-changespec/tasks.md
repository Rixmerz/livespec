## 1. Domain

- [x] 1.1 Move the `git diff --name-status -M` core into `domain/git_range.py` (refs after `--end-of-options`, failures as data)
- [x] 1.2 Symbol-level range diff in `domain/tracks.py` (added / modified / removed, renames, reformat-only ignored)
- [x] 1.3 OpenSpec deltas from `openspec/changes/**/specs/**/spec.md`
- [x] 1.4 Spec mapping (annotations, link table, Specs existing at the commit)
- [x] 1.5 `CHANGESPEC.md` render / parse / upsert and ingest in `domain/changespec.py`

## 2. Storage

- [x] 2.1 Migration 25: `track`, `track_spec`, `track_symbol` (+ `schema.sql`)
- [x] 2.2 `index_project` re-ingests `CHANGESPEC.md`, tolerating a missing or malformed file

## 3. Tools

- [x] 3.1 `propose_track`, `record_track`, `get_spec_history`, `get_symbol_history`
- [x] 3.2 `git_diff_impact` symbol-precise `changed_symbols`
- [x] 3.3 Parameter descriptions, Skill and agent playbook
- [x] 3.4 Spec history on the `livespec ui` Spec page

## 4. Verification

- [x] 4.1 Integration tests over a two-commit git fixture (`tests/test_tracks.py`), migration test
- [x] 4.2 Full suite green; run on a real Deno Fresh repo clone
