# Experiment: SQLite vs Kerno (kernodb) as livespec's store

**Status: experimental branch, not for `main`.** SQLite stays the store and
the source of truth. Nothing here is on unless `LIVESPEC_EXPERIMENTAL_KERNO=1`
is set and the optional `kernodb` package is installed.

[Kerno](https://github.com/Rixmerz/kernodb) is an embedded LSM database of
*units* (typed records), their *revisions* and the *relations* between them.
Tested: `kernodb 1.0.0a4` (Python bindings) against the SQLite 3.53 that
livespec uses, on one Linux container. Reproduce with:

```bash
uv pip install --pre kernodb
uv run python bench/versus.py --workdir /tmp/versus --repos requests fastapi django
LIVESPEC_EXPERIMENTAL_KERNO=1 uv run pytest -q     # the suite with the graph served by Kerno
```

Raw numbers of the run below: `bench/versus-results.json`.

## Verdict

**Do not replace SQLite. Kerno is worth a second look for search only.**

- A full replacement is not a port, it is a rewrite. livespec has ~700 SQL
  call sites in 36 modules, and the indexer uses SQL as its *compute* engine
  (ref resolution is `INSERT ... SELECT` over joins, with `GROUP BY`,
  `INSERT OR IGNORE`, cascades, triggers). Kerno's query language reads units
  through typed relations; it has no aggregation, no write queries and no
  arbitrary joins. Everything the resolver does would move into Python.
- On the part that *does* map (symbols as units, edges as relations, chunks as
  searchable units), answers are identical and Kerno loses every round except
  search: 4-14x slower to ingest, 1.5-3.3x larger on disk, 4-18x slower on
  point lookups, on par to 1.6x slower loading the whole graph.
- Kerno wins search clearly: keyword (BM25) top-10 is 4-11x faster than the
  FTS5 path with the same recall, and trigram substring search is 2-8x faster
  than `LIKE '%x%'`.
- The experiment found one silent data-loss bug in kernodb's keyword index and
  one freshness bug in the first version of the mirror (both below).

## How the comparison was made

`storage/kerno_store.py` mirrors one project of the SQLite index into a Kerno
directory: symbol -> unit (document, `sid` unique), edge -> relation, chunk ->
unit with the keyword and substring indexes on. `bench/versus.py` indexes a
real repo with the normal pipeline, mirrors it, then asks both stores the same
questions. **Every timing row is preceded by a parity check**: a round only
counts if both sides return the same answer.

Fairness notes:

- *Ingest* writes the same rows into an empty database on both sides. The
  SQLite side is the subset of livespec's schema the mirror covers, with the
  same indexes and FTS5, in one transaction. Kerno syncs every commit to disk,
  so SQLite `synchronous=FULL` is the like-for-like row (livespec ships
  `NORMAL`).
- *Lookups* return the same columns on both sides. Latencies are the median
  per call over 100-200 real inputs, best of 3 passes.
- *Keyword search*: the SQLite side is livespec's real `fts_search` (it fetches
  3x the limit and a snippet); the rankers differ, so parity is recall (same
  number of matching chunks) plus top-10 overlap.
- Both stores sat in the OS page cache. No cold-disk numbers.

## Results

### requests: 36 files, 753 symbols, 1481 edges, 778 chunks

| Round | SQLite | Kerno | Verdict |
|---|---|---|---|
| Ingest the same rows (ms) | 25.6 (NORMAL) / 27.3 (FULL) | 192.4 | Kerno 7.0x slower |
| Disk, same rows (MB) | 1.45 | 4.82 (4.82 compacted) | 3.33x |
| Open + first query (ms) | 0.159 | 0.649 | Kerno 4.1x slower |
| graph_load (ms) | 5.4 | 8.72 | Kerno 1.6x slower |
| find_symbol_by_name (ms) | 0.0034 | 0.0129 | Kerno 3.8x slower |
| find_by_qualified_name (ms) | 0.0035 | 0.013 | Kerno 3.7x slower |
| symbols_in_file (ms) | 0.0196 | 0.1324 | Kerno 6.8x slower |
| callers_1_hop (ms) | 0.0022 | 0.0121 | Kerno 5.5x slower |
| keyword_search_top10 (ms) | 0.2502 | 0.0685 | Kerno 3.7x faster |
| substring_search (ms) | 0.8124 | 0.3153 | Kerno 2.6x faster |
| Re-sync after a 1-file edit (ms) | 56.9 (whole re-index) | +102.8 (mirror only) | - |
| No-op re-sync (ms) | - | 31.8 | - |

Parity: `noop_resync_wrote_nothing`=True, `graph_nodes_equal`=True, `graph_edges_equal`=True, `find_symbol_by_name`=True, `find_by_qualified_name`=True, `symbols_in_file`=True, `callers_1_hop`=True, `keyword_matches_sqlite`=715, `keyword_matches_kerno`=715, `keyword_top10_jaccard_mean`=0.89, `substring_missing_in_kerno`=0, `substring_extra_in_kerno`=0, `graph_equal_after_incremental`=True

### fastapi: 1145 files, 4495 symbols, 145591 edges, 4615 chunks

| Round | SQLite | Kerno | Verdict |
|---|---|---|---|
| Ingest the same rows (ms) | 567.7 (NORMAL) / 674.5 (FULL) | 2665.1 | Kerno 4.0x slower |
| Disk, same rows (MB) | 27.09 | 41.88 (40.43 compacted) | 1.49x |
| Open + first query (ms) | 0.223 | 2.98 | Kerno 13.4x slower |
| graph_load (ms) | 292.98 | 349.78 | Kerno 1.2x slower |
| find_symbol_by_name (ms) | 0.005 | 0.0283 | Kerno 5.7x slower |
| find_by_qualified_name (ms) | 0.0035 | 0.0152 | Kerno 4.3x slower |
| symbols_in_file (ms) | 0.0059 | 0.0359 | Kerno 6.1x slower |
| callers_1_hop (ms) | 0.003 | 0.0551 | Kerno 18.4x slower |
| keyword_search_top10 (ms) | 0.5841 | 0.1086 | Kerno 5.4x faster |
| substring_search (ms) | 4.7962 | 0.5611 | Kerno 8.5x faster |
| Re-sync after a 1-file edit (ms) | 811.2 (whole re-index) | +1180.6 (mirror only) | - |
| No-op re-sync (ms) | - | 957.7 | - |

Parity: `noop_resync_wrote_nothing`=True, `graph_nodes_equal`=True, `graph_edges_equal`=True, `find_symbol_by_name`=True, `find_by_qualified_name`=True, `symbols_in_file`=True, `callers_1_hop`=True, `keyword_matches_sqlite`=2762, `keyword_matches_kerno`=2762, `keyword_top10_jaccard_mean`=0.816, `substring_missing_in_kerno`=0, `substring_extra_in_kerno`=0, `graph_equal_after_incremental`=True

### django: 2898 files, 40263 symbols, 413714 edges, 41881 chunks

