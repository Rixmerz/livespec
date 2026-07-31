# Competitive note — Graphify vs livespec

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
