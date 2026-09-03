# Competitive note — Graphify vs livespec

> **Update (2026-09-03): ingestion landed. The graph is no longer read-only.**
> `ingest_external_graph` writes Graphify's dependency edges into `symbol_edge`
> under `origin='external:graphify'`, so `who_calls` and `analyze_impact` see
> them too — not just `find_dead_code`. See "Ingesting edges" at the end.
>
> **Update (2026-08-11): we now *consume* Graphify instead of only coexisting.**
> `find_dead_code(corroborate_with=<graph.json>)` reads a Graphify graph as
> corroborating evidence. This does not reverse the decision below — it is the
> strongest form of it. Importing is how you get inheritance edges and 36-language
> reach *without* building either. See "Consuming graph.json" at the end.

**Decision (2026-07-31):** do **not** port Graphify features into the 0.29
beta core. Overlap is real (tree-sitter call graph + MCP + anti-vector);
livespec’s wedge remains Spec↔code + polyrepo routes + framework endpoints.

Sources: https://graphify.net/ · https://github.com/Graphify-Labs/graphify
(re-checked 2026-07-31: MIT license, 36 declared languages, Leiden + god
nodes, `graph.json` with HTML/Obsidian/Neo4j export, LLM pass only for
docs/media.)

## Verdict

**Complementary products.** Agents can run both MCPs. Graphify is stronger on
multimodal docs→graph and edge provenance UX; livespec is stronger on Spec /
OpenSpec, `group_db` HTTP joins, and agent ops (`find_dead_code`,
`find_legacy_flows`, framework `find_endpoints`).

## Explicitly deferred (do not build now)

| Graphify idea | Why deferred |
|---------------|--------------|
| Leiden communities + HTML map | Explorer exists; PageRank covers ranking; new dep + surface |
| PDF / image / SQL / Terraform → graph | Needs LLM pass; fights FTS-only / local-first core |
| New `provenance` column | Already encoded as `symbol_edge.weight` + `edge_type` |
| MIT-style permissive default | Product stays AGPL-3.0-only by choice |

## Mapping we already have (no schema change)

| livespec signal | Graphify-ish label |
|-----------------|--------------------|
| `calls` weight ≥ 0.9 | extracted / strongly resolved |
| `calls` weight 0.7 | same-file scoped |
| `calls` weight 0.5 | ambiguous (fan-out; filtered by `min_weight=0.6`) |
| `invokes_route` | inferred (path join across client/server) |

## If an adopter asks for provenance later

Expose a **derived** `provenance` field on depth-1 `who_calls` /
`route_callers` from existing weight + edge_type. No migration, no re-extract.
Do not invent a parallel confidence system.

---

## Consuming `graph.json` (2026-08-11)

Verified against a real graph, not the docs: Graphify writes NetworkX node-link
JSON. Nodes carry `source_file` (repo-relative — same shape as `file.path`),
`source_location` (`"L53"`), `community`, and `_origin`. Links carry `relation`
(`calls`, `contains`, `references`, `rationale_for`, `imports`, `imports_from`,
`method`, `indirect_call`, `uses`, `inherits`, `re_exports`), `confidence`
(`EXTRACTED` / `INFERRED`) and `confidence_score`.

**A code-only run costs no LLM and no API key.** Extraction on `src/` of this
repo reported `input_tokens: 0, output_tokens: 0`, and every edge came back
`_origin: "ast"`. The semantic (LLM) pass only exists for docs/media.

### Why consuming beats coexisting — measured across 13 repos

A 14-repo sweep (`scripts/dogfood_corroboration.py`), not two anecdotes:

| Tool | Before | After | Helped in | Median where it helped |
|---|---:|---:|---:|---:|
| `find_dead_code` | 382 | **264** | 11 / 13 | 50% |
| `find_orphan_tests` | 273 | **260** | 3 / 7 | 47% |

Dead-code corroboration is broadly effective; orphan-test corroboration is
narrowly effective. Both are honest about finding nothing.

Drops came from `imports` (68), `indirect_call` (49), `calls` (4) and
`inherits` (4) — that is, overwhelmingly from *reference* relations livespec
does not model, not from disagreement about calls.