| Round | SQLite | Kerno | Verdict |
|---|---|---|---|
| Ingest the same rows (ms) | 2474.3 (NORMAL) / 2380.1 (FULL) | 32790.6 | Kerno 13.8x slower |
| Disk, same rows (MB) | 132.02 | 388.95 (356.70 compacted) | 2.70x |
| Open + first query (ms) | 2.244 | 28.843 | Kerno 12.9x slower |
| graph_load (ms) | 965.28 | 1575.73 | Kerno 1.6x slower |
| find_symbol_by_name (ms) | 0.0056 | 0.0228 | Kerno 4.1x slower |
| find_by_qualified_name (ms) | 0.0041 | 0.017 | Kerno 4.1x slower |
| symbols_in_file (ms) | 0.0527 | 0.42 | Kerno 8.0x slower |
| callers_1_hop (ms) | 0.0079 | 0.0591 | Kerno 7.5x slower |
| keyword_search_top10 (ms) | 2.3817 | 0.2105 | Kerno 11.3x faster |
| substring_search (ms) | 61.0365 | 27.5107 | Kerno 2.2x faster |
| Re-sync after a 1-file edit (ms) | 1299.4 (whole re-index) | +4362.8 (mirror only) | - |
| No-op re-sync (ms) | - | 5485.1 | - |

Parity: `noop_resync_wrote_nothing`=True, `graph_nodes_equal`=True, `graph_edges_equal`=True, `find_symbol_by_name`=True, `find_by_qualified_name`=True, `symbols_in_file`=True, `callers_1_hop`=True, `keyword_matches_sqlite`=15811, `keyword_matches_kerno`=15811, `keyword_top10_jaccard_mean`=0.874, `substring_missing_in_kerno`=0, `substring_extra_in_kerno`=0, `graph_equal_after_incremental`=True

The 1-file-edit row is not a head-to-head: the SQLite number is the whole
re-index (parse + resolve + chunks), the Kerno number is the extra cost of
bringing the mirror up to date afterwards.

### Reading the rounds

| Round | Winner | Why |
|---|---|---|
| Ingest | SQLite, 4-14x | One transaction vs many bounded change sets; the keyword and trigram entries are written inline. The gap grows with size (Django: 2.4 s vs 32.8 s). |
| Disk | SQLite, 1.5-3.3x | Kerno's README reports ~2x *smaller* than SQLite on its own workload; with both text indexes on, a code index goes the other way. |
| Open + first query | SQLite, 4-13x | Kerno's open cost grows with the store (29 ms on Django). livespec opens a connection per workspace, so this is paid rarely. |
| Graph load | SQLite, 1.2-1.6x | The closest structural round. Kerno reads relations in one scan but needs one `read_document` call per symbol: there is no bulk document read in the bindings. |
| Point lookups, callers | SQLite, 4-18x | Both are microseconds (Kerno 13-60 us). Not a bottleneck for an agent, but not a reason to switch. |
| Keyword search | **Kerno, 4-11x** | Same recall (715/715, 2762/2762, 15811/15811 matching chunks), top-10 overlap 0.82-0.89. |
| Substring search | **Kerno, 2-8x** | Trigram index vs a full `LIKE` scan; identical result sets. |
| History | Kerno only | Every write is a revision and `read_as_of(seq)` reads the past. livespec cannot use it yet (see finding 4). |

### The suite with the graph served by Kerno

`load_graph` is the hot path behind `who_calls`, `analyze_impact`,
`find_dead_code` and the rest of the graph tools. With the flag on,
`index_project` shadows each finished run into Kerno and `load_graph` builds
the NetworkX graph from the mirror, falling back to SQL when the mirror cannot
vouch for the current state.

| | SQLite | Kerno shadow |
|---|---|---|
| Tests passed | 1329 / 1329 | 1329 / 1329 |
| Wall time | 43 s | 51 s |
| Graph loads | all SQL | 205 from Kerno, 43 fell back to SQL, 804 mirror syncs, 0 errors |

## Findings

1. **kernodb bug: the keyword index silently skips document payloads.**
   A payload that is not valid UTF-8 is not indexed, and a typed document's
   binary encoding stops being valid UTF-8 as soon as an int field is >= 128
   or a text field is >= 128 bytes long. No error, `verify()` reports clean,
   `search_text` just returns fewer hits. As documents, 5% of the chunks were
   searchable. Minimal reproduction:

   ```python
   db.define_schema("doc", [{"name": "n", "type": "int"}, {"name": "text", "type": "text"}])
   db.set_fulltext("doc", True)
   a = db.store_document("doc", {"n": 127, "text": "zebra"})   # found by search_text("zebra")
   b = db.store_document("doc", {"n": 128, "text": "zebra"})   # NOT found
   c = db.store_document("doc", {"n": 1, "text": "zebra " + "x" * 122})  # len 128: NOT found
   ```

   Workaround used here: chunks are raw `str` payloads with a tab-separated
   header line, which costs the schema and therefore `upsert` by natural key.
   The trigram (substring) index is not affected.

2. **A relation carries no payload.** `symbol_edge` has `weight` and `origin`.
   The mirror encodes them in the relation type name
   (`e:calls~0.8~external:tag`, 64-byte limit), which works but turns every
   "all edges of type X" read into a scan plus a parse.

3. **A change set is bounded in engine ops and bytes, not in caller ops.**
   5,000 chunk writes became 90,997 operations / 64 MiB once the index entries
   were counted and the commit was refused. The caller cannot predict the
   expansion, so the mirror halves a batch until it fits. One sync is therefore
   several commits and not atomic; the mirror writes its freshness marker last.

4. **Revisions need stable identities, and livespec's are not.** A re-extracted
   file gets new `symbol.id` values, so an edit shows up in Kerno as
   delete + create (49 deleted / 50 created for one edited file), not as a new
   revision of the same unit. To get per-symbol history from Kerno, symbols
   would have to be keyed by `(path, qualified_name)` instead of the rowid.

5. **livespec finding: the run id is not a freshness key for the edge set.**
   The first shadow keyed the mirror on `index_run.id`, like the in-process
   graph cache. Nine tests failed under the flag, because `symbol_edge` also
   changes outside index runs (external-graph ingest, route edges resolved when
   a sibling project of a group DB is indexed). The mirror now checks a
   fingerprint of the edge set (count, max id, weight sum, own-origin count).
   Any future second store, or cross-process cache, needs the same care.

6. **No bulk read of documents, no change detection in bulk.** A no-op re-sync
   costs 5.5 s on Django: every symbol document is sent again for Kerno to
   compare, and the existing units are read one call at a time.

7. Smaller frictions: indexed text values hold 254 bytes (qualified names and
   paths need a truncated key plus the full value); `commit` takes documents
   only through `upsert`, not `store`; a unit can be written once per change
   set.

## Round 2: after the kernodb fixes (kernodb `main` at 7d020f6)

