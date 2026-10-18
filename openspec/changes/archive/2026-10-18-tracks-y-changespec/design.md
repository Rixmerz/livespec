## Context

The index is a projection of the working tree. A track is about the past —
two commits — so it cannot be read from the index alone, and its record must
survive a deleted `.mcp-docs/` and travel with the repository.

## Goals / Non-Goals

**Goals:** symbol-level precision, a versioned and human-editable record, the
index rebuildable from it, one call to draft and one to record.

**Non-Goals:** inferring the *why* (the agent drafts it, the person confirms
it); per-commit history (a track is as coarse as the person wants it).

## Decisions

- **Both sides from git, not the index.** `git cat-file --batch` reads the base
  and head blobs; the regular extractors run on the text, and their
  `body_hash_seed` is hashed exactly as the indexer does, so a reformat never
  counts as a change. Every ref goes after `--end-of-options`.
- **Identity is the name local to the module.** Symbols pair by qualified name
  minus the module prefix, so a renamed file (`--name-status -M`) yields a
  `modified` with `old_path`, or nothing when only the path moved.
- **A member's change is not its container's.** A class whose only change is a
  method that is itself reported is not reported again.
- **Spec mapping.** Head-side `@spec:` annotations and the link table for added
  and modified symbols; base-side annotations for removed ones. In an OpenSpec
  repo the Specs that existed at that commit (its `openspec/specs/**` tree)
  bound the mapping, because the link table describes today's code and a past
  range must not credit a Spec born later.
- **The file is the truth, the tables a cache.** `index_project` deletes the
  project's track rows and re-inserts what `CHANGESPEC.md` holds; a missing
  file means no tracks, a block that does not parse is reported and skipped.
  Spec ids and symbol names are TEXT, so history outlives what it mentions.
- **Round-trip format.** Each entry carries
  `<!-- livespec:track id=… base=<sha> head=<sha> -->`; code items are
  `+|~|- name (path[ ← old_path])`, deltas `OP "name"`. Updating an entry
  rewrites only its block and keeps per-Spec whys not restated.

## Risks / Trade-offs

- A function linked to many Specs makes every one of them "touched"; the
  OpenSpec deltas say which Specs the change meant. Kept: the file records
  what the links say, and the person edits the prose.
- Anonymous callbacks (`Deno.test(...)`, Jest `it(...)`) are no symbols, so a
  range that only adds such tests shows no test code.
