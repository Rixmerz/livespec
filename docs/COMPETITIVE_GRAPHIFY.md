# Competitive note — Graphify vs livespec

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

### Why consuming beats coexisting

Measured on a real TypeScript composer: **46 dead-code candidates → 27** once
corroborated. Of the 19 dropped, the relations were `indirect_call` (9),
`method` (6), `imports` (3), `inherits` (1), `calls` (1). Three were verified by
hand and all three were genuine livespec misses:

| Candidate | Why livespec was wrong |
|---|---|
| `assertMaxContentLength` | imported *and* called one file over — resolver lost it |
| `DomainError` (+4 methods) | alive only via `class BadRequestError extends DomainError`; we have no inheritance edge |
| `AppMetadata` | an interface used purely as a type annotation; we don't track type-position usage |

Every one of those is a livespec blind spot, not Graphify cleverness. That is
exactly why a second extractor is worth more as a **filter** than as a source.

On the same repo the two tools agreed on 385 of 434 `calls` edges (89%), which
cross-validates both. livespec still found more edges overall (2481 vs 1855).

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

### Still deferred

Ingesting edges into `symbol_edge`, and using `community` to group
`propose_specs_from_codebase` proposals. Both are attractive; both need the
provenance question above answered deliberately rather than in passing.