Findings 1 and 3 are fixed upstream (kernodb issues #12 and #13), and the Python binding gained
`Unit.document()` (no second read per unit) and `Database.neighbors()` (relations with the unit
at the other end, in one call); the mirror uses both when the installed build has them. Same
harness, same machine, kernodb built from source with LTO:

| Round (Django) | SQLite | Kerno before | Kerno after |
|---|---|---|---|
| Ingest the same rows | 2.6 s | 32.8 s | 29.5 s |
| Open + first query | 2.3 ms | 28.8 ms | 25.2 ms |
| Graph load | 923 ms | 1576 ms | 1137 ms |
| find_symbol_by_name | 6.1 us | 22.8 us | 21.6 us |
| find_by_qualified_name | 4.1 us | 17.0 us | 14.5 us |
| symbols_in_file | 48 us | 420 us | 284 us |
| callers_1_hop | 8.2 us | 59 us | 79 us (noise; unchanged) |
| Keyword search top 10 | 3.1 ms | 0.21 ms | 0.33 ms |
| Substring search | 70 ms | 27.5 ms | 27.9 ms |

Parity is unchanged (identical answers). The verdict does not move: Kerno narrowed the read
gaps (graph load is now 1.0-1.2x of SQLite) and still loses ingest, disk, open and point
lookups. Profiling says why, and none of it is a quick fix:

- a unit read is three point reads (head, revision metadata, payload) plus a content-hash
  check, where SQLite does one B-tree lookup;
- each neighbour of a relation read costs its `REL` record, the other end's head and a count
  check, by design (the engine verifies what it returns);
- every chunk is written into two inline text indexes (BM25 and trigram) through an LSM tree;
  SQLite builds one FTS index in one transaction;
- opening verifies every table's index and filter up front.

Closing those needs format or design changes in kernodb (a denormalised current record, lazy
table open, deferred index builds), not tuning.

## Round 3: read cache and parallel open (kernodb `main` at 9c19a99)

kernodb now keeps what a read verified (schemas, units, indexed-field ranges, the relations of
a unit) until the next commit, and opens its tables in parallel. Clean run, Django:

| Round | SQLite | Kerno round 2 | Kerno round 3 |
|---|---|---|---|
| find_symbol_by_name | 5.8 us | 21.6 us | 6.3 us |
| find_by_qualified_name | 5.4 us | 14.5 us | **4.7 us** |
| symbols_in_file | 47 us | 284 us | 104 us |
| callers_1_hop | 7.2 us | 79 us | 8.7 us |
| Graph load | 968 ms | 1137 ms | 1195 ms (FastAPI: **273 ms vs 299 ms**) |
| Open + first query | 3.8 ms | 25.2 ms | 19.7 ms |
| Keyword search top 10 | 3.1 ms | 0.33 ms | **0.21 ms** |
| Substring search | 69 ms | 27.9 ms | **21.6 ms** |
| Ingest the same rows | 2.5 s | 29.5 s | 32.9 s (noise; unchanged) |

Read the lookup rows with their condition: they are repeated reads between two commits (the
harness takes the best of three passes), which is what the cache serves. A first read of a
key costs what it did in round 2. That is the workload of an agent querying an index that is
not being rewritten, but it is not a cold-read win.

Where it stands: point lookups and one-hop callers are at parity with SQLite (within 1.2x
either way), search is 3-16x faster, graph load is within 1.2x either way. SQLite still wins
cold reads, open (5x), ingest (4-13x) and disk (1.5-3.3x). Of the 33 s Django ingest, the
trigram index is 17 s, relations 10 s, the keyword index 3.5 s and the units 2.5 s; with the
keyword index only (what the SQLite side builds) it is 20 s, still 7x.

## Round 4: lazy tables (kernodb `main` at 1a52d2e)

A read-only handle now loads each table at the first read that needs it. On the Django mirror
(356 MB, 50 tables), from Python:

| | SQLite | Kerno round 3 | Kerno round 4 |
|---|---|---|---|
| Open and close | ~0.05 ms | 11 ms | 1.1 ms |
| Open + one lookup by qualified name + close | 0.12 ms | - | 4.6 ms |
| Open + `find("__init__")` (911 hits) + close | 1.9 ms | 19.7 ms | 14.3 ms |

The open itself stopped scaling with the database, but a short-lived reader is still 7-38x
slower than SQLite: every table a read touches costs about a millisecond to load and check
(index decode, filter check), and a first read of a unit is still three point reads.

## Round 5: units layer 10 and cheaper bulk writes (kernodb `main` at 04abc45)

With the owner's go-ahead for format changes, kernodb now keeps a copy of a small payload in
the unit's head (one point read per unit instead of three; a format cut, layer 10) and
writes posting and adjacency blocks more cheaply. Same harness, Django:

| Round | SQLite | Kerno round 4 | Kerno round 5 |
|---|---|---|---|
| Ingest the same rows | 2.4 s | 32.9 s | 19.6 s |
| Open + first query (911 hits) | 2.2 ms | 14.3 ms | 5.7 ms |
| Graph load | 955 ms | 1195 ms | 1042 ms (FastAPI: **240 vs 267 ms**) |
| find_symbol_by_name | 6.8 us | 6.3 us | **6.2 us** |
| find_by_qualified_name | 3.9 us | 4.7 us | 4.2 us |
| symbols_in_file | 58 us | 104 us | 92 us |
| callers_1_hop | 7.1 us | 8.7 us | 8.1 us |
| Keyword search top 10 | 2.6 ms | 0.21 ms | **0.14 ms** |
| Substring search | 61 ms | 21.6 ms | **17.8 ms** |
| Disk | 132 MB | 355 MB | 356 MB |

Parity is unchanged (identical answers on the three repos). Kerno now leads on search
(3-19x) and is within 1.1-1.6x of SQLite, either way, on lookups, callers and graph load.
SQLite still leads on ingest (2.5-8x), on a short-lived reader (1.1-2.6x) and on disk
(1.5-3.5x). The profiled reason for the ingest gap is the stored design: one
read-modify-write per posting or adjacency list a change set touches, plus two count records
per relation. Closing it means writing a change set's relations and postings as sorted runs
merged later, which redefines adjacency, counts and `verify`: a project of its own.

## Round 6: staged relations, single large change sets, no trigram index

Build: kernodb branch `perf/staged-relations` at d47d692 (the package still
reports `1.0.0a4`). The mirror now sends one change set per type (`_BATCH` =
500,000, the halve-on-`ChangeSetTooLargeError` logic is unchanged) and does not
build the trigram index unless `LIVESPEC_EXPERIMENTAL_KERNO_TRIGRAM=1`, so
`grep` is answered by kernodb's parallel scan. The sandbox has about 1.5
effective cores, which limits the parallel scan. Parity checks all passed on
the three repos (graph nodes and edges equal, lookups and callers equal, 0
missing / 0 extra substring hits, keyword match counts equal).

The ingest row changed. SQLite's number is the subset ingest including the
checkpoint and close (rows are fetched before its timer). `Kerno write` is the
time inside `db.commit(...)` plus `close()`; `Kerno whole` is the full `sync()`
(SQL reads, Python diffing, writes) plus `close()`. `SQLite+trigram` adds an
`fts5(text, content='', tokenize='trigram')` table filled from `chunk`, the
equal-features reference for runs with the trigram flag.

| Metric | requests SQLite | requests Kerno | fastapi SQLite | fastapi Kerno | django SQLite | django Kerno |
|---|---|---|---|---|---|---|
| Ingest NORMAL vs Kerno write (ms) | 32.7 | 61.4 | 739.9 | 664.5 | 2943.0 | 4063.2 |
| Ingest SQLite FULL (ms) | 24.3 | - | 718.7 | - | 3276.0 | - |
| Ingest SQLite+trigram (ms) | 79.8 | - | 1187.7 | - | 9871.8 | - |
| Ingest Kerno whole sync+close (ms) | - | 73.0 | - | 1234.8 | - | 5909.8 |
| Disk (MB) | 1.45 | 3.99 | 27.09 | 23.83 | 132.02 | 147.38 |
| Disk with trigram (MB) | 3.12 | - | 38.12 | - | 248.91 | - |
| Open + first query (ms) | 0.209 | 0.176 | 0.4 | 0.702 | 2.497 | 7.03 |
| Graph load (ms) | 5.75 | 5.07 | 376.67 | 272.31 | 1350.55 | 1297.05 |
| find_symbol by name (ms) | 0.0041 | 0.0048 | 0.006 | 0.007 | 0.0078 | 0.0073 |
| find by qualified name (ms) | 0.0046 | 0.0051 | 0.0042 | 0.0046 | 0.0049 | 0.0047 |
| symbols in file (ms) | 0.0251 | 0.0339 | 0.0067 | 0.0093 | 0.0741 | 0.0878 |
| callers, 1 hop (ms) | 0.0031 | 0.0043 | 0.0032 | 0.0092 | 0.0084 | 0.0095 |
| Keyword top 10 (ms) | 0.2685 | 0.0406 | 0.5927 | 0.0559 | 3.0325 | 0.1978 |
| Substring (ms) | 1.3612 | 0.6343 | 7.217 | 1.6953 | 78.333 | 30.6156 |
| Re-sync after 1-file edit (ms) | 94.7 (whole re-index) | +60.0 | 948.8 (whole re-index) | +925.3 | 1816.9 (whole re-index) | +3609.6 |
| No-op re-sync (ms) | - | 20.7 | - | 754.1 | - | 3785.1 |

Where SQLite still wins:

- requests: ingest (write-only 1.9x, whole sync 2.2x), disk (2.75x smaller),
  and the four point lookups (1.1x to 1.4x).
- fastapi: ingest of the whole sync (1.7x), open + first query (1.8x), the
  four point lookups (1.1x to 2.9x; callers is the widest gap). Kerno is 1.1x
  faster on write-only ingest and 0.88x the disk size.
- django: ingest in both forms (write-only 1.4x, whole sync 2.0x), open + first
  query (2.8x), disk (1.12x smaller), `symbols_in_file` and callers (1.2x and
  1.1x), and the incremental and no-op re-sync, where the mirror alone costs
  about twice the whole SQLite re-index. Name and qualified-name lookups and
  graph load are within noise.
- Against the equal-features reference, Kerno write-only ingest is faster than
  SQLite+trigram on fastapi and django (4063 vs 9872 ms on django), but that
  comparison does not apply to the default configuration, which has no trigram
  index.

Kerno wins on keyword search (6.6x, 10.6x, 15.3x faster on requests, fastapi,
django) and substring search by parallel scan (2.1x, 4.3x, 2.6x), with the
same match counts as SQLite. Single-run numbers on a shared sandbox; treat
differences under about 1.2x as noise.

## Round 7: filtered neighbors, no-op shortcut, tuple-speed diff

kernodb build: branch `perf/neighbors-filter` (commit ad81a9e, one commit on top
of `main` d9c2908; not merged).

What changed:

- `Database.neighbors` takes `type_prefix=`. Relations whose type does not start
  with it are skipped before the unit at their other end is loaded. The mirror
  passes `e:<edge_type>` and still checks the parsed edge type exactly, so
  `e:calls_x` cannot pass as `calls`. It feature-detects the argument once and
  falls back to the unfiltered call on older builds.
- `sync()` returns immediately when the stored fingerprint (run id + edge set)
  equals the current one and is not `incomplete`, and, for `chunks=True`, a
  second stamp also matches. That stamp is `COUNT(*), MAX(id), SUM(LENGTH(text))`
  of the project's chunks, kept in its own `meta` unit (key `chunks`) so the
  fingerprint read by `load_graph_inputs` is unchanged.
- The edge diff and the symbol and chunk scans use tuple cursors, `itemgetter`
  / `attrgetter` / `map`, set differences, and one `_edge_type` call per distinct
  `(edge_type, weight, origin)`. Stats and semantics are the same.

| Repo | callers 1 hop, SQLite / Kerno (us) | symbols in file, SQLite / Kerno (us) | 1-file re-sync, SQLite re-index / Kerno mirror only (ms) | No-op re-sync, Kerno (ms) | Open + first query, SQLite / Kerno (ms) |
|---|---|---|---|---|---|
| requests | 2.7 / 5.0 | 24.0 / 31.6 | 67.7 / 36.3 | 1.3 | 0.258 / 0.179 |
| fastapi | 3.2 / 5.8 | 7.4 / 9.8 | 985.8 / 684.3 | 30.6 | 0.301 / 0.612 |
| django | 9.1 / 9.9 | 60.8 / 96.0 | 1877.8 / 3461.8 | 163.9 | 2.536 / 5.99 |

Callers 1 hop on fastapi went from 9.2 to 5.8 us (SQLite 3.2) and on django is
now within 1.1x of SQLite. The no-op re-sync on django went from 3.8 s to
0.16 s. The 1-file re-sync on django is 3.5 s against 3.6 s before: it changes
one file, so the shortcut does not apply, and the cost is dominated by the
engine commits and the unit reads, not the Python diff. Ingest, disk, graph
load, name lookups, keyword and substring search are unchanged in substance.
All parity checks pass, including `noop_resync_wrote_nothing`.

## Round 8: incremental re-sync, one-call lookups

kernodb build: branch `perf/neighbor-docs` (commit e48ab99, on top of
`perf/neighbors-filter` ad81a9e; not merged).

What changed:

- `sync()` has an incremental path. After every sync the mirror stores, in a
  `state` meta unit, the maximum symbol, edge and chunk ids, the symbol, edge and
  chunk counts, the rounded edge weight sum and the chunk text length. SQLite
  re-extracts a changed file under new ids, so the next sync reads only rows
  above those maxima, finds deleted symbols and chunks by set difference against
  the SQL id lists, and reads the relations of the deleted symbols to know what
  the cascade removes. Before writing anything it checks that the stored edge
  count, weight sum, symbol count and chunk count and length, adjusted by those
  changes, equal what SQLite has now. Any mismatch (an edge weight updated in
  place, an edge dropped between surviving symbols, a chunk edited in place with
  a different length) falls back to the full diff. The path never calls
  `relations()`. An edit that keeps a row's id and length (for example a symbol
  renamed in place) is not seen by the path; indexing does not do that.