Spot-checked drops on one TypeScript service (46 → 33), all genuine livespec
misses:

| Candidate | Why livespec was wrong |
|---|---|
| `assertMaxContentLength` | imported *and* called one file over — resolver lost it |
| `DomainError` | alive only via `class BadRequestError extends DomainError`; we have no inheritance edge |
| `AppMetadata` | an interface used purely as a type annotation; we don't track type-position usage |

Every one of those is a livespec blind spot, not Graphify cleverness. That is
exactly why a second extractor is worth more as a **filter** than as a source.

On the same repo the two tools agreed on 385 of 434 `calls` edges (89%), which
cross-validates both. livespec still found more edges overall (2481 vs 1855).

### The sweep paid for itself immediately

The first run reported 382 → 167 (56%), with `method` as the single largest
evidence relation (98 of 223 drops). It was wrong. Graphify emits `method` as
`Class -> .method()` — always same-file, always from the declaring class, and
every method has one. It is containment, exactly like `contains`, and counting
it as evidence had quietly made every method of every class un-killable.

Moving it to `STRUCTURAL_RELATIONS` cut the measured benefit almost in half, to
the 31% above. **Two hand-checked repos had not revealed this; thirteen did.**
Worth remembering the next time a corroboration signal looks unusually strong.

### Boundaries held

- **Never a source.** Corroboration adds no symbols and writes no edges. The
  call graph stays ours; the external file only removes candidates.
- **Structural relations are not evidence.** `contains` and `rationale_for` are
  excluded — every symbol is "contained" by its file.
- **Fails loudly.** Missing file, unparseable JSON, or a graph whose paths don't
  overlap this index all return a shaped `mcp_error`. Reporting "0 dropped" for
  a graph nobody could match would read as a clean bill of health.
- **Zero-LLM stays honest.** Any edge not marked `_origin: ast` raises a
  `warning` in the payload rather than passing silently.

### Communities → Spec proposals (landed)

`propose_specs_from_codebase(community_graph=…)` groups by detected community
instead of qname prefix. Module prefix follows the folder layout, not the
capability: a feature split across `services/` and `routes/` reads as two
features. On the same TypeScript service this consolidated **20 proposals into
12** (177 of 189 symbols grouped by community).

Only community *membership* is consumed. Graphify's community *labels* are
LLM-written, so importing them would put a model in a deterministic path;
titles are derived from the member symbols instead. Spec ids are never seeded
from the community number either — Leiden ids depend on the clustering run, so
that id would change under the user on the next graph build.

### Orphan tests → corroboration also lands, but not everywhere

`find_orphan_tests(corroborate_with=…)` asks the mirror question: not "does
anything refer to this?" but "does this reach anything outside the tests?".

Two repos, two different answers, and the second is the useful one:

| Repo | Orphans | After | Why |
|---|---:|---:|---|
| Java service | 17 | **9** | JUnit `setUp` doing `new RepositoryImpl()` — direct instantiation livespec missed; verified by hand |
| livespec itself | 26 | 26 | in-process FastMCP `Client(mcp)` harness dispatches by string name — a blind spot **both** extractors share |

**Corroboration only helps where blind spots differ.** That is the general
lesson from this whole line of work, and it bounds how much a second extractor
can ever be worth: not "Graphify is more thorough" (livespec still finds more
edges overall) but "Graphify fails differently". Where the two fail the same
way — dynamic dispatch, string-keyed harnesses, reflection — nothing is
recovered, and the payload reports zero rather than pretending.

---

## Ingesting edges (2026-09-03)

The v0.32 note deferred this pending "the provenance question answered
deliberately rather than in passing". Here is the answer, and what it bought.

### The provenance question, answered

If a `graph.json` can put rows into `symbol_edge`, how does anyone tell them
apart afterwards, and how do they come back out?

Not by `weight`. That is a resolution-confidence ladder (1.0 resolved, 0.7
same-file scoped, 0.5 ambiguous fan-out) that `min_weight` filters on; a
livespec edge can legitimately hold any value on it, and
`UNIQUE(src, dst, edge_type)` means an ingested edge can land on a row we
already own. Nothing on that ladder makes an ingest *reversible*.

