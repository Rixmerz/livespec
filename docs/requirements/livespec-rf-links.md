# livespec-mcp — RF↔symbol link seed (reproduction note)

The RF definitions in `livespec-rfs.md` carry no symbol links by
themselves. The `implements` / `tests` links — i.e. which functions
implement each RF and which tests exercise it — were seeded separately
via the `bulk_link_rf_symbols` tool against the live index.

## How to reproduce the links

Pick one of:

1. **Re-run the bulk seed.** Index the project, then call
   `bulk_link_rf_symbols` with a mapping of RF id → qualified symbol
   names (one batch of `implements` links, one of `tests`). The seed
   used to populate the original 12 RFs has not been checked in as a
   data file; re-running it requires re-specifying that mapping.

2. **Add `@rf:` annotations in source.** Annotate the implementing
   functions/classes and their tests with `@rf:RF-NNN` comments or
   docstrings, then run `scan_rf_annotations` /
   `scan_docstrings_for_rf_hints`. This is the durable, self-documenting
   path and the one a fresh clone would ideally use.

## Honest gap

Neither path above is committed today. This file documents the gap:
running `import_requirements_from_markdown` recreates the **12 RF
definitions** but NOT the symbol links. A full annotation pass (option 2)
is out of scope for this change — it is tracked here so the gap is
explicit rather than silent.