- kernodb: `Database.find_documents(unit_type, field, value)` returns the hits'
  documents directly, and `Database.neighbor_documents(unit_type, field, value,
  direction, type_prefix)` does find, relations and documents in one call. The
  mirror uses them (feature-detected) in `find_symbol`, `find_qname`,
  `symbols_in_file` and `callers` / `callees`.

| Repo | 1-file re-sync, SQLite re-index / Kerno mirror (ms) | callers 1 hop, SQLite / Kerno (us) | by name (us) | by qualified name (us) | symbols in file (us) | open + first query (ms) |
|---|---|---|---|---|---|---|
| requests | 59.8 / 28.0 | 3.0 / 3.0 | 4.3 / 4.9 | 4.6 / 4.9 | 24.9 / 30.5 | 0.209 / 0.166 |
| fastapi | 999.6 / 190.4 | 3.4 / 4.4 | 6.7 / 7.5 | 4.4 / 4.6 | 7.5 / 8.2 | 0.268 / 0.536 |
| django | 1826.8 / 968.6 | 7.5 / 7.1 | 7.1 / 6.6 | 5.1 / 5.2 | 64.5 / 92.4 | 2.49 / 5.88 |

The Django 1-file mirror re-sync went from 3.46 s to 0.97 s (SQLite's own
re-index is 1.8 s); fastapi from 0.68 s to 0.19 s. What remains on Django is the
mirror's id-to-unit map and the chunk map, both read in full, and the SQL id
scans. Callers on requests equal SQLite and on Django are 1.1x faster; on
fastapi they are 1.3x slower. Symbols in file and open + first query moved
little. The no-op re-sync, ingest, disk, graph load, keyword and substring
search are unchanged in substance. All parity checks pass.

