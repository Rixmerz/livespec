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

## If this continues

- Report finding 1 upstream; it is the only correctness problem found.
- The one promising direction is narrow: Kerno as the *search* index
  (`search`, `grep_in_indexed_files`) beside SQLite, where it is faster with
  the same recall. That trades 2-3x disk and a slower index for it, so it would
  need the upstream fix, a bulk document read, and a measured end-to-end tool
  latency before it is worth a dependency in alpha.
- Per-symbol history through revisions would need finding 4 solved first, and
  livespec already answers it from git (`get_symbol_history`).
