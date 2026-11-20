# Experimental: Kerno-backed index history

Branch `ccr-5d0e38e9-1hmqh0` (experimental). Not for `main` until Kerno leaves alpha.

## What it does

SQLite stays the source of truth for every livespec query. With `LIVESPEC_KERNO=1`,
each `index_project` run (MCP tool or `livespec index`) also mirrors the index into a
[Kerno](https://github.com/Rixmerz/kernodb) database at `.mcp-docs/kerno/`, beside
`docs.db`. It uses the Kerno features SQLite does not give livespec:

| Kerno feature | Use in livespec |
|---|---|
| Revisions (`update` keeps the previous ones) | one unit per symbol; a revision only when body, signature or kind changes (reformat does not) |
| Tombstone deletes | a symbol removed from the code keeps its history |
| Atomic change sets (`commit`) | every sync of symbols is one change set |
| As-of reads (`read_as_of`) | a symbol as it was at an earlier index run |
| Relations | call edges (`calls`), diffed each run |
| `verify` | end-to-end integrity check of the history store |

## Tools (registered only with `LIVESPEC_KERNO=1`)

- `kerno_symbol_revisions(qname, limit=20)`: every semantic revision of a symbol, newest
  first, with the index run that saw it. Needs no git, unlike `get_symbol_history`.
- `kerno_symbol_as_of(qname, index_run)`: the symbol at an earlier index run, and whether
  it is the same as now (`absent` / `present` / `deleted`).
- `kerno_verify_history()`: Kerno's units-layer verification.

`index_project` adds a `kerno_history` section (added / changed / removed symbols and
edges). A failure there is reported, never fails the index run.

## Install

The bindings this needs (`update`, `commit`, `history`, `read_as_of`, `verify`) are on the
matching kernodb branch, not yet in the `1.0.0a1` wheel on PyPI:

```bash
uv sync --extra dev
uv pip install /path/to/kernodb/bindings/python    # builds with maturin (Rust toolchain)
LIVESPEC_KERNO=1 uv run livespec index /path/to/repo
```

## Measured (kernodb repository, 16 real commits, ~9.7k symbols, ~272k call edges)

| | without Kerno | first version | kernodb alpha.2 branch |
|---|---|---|---|
| 16 index runs, total | 27.4 s | 135.8 s | 56–59 s |
| first index | 4.8 s | 18.6 s | 6.4–6.9 s |
| run with no file changes | 0.06 s | ~4 s | 0.07 s (skipped) |
| symbols / edges mirrored | | 8,868 / 266,571 (lossy) | 9,663 / 271,655 (exact, verify clean) |
| Kerno store on disk | | 95 MB | 35 MB (LZ4 blocks, no keyword index) |

With the kernodb alpha.2 branch, a sync is one change set when it fits in 10,000 operations
(`atomic: true`), else a few, with the sync log written last. Kerno's keyword index is off by
default there, so the store keeps plain JSON. With kernodb 1.0.0a1 (no `set_fulltext`, no batch
relations) the mirror falls back to zlib-compressed payloads and one `relate` call per edge.

The `kerno` extra still says `kernodb>=1.0.0a1`; once 1.0.0a2 is on PyPI, raise it to
`kernodb>=1.0.0a2` and run `uv lock`.

## Limits (known)

- Kerno allows one open handle per directory, read-only included: two livespec processes on
  the same workspace cannot both use the store (the second one reports it and skips).
- Renaming a file, or deleting and recreating a symbol, starts a new history (the key holds
  the file path); the old one stays readable but is not linked.
- A sync larger than 10,000 operations (a first index of a big repository) spans several
  change sets; an interruption between them is completed by the next run.
- The watcher's background reindex does not sync to Kerno.