## Round 9: units layer 11, batched unit heads (kernodb `main` at cfe3875)

What changed, all in kernodb:

- Units layer 11: a payload small enough to live in the unit head is stored only
  there. Its `REV_PAYLOAD` record is written when a later revision supersedes it
  and history keeps it. Layer 10 files are refused (`LayerTooOld`); the mirror
  was rebuilt.
- `find` reads all unit heads of its hits in one sorted multi-get (each block
  once, threaded past 64 keys); read-only lazy tables are memory-mapped.

| Repo | Ingest, SQLite NORMAL / Kerno commit()+close (ms) | Disk, SQLite / Kerno (MB) | open + first query (ms) |
|---|---|---|---|
| requests | 24 / 83 | 1.45 / 3.25 | 0.38 / 0.43 |
| fastapi | 783 / 692 | 27.1 / 22.7 | 0.56 / 1.20 |
| django | 3005 / 4395 | 132.0 / 133.5 | 2.52 / 5.72 |

Disk on Django went from 147 MB to 133.5 MB (SQLite 132.0) and on fastapi Kerno
is now smaller. fastapi ingest is 1.1x faster than SQLite; Django is still 1.5x
slower. Open + first query did not move on Django: the 911-hit lookup spends its
time decompressing and checksumming about 770 blocks, not in the reads the batch
removed. Point lookups, callers, graph load, searches and re-syncs are within
run-to-run noise of round 8. All parity checks pass.

## Round 10: units layer 12, keyword postings by document ordinal (kernodb `main` at eab92e0)

A posting was 24 bytes (16-byte unit id, term frequency, document length) and
the posting records were 54 MB of the 248 MB the Django mirror holds before
compression. Layer 12 gives every indexed document a dense 32-bit ordinal, as
FTS5 does with rowids, and stores postings as varint deltas. Equal scores now
tie by ordinal (indexing order). Layer 11 files are refused; the mirror was
rebuilt.

| Repo | Ingest, SQLite NORMAL / Kerno commit()+close (ms) | Disk, SQLite / Kerno (MB) | open + first query (ms) | symbols in file (us) |
|---|---|---|---|---|
| requests | 25 / 50 | 1.45 / 2.50 | 0.21 / 0.22 | 22.3 / 27.7 |
| fastapi | 643 / 690 | 27.1 / 20.2 | 0.31 / 0.84 | 7.5 / 9.0 |
| django | 3047 / 4145 | 132.0 / 106.2 | 2.71 / 5.16 | 61.7 / 72.9 |

Disk on Django is now 0.80x of SQLite's and on fastapi 0.75x. Django ingest is
1.4x slower (was 1.5x); fastapi ingest is within noise of SQLite. The rows
SQLite still wins on Django are ingest, open + first query and symbols in file.
All parity checks pass.

### Round 10b: mimalloc in the Python extension (kernodb `main` at 89ed8ca)

malloc and free were a quarter of the main thread in a profile of bulk relation
writes. The extension now uses mimalloc for its own allocations.

| Repo | Ingest, SQLite / Kerno (ms) | open + first query (ms) | by name (us) | by qualified name (us) | symbols in file (us) | callers (us) |
|---|---|---|---|---|---|---|
| requests | 23 / 60 | 0.19 / 0.17 | 4.0 / 3.6 | 4.0 / 3.7 | 21.7 / 24.7 | 2.6 / 2.5 |
| fastapi | 649 / 661 | 0.40 / 0.91 | 6.6 / 6.7 | 4.9 / 4.3 | 9.0 / 8.1 | 3.2 / 3.3 |
| django | 3099 / 3680 | 2.42 / 4.14 | 7.2 / 6.4 | 5.1 / 4.4 | 56.2 / 68.1 | 7.8 / 6.8 |

Django ingest is 1.2x slower than SQLite (was 1.4x). Point lookups and callers
are now equal or faster on all three repos. SQLite still wins Django ingest,
open + first query on fastapi and Django, symbols in file on Django, and ingest
and disk on requests (a fixed cost that matters only at that size). All parity
checks pass.

### Round 10c: content hash on read optional, cheaper op conversion (kernodb `main` at 63b06f2)

Two changes. A unit's content hash is no longer recomputed on every read unless
the database is opened with `verify_payload_on_read` (the block checksum still
covers the bytes; `verify`, history, export and import always check it). And
`commit` converts Python ops without a string allocation per op (relate
conversion 0.44 to 0.21 us/op). Two more were tried and dropped: decompressing
without zeroing the buffer is not possible with the safe LZ4 decoder, and the
table index has no random access, so opening a table still decodes it.

| Django | SQLite | Kerno |
|---|---|---|
| Ingest (ms) | 2983 | 3676 |
| Disk (MB) | 132.0 | 106.2 |
| open + first query (ms) | 2.39 | 4.24 |
| symbols in file (us) | 65.5 | 76.7 |
| by name / by qualified name (us) | 7.0 / 5.0 | 5.2 / 4.0 |
| callers (us) | 8.8 | 7.6 |

No row changed sides. The cold 911-hit lookup in the Rust harness went from
about 4.0 ms to 3.2 ms, which the end-to-end row does not show above its noise.
SQLite keeps Django ingest (1.2x), open + first query (1.8x) and symbols in file
(1.2x). What is left in those is LZ4 decompression, block checksums, opening
tables and the revision records written per unit: none has a fix that keeps the
feature. All parity checks pass.

## Round 11: shared cached units, cheaper document dicts (kernodb `main` at 0566ff3)

A warm `find` served every hit from the read cache and then deep-cloned it;
the cache also hashed 16-byte ids with SipHash. kernodb now hands out the shared
unit and uses its id hasher (175-hit find in the Rust harness: 34 us to 8 us).
That did not move the end-to-end row, because building the Python dicts was
the larger cost: 160 us of 185 us for those 175 symbols. The binding now reuses
one interned str per field name per call (185 us to 131 us) and takes
`fields=` on `find_documents` and `neighbor_documents`. The mirror passes
`fields=` for symbols in file only: it costs 0.4 us a call, which a one-hit
lookup does not earn back.