So: `symbol_edge.origin`, `DEFAULT 'livespec'` (migration 22). This is not the
parallel confidence system the table above refused to build — confidence still
lives in `weight`. `origin` says *whose claim* the row is, which `weight` has
never encoded and cannot. `DELETE ... WHERE origin='external:graphify'` touches
exactly the rows ingest wrote, and `_resolve_refs` reclaims a row to
`'livespec'` in its existing `ON CONFLICT` clause, so an ingested label never
outlives our own extraction of the same edge.

Three boundaries hold the rest, in descending order of how much they matter:

1. **The symbol table stays ours, absolutely.** An edge is ingested only when
   *both* endpoints already resolve to livespec symbols. Symbols are what every
   tool enumerates; the moment a foreign file can add one, every count in the
   product is partly someone else's.
2. **Every ingested row is labelled and reversible**, and every apply rewrites
   rather than accumulates — the state after an ingest is a function of the
   current graph and the current index, never of history.
3. **Ingested edges are ordinary edges afterwards.** That is the point; a
   labelled edge nobody reads is worth nothing. The tools say so in their
   payloads (`via_external_edge` at depth 1, an `external_edges` block) instead
   of pretending the answer is purely ours.

### Measured on this repo

Against a code-only Graphify run of livespec's own tree — 3564 nodes, 6089
edges, `input_tokens: 0`:

| | |
|---|---:|
| livespec symbols matched to an external node | 1394 / 1593 |
| ambiguous (two symbols claimed one node → dropped) | 1 |
| external `calls` edges livespec **already had** | 1250 |
| edges livespec **lacked** | **165** |

165 breaks down as 63 `calls`, 2 `indirect_call`, 83 `uses`, 17 `references` —
the same shape as the 133 the v0.32 note predicted, a little larger because the
tree grew. The 1250 agreed `calls` is the more interesting number: **95%
agreement** on the pairs both tools can see, which cross-validates both and
bounds how much a second extractor can ever be worth.

The concrete win is the one this document has been describing since v0.32.
`who_calls(ExternalNode)` returned **1** caller (the function that constructs
it) and now returns **5** — the four methods that take it as a type annotation.
livespec does not model type-position usage at all. That blind spot was
previously visible only to `find_dead_code`; now it is visible to the tool an
agent actually calls.

### Import relations stay off, and the measurement says why

Corroboration accepts `imports` as evidence, correctly: it answers "does
*anything* refer to this?", and imports were its single largest source (68 of
the 133 drops in the 13-repo sweep). Ingestion cannot inherit that. `who_calls`
does not distinguish edge types, so an ingested `imports` row would report
importers as callers — lying in one tool to improve another.

The measurement settles the trade at zero cost: ingesting **all nine**
relations on this repo adds exactly the same **165** edges. Graphify hangs
import edges off its per-file nodes, and a file node never maps to a livespec
symbol (`ExternalNode.is_file_node` has refused them since v0.32). The 509
import links skipped by default would all die at `endpoint_not_indexed` anyway.

That is also the clearest statement of why both features stay: **ingestion
makes the call graph better; corroboration answers a broader question more
cheaply. Neither subsumes the other.**

### Boundaries that did *not* survive unchanged

The v0.32 note said "Never a source. Corroboration adds no symbols and writes
no edges. The call graph stays ours." Half of that is now false and should be
read as superseded: livespec writes edges from an external graph, on explicit
request, labelled and reversible. **The symbol half is not negotiable and did
not move.**

The staleness window is the honest cost. A re-extract deletes the symbols of
every changed file and the FK cascade takes their ingested edges with them, so
after any `index_project` an ingest is partly gone and partly stale.
`index_project` samples the count *before* the run and reports both halves —
counting only afterwards would report nothing at all for exactly the rows the
run destroyed.

### Still deferred

The documentation layer (**316** `rationale_for` edges + **409** prose nodes).
It is still the closest thing Graphify has to our Spec↔code wedge and still the
only piece that would need an LLM — theirs, not ours, but an LLM in the path of
something we would present as traceability. Ingesting edges did not make that
question easier; it only means the mechanism (`origin`, reversibility, a
labelled payload) now exists if the answer ever turns out to be yes.