| Repo | symbols in file, SQLite / Kerno (us) | by name (us) | by qualified name (us) | callers (us) |
|---|---|---|---|---|
| requests | 23.7 / 21.1 | 4.5 / 4.0 | 4.6 / 4.3 | 3.1 / 3.3 |
| fastapi | 8.0 / 7.6 | 6.0 / 6.4 | 4.7 / 4.4 | 3.3 / 6.2 |
| django | 95.6 / 46.9 | 10.5 / 9.2 | 7.2 / 7.0 | 8.0 / 8.1 |

Symbols in file is now faster on Kerno in all three repos, in each of the two
runs made after the change (Django 59.3 / 48.1 us in the other run). The
machine was noisy for this round: between those two runs SQLite's Django ingest
read 2988 and 3377 ms, the fastapi callers figure above is an outlier (3.7 us in
the other run), and Django graph load read 1.5 s for Kerno inside the benchmark
against 0.85 to 1.08 s measured alone, the same as with the previous build.
Django ingest (3377 / 3672 ms here, 2988 / 3798 ms in the other run) and open +
first query (2.5 / 3.6 ms) remain SQLite's. All parity checks pass.

### Round 11b: one-pass table index decode (kernodb `main` at f911d9a)

A profile of the main thread alone (the helper threads of the batched read are
not the wall-clock limit) put 29% of a cold 911-hit lookup in loading tables.
The index is now decoded and checked in one pass without an allocation per
entry: cold lookup in the Rust harness 3.4-3.5 ms to 2.9-3.2 ms. The re-check
of each hit against the looked-up value stays: it guards index and unit
consistency. An experiment with block compression off settled an earlier
guess: Django ingest was unchanged (3.6 s), the cold lookup went from 3.66 to
3.43 ms and disk doubled (107 to 208 MB), so compression is not what these two
rows cost.

| Django | SQLite | Kerno |
|---|---|---|
| Ingest (ms) | 3092 | 3884 |
| open + first query (ms) | 2.56 | 4.14 |
| graph load (ms) | 1362 | 1207 |
| by name / by qualified name (us) | 7.4 / 4.7 | 6.3 / 4.6 |
| symbols in file (us) | 62.4 | 48.3 |
| callers (us) | 8.2 | 7.5 |

On Django, SQLite keeps ingest (1.3x) and open + first query (1.6x); every
other row is Kerno's or level. On fastapi SQLite keeps open + first query
(0.35 / 0.83 ms) and callers (3.1 / 4.3 us); on requests, ingest and disk. All
parity checks pass.

## Round 12: point lookups without per-call schema work (kernodb `main` at 253326b)

A warm one-hit `find` spent about 18,500 instructions, most of it independent
of the data: the schema's field list was cloned on every call, the schema and
range caches hashed their keys with SipHash, and the value was escaped byte by
byte. With a shared schema, a cheap hasher and a scan-then-copy escape, the
find in the Rust harness went from 1.8-2.1 us to 0.8 us.

| Repo | by name, SQLite / Kerno (us) | by qualified name (us) | symbols in file (us) | callers (us) |
|---|---|---|---|---|
| requests | 4.6 / 3.4 | 4.6 / 3.4 | 24.4 / 19.4 | 2.9 / 2.7 |
| fastapi | 6.9 / 6.1 | 4.9 / 3.7 | 8.3 / 7.1 | 3.5 / 3.5 |
| django | 11.0 / 8.8 | 8.0 / 6.5 | 87.5 / 61.0 | 8.3 / 6.6 |

Every warm lookup row is now Kerno's or level on all three repos. What SQLite
keeps: ingest (Django 2979 / 3808 ms, fastapi 681 / 717 ms, requests 28 / 58
ms), open + first query (Django 2.5 / 5.3 ms, fastapi 0.55 / 1.00 ms) and disk
on requests (1.45 / 2.50 MB). All parity checks pass.

### Round 12b: content hashes off the commit thread, chunks committed first (kernodb `main` at 587b800)

A profile of the commit thread alone put 6.6% of a bulk ingest in the content
hash of each payload; it now runs in the parallel pass that already tokenizes
(175M instructions to 0 on that thread). The mirror commits chunks before
symbols and edges, so the largest table is written out in the background
during the other two commits and close has less left (close 0.7-0.8 s to
0.45-0.6 s).

Ingest did not get closer in this round's runs, and the reason is the machine,
not the build: with the same wheel and data, Kerno's Django commit + close read
3.7 s earlier in the day and 4.1 to 4.7 s now, for the previous build as well
as this one, while SQLite's single-threaded ingest stayed at 2.9 to 3.0 s.
Kerno's ingest leans on background threads (tokenizing, hashing, compressing,
flushing), and this box gives it about 1.5 cores, less at times.

| Django, two runs | SQLite | Kerno |
|---|---|---|
| Ingest (ms) | 3030, 2892 | 4299, 4396 |
| open + first query (ms) | 2.61, 3.32 | 4.27, 4.97 |
| by name (us) | 6.7, 6.6 | 4.6, 4.5 |
| by qualified name (us) | 5.0, 4.5 | 3.6, 3.2 |
| symbols in file (us) | 65.8, 59.3 | 47.9, 41.7 |
| callers (us) | 8.0, 8.0 | 6.3, 6.1 |
| graph load (ms) | 1302, 1221 | 1304, 1071 |

SQLite keeps ingest and open + first query on Django, open + first query on
fastapi (0.3 / 0.7 ms; fastapi ingest is level at 685 / 675 ms), and ingest and
disk on requests. All parity checks pass.

## Round 13: result dicts from the binding, callers without `fields=` (kernodb `main` at a94760c)

Wall-clock timers inside a cold 911-hit lookup (about 3.0 ms in the Rust
harness) gave: reading the 730 blocks 1.3-1.5 ms, decoding and caching the
units 0.34-0.38, close 0.35-0.43, the first schema read 0.30-0.34, the
agreement check of each hit 0.30, the index scan 0.27-0.31, open 0.2. Of the
five cuts tried, two held: the batch goes into the read cache under one lock
(0.31-0.34 to 0.25 ms), and `fields=` may be a mapping that renames, so the
mirror's many-hit lookups get their result dicts from the binding. Not kept: a
persistent pool for the batched read (it saved 0.1 ms in the read and added
0.17 ms to close); close has nothing avoidable (freeing 730 blocks and 911
units, unmapping); the agreement check already compared encoded bytes.

On the mirror side, callers no longer passes `fields=`: for the few callers a
symbol has, the keyword cost close to 1 us a call.

| Repo, second of two runs | by name, SQLite / Kerno (us) | by qualified name (us) | symbols in file (us) | callers (us) | open + first query (ms) | ingest (ms) |
|---|---|---|---|---|---|---|
| requests | 4.5 / 3.6 | 4.5 / 3.6 | 22.3 / 17.0 | 3.1 / 2.4 | 0.23 / 0.17 | 24 / 45 |
| fastapi | 7.8 / 4.5 | 4.2 / 3.2 | 6.8 / 5.3 | 3.0 / 2.0 | 0.29 / 0.50 | 665 / 552 |
| django | 6.9 / 4.6 | 4.6 / 3.3 | 56.7 / 35.6 | 8.3 / 5.6 | 2.19 / 3.49 | 2766 / 4077 |

Every warm lookup is 1.2x to 1.8x faster on Kerno in both runs, and fastapi
ingest is faster in both (813 / 717 and 665 / 552 ms). SQLite keeps Django
ingest (1.4-1.5x on this machine, see round 12b), open + first query on fastapi
and Django (1.4-2.0x), and ingest and disk on requests. Django graph load is
level (1146 / 1230 and 1151 / 1185 ms). All parity checks pass.

### Round 13b: buffered durability, and where the ingest profile ends (kernodb `main` at 99a6e25)

`kerno.open(durability="buffered")` exposes the engine's second durability mode
(a commit does not wait for stable storage; `close()` still does), and the
mirror uses it: it is what the SQLite side's `synchronous=NORMAL` does. On this
machine's disk an fsync costs under a millisecond, so no row moved.

The commit thread's profile is now flat. The last candidate, sorting a
transaction's entries at commit, was 116M of 2,169M instructions (the 288M first
read off the profile counted recursive calls twice); a prefix-keyed unstable
sort made it worse, because the standard stable sort already exploits the long
sorted runs the units layer produces, and a stable prefix sort saved 34% of
that 5%. Nothing was merged for it.

| Final table of this branch | requests | fastapi | django |
|---|---|---|---|
| Ingest, SQLite / Kerno (ms) | 32 / 64 | 687 / 615 | 3043 / 3911 |
| Disk (MB) | 1.45 / 2.50 | 27.1 / 20.2 | 132.0 / 106.1 |
| Open + first query (ms) | 0.22 / 0.18 | 0.35 / 0.61 | 3.69 / 4.76 |
| Graph load (ms) | 6.8 / 5.4 | 396 / 331 | 1279 / 1452 |
| By name (us) | 4.8 / 3.8 | 7.0 / 4.9 | 8.5 / 5.1 |
| By qualified name (us) | 4.7 / 3.6 | 4.3 / 3.8 | 5.4 / 3.9 |
| Symbols in file (us) | 25.5 / 19.0 | 7.9 / 6.2 | 68.6 / 44.2 |
| Callers (us) | 3.1 / 1.8 | 3.2 / 2.4 | 9.0 / 6.6 |
| Keyword top 10 (ms) | 0.33 / 0.06 | 0.55 / 0.08 | 2.76 / 0.19 |
| Substring (ms) | 1.04 / 0.57 | 5.17 / 1.53 | 75.3 / 25.9 |
| Re-sync after a 1-file edit (ms) | 62 / 20 | 971 / 167 | 1755 / 795 |

SQLite keeps: Django ingest (1.3x here), open + first query on fastapi and
Django, ingest and disk on requests. Django graph load is level across runs
(1.1x either way). Everything else is Kerno's. All parity checks pass.

## Round 14: large WAL frames written in pieces (kernodb `main` at ff32492)

Wall-clock timers inside the three large commits of a Django ingest found what
instruction counts could not (it is kernel time): the WAL. Every large commit
is written to the log and then again to its table, and the 94 MB frame of the
chunk commit was first built as one fresh 94 MB buffer (245 ms) and then
written (138 ms). Frames above 4 MiB are now encoded into a reused 1 MiB
buffer, checksummed incrementally and written piece by piece with the header
last; a crash before the header leaves what replay already treats as a torn
tail, and the bytes on disk are the same. That write went from 392 ms to
124 ms. The sorted writes of a commit are also deduplicated in place.

Two earlier attempts on ingest in this round were not merged: sorting a
commit's entries by a cached key prefix (the standard stable sort already uses
the long sorted runs), and resolving a batch of upserts' existing units in one
ordered scan (ingest unchanged, re-upsert slower).

| Django, two runs | SQLite | Kerno |
|---|---|---|
| Ingest (ms) | 3049, 2927 | 3701, 4083 |
| open + first query (ms) | 2.23, 2.36 | 3.57, 3.50 |
| graph load (ms) | 1131, 1398 | 1248, 1234 |
| callers (us) | 7.9, 8.1 | 6.2, 5.7 |

The chunk commit phase measured alone went from 1.7-2.1 s to 1.24-1.42 s, but
the machine carried outside load during these runs and the ingest row did not
change sides: SQLite keeps Django ingest (1.2-1.4x) and open + first query.
fastapi ingest is level to faster (668 / 676, 753 / 699 ms). The requests graph
load is one cold sample of a 5 ms operation and read 8, 32 and 34 ms in three
repeats; it was bimodal before this round too. All parity checks pass.

## Round 15: covering index on `name` (kernodb `main` at 7cf62f6, units layer 13)

The cold by-name lookup spent half its time reading the ~730 blocks that hold
the 911 matching units. Layer 13 adds a per-field `covering` flag: the field's
index entry carries the unit's inline payload, so the lookup reads the index
range alone. The mirror sets it on `name` only (feature-detected;
`LIVESPEC_KERNO_COVERING=0` turns it off). A layer-12 database opens as it is
and is marked 13 when the first covering schema is defined.

| Open + first query (ms), two runs | SQLite | Kerno |
|---|---|---|
| requests | 0.192, 0.192 | 0.107, 0.076 |
| fastapi | 0.294, 0.290 | 0.209, 0.213 |
| Django | 2.262, 2.463 | 1.327, 1.552 |

Same window, covering off: Django 3.52 and 3.30 ms. The row changes sides in
all three repos. Ingest with the flag on and off was within noise (Django
write phase 3.52 vs 3.37 s in one pair); the mirror grows about 5 MB on Django
in that pair and stays below SQLite (111-117 vs 132 MB).

Still with SQLite: Django ingest (2673 / 3263, 2841 / 3721 ms), and ingest
and disk on requests (22 / 44 ms, 1.45 / 2.75 MB). All parity checks pass.

### Round 15b: small close-time flushes are compressed (kernodb `main` at 6d68689)

A close skipped the LZ4 pass on the table it writes. For requests that table
is the whole mirror: 2.7 MB of records stored as 2.75 MB. A close now
compresses a memtable of up to 8 MiB (larger ones still skip the pass, which
keeps the Django close as it was).

| Disk (MB) | SQLite | Kerno |
|---|---|---|
| requests | 1.45 | 1.33 |
| fastapi | 27.09 | 20.71 |
| Django | 132.02 | 111.37 |

The pass costs a few milliseconds of the requests close. Requests ingest stays
with SQLite (24-28 ms against 44-52 ms in quiet samples, up to 110 ms under
load in this container), and so does Django ingest (2956 / 3671 ms).

## Round 16: ingest CPU (kernodb `main` at 715fd6b)

Wall-clock ingest in this container moves by +-0.4 s between identical runs,
so this round was decided on process CPU and callgrind instruction counts.

- `close_compaction=False` (`CloseBudget::Deferred`): a close always compacted
  L0 below its trigger, 335 ms of a 490 ms close after the Django load. The
  mirror's close now only flushes; the next writer session compacts in the
  background. This defers work rather than removing it.
- LZ4 through the crate's fast encoder (decoding stays on the safe one):
  process CPU of the Django ingest 6.1 -> 5.4 s.
- Op conversion in the Python binding (ids through a hex table and a per-call
  cache), upsert without a snapshot read when the unique field has no stored
  entry, one parse less per upsert, and a table-driven ASCII tokenizer:
  `Database.commit` on the fastapi load 1.538 -> 1.305 G instructions (-15%).

| Ingest (ms), three runs | SQLite | Kerno |
|---|---|---|
| requests | 28.9, 30.7, 29.4 | 110.3, 48.6, 70.3 |
| fastapi | 724.6, 704.4, 751.6 | 676.3, 714.8, 735.1 |
| Django | 2916, 3065, 3283 | 3560, 3665, 3551 |

The container was slower during these runs than in round 15 (SQLite's own
Django ingest rose from 2.7-2.9 s). SQLite keeps Django ingest by 1.1-1.2x and
requests ingest by 1.6x or more; fastapi is level. Disk and open + first query
stay with Kerno in all three repos. All parity checks pass.

### Round 16b: frozen-memtable flush without the merge iterator (kernodb `main` at 0597a11)

A flush of a large commit's memtable went through a one-child merge iterator
and a tombstone cover check per entry. It now walks the commit's sorted points
directly, and each data block starts with the previous block's capacity:
`write_memtable` over all threads on the fastapi load 309-353 M -> 168 M
instructions. Document keys of a batch of upserts are converted once.

| Ingest (ms), three runs | SQLite | Kerno |
|---|---|---|
| requests | 26.8, 28.5, 25.7 | 125.7, 60.8, 77.1 |
| fastapi | 671, 785, 786 | 506, 518, 622 |
| Django | 2785, 2905, 3030 | 3046, 3133, 3402 |

fastapi ingest is now 1.3-1.5x faster on Kerno. Django is 1.1x slower (0.23 to
0.37 s). Requests stays 2x or more: its 3,000 operations cost about 176 M
instructions in commit and close, 35 M of them skip-list inserts of commits too
small for the frozen path, plus about 20 sync calls (11 ms) against SQLite's
handful.

What is measured and left for Django: the WAL path of the three large commits,
0.35 s (0.24 s of it the sync at rotation, which recovery rule REC-10a
requires).

## Round 17: bulk commits without the log (kernodb `main` at fd47817)

REC-10a was left alone: cutting a torn middle log instead of failing the open
would let a `Sync` database lose confirmed commits silently. Instead kernodb
gained an opt-in, `Config::bulk_without_wal` (Python
`kerno.open(durability="buffered", bulk_without_wal=True)`): a commit of 512
writes or more writes no WAL frame, is kept as a frozen memtable (no skip-list
insert) and is durable once its table is installed. No later commit reaches a
log before that, and `sync()` and `close()` wait for it, so a crash still
leaves an exact prefix. The cost, and the reason it is off by default: until
the table is installed a process kill, not only a power cut, loses the commit.
The mirror turns it on; it is derived data and is rebuilt when its stamp does
not match. Recovery accepts a log that starts below the expected sequence only
when the MANIFEST already covers the gap; nothing else in recovery changed and
there is no format change. A crash-at-every-step test over the simulated disk
checks the prefix property.

| Ingest (ms), five runs | SQLite | Kerno |
|---|---|---|
| requests | 27.3, 24.7, 27.0, 28.9, 27.1 | 62.2, 28.4, 30.8, 36.6, 54.6 |
| fastapi | 701, 669, 671, 705, 663 | 363, 402, 379, 378, 395 |
| Django | 3175, 3250, 2846, 2876, 3067 | 2576, 2581, 2625, 2771, 2653 |

Django ingest changes sides (1.0-1.3x faster) and fastapi is 1.7-1.9x faster.
Requests is the one row SQLite keeps: 1.1x in the quiet runs, 2x when the
flusher thread and the committing thread compete for this container's ~1.5
cores (the small close went from 20 ms to 2 ms; what is left is per-operation
cost, about 37k instructions per unit created). Disk and every read row stay
with Kerno; all parity checks pass.

### Round 17b: per-unit cost on the commit path (kernodb `main` at f5198cc)

An upsert parsed its document two or three times, a created unit's head was
decoded back three times, keyword staging hashed each term again on the
committing thread, and several encoders grew their buffers by reallocation.
With those removed, `Database.commit` on the requests load went from 115.8 M to
98.8 M instructions (-14.7%).

| Ingest (ms), five runs | SQLite | Kerno |
|---|---|---|
| requests | 25.3, 25.5, 34.9, 28.5, 32.3 | 59.5, 37.9, 27.1, 28.4, 35.5 |
| fastapi | 629, 622, 641, 658, 780 | 331, 313, 320, 343, 336 |
| Django | 3028, 2791, 2979, 2862, 2869 | 2224, 2272, 2221, 2355, 2314 |

Django ingest is 1.2-1.4x faster and fastapi about 2x. Requests is level in the
quiet runs (27-28 ms on both sides) and behind when the three background
flushes of a 30 ms load compete with the committing thread for this
container's ~1.5 cores; it is not a row Kerno wins reliably here.

Close-time compaction stays off in the mirror
(`LIVESPEC_KERNO_CLOSE_COMPACTION=1` turns it back on): with it Django ingest
goes back behind SQLite (3.02-3.08 s against 2.93-3.00 s in the same window),
and the read rows were faster than SQLite either way.

### Round 17c: a measurement error in rounds 17 and 17b, corrected

The harness times `commit()` and `close()`. With `bulk_without_wal`, the first
small commit after the bulk ones waits for their tables to be installed, and
that small commit was the mirror's stamp `upsert()`, which the harness does not
time. The ingest figures of rounds 17 and 17b therefore left that wait out.
The stamps now ride in the last edge commit (one atomic write, one table less),
so the wait falls in `close()` and is timed.

| Ingest (ms), four runs, wait included | SQLite | Kerno |
|---|---|---|
| requests | 26.3, 32.8, 35.2, 26.7 | 86.3, 35.5, 43.9, 28.4 |
| fastapi | 939, 662, 683, 696 | 400, 348, 350, 347 |
| Django | 3361, 3170, 3120, 3239 | 2849, 2508, 2333, 2218 |

Django (1.2-1.5x) and fastapi (about 2x) stay ahead with the wait counted.
Requests does not: about 23 ms of commits plus a 6-8 ms tail of flushes and
close against SQLite's 26-33 ms, and the "level" reading of round 17b was the
artifact.

## If this continues

- Report finding 1 upstream; it is the only correctness problem found.
- The one promising direction is narrow: Kerno as the *search* index
  (`search`, `grep_in_indexed_files`) beside SQLite, where it is faster with
  the same recall. That trades 2-3x disk and a slower index for it, so it would
  need the upstream fix, a bulk document read, and a measured end-to-end tool
  latency before it is worth a dependency in alpha.
- Per-symbol history through revisions would need finding 4 solved first, and
  livespec already answers it from git (`get_symbol_history`).
