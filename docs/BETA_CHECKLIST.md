# Public beta surface checklist — livespec 0.30

Status: **complete** (2026-07-31 docs + dogfood pass).

## Positioning

- [x] README: public **beta** banner (local-first, FTS-only, graph ≠ traffic)
- [x] No “Unreleased” labels for features already in **0.30.0** (HANDOFF §3 / ROADMAP / deck)
- [x] Package story = `livespec` / `uvx livespec@0.31.4` (livespec-mcp = alias)

## Tool surface truth

- [x] 50 = 33 core + 12 Spec + 5 docs everywhere that counts tools
- [x] `find_legacy_flows` in PLAYBOOK + QUICKSTART + pagination lists
- [x] Express + Hono in default `find_endpoints` (README / PLAYBOOK / CLAUDE)
- [x] No current claim of `embed_chunks` / vectors / `agent_scratch` as live tools
- [x] No `LIVESPEC_WORKSPACE` as live env (HANDOFF / USAGE_DATA)

## Docs / agent UX

- [x] CLAUDE.md architecture matches 0.30
- [x] HANDOFF §1–3 current
- [x] ROADMAP “próximo pilar” / RAG-plugin superseded
- [x] Presentation deck → 0.30 beta
- [x] plugin Skill Express in framework list
- [x] AGENT_PLAYBOOK + AGENT_QUICKSTART aligned

## Dogfood (self)

- [x] `index_project` + `get_project_overview` on livespec-mcp
- [x] `list_specs` / `audit_coverage` / `find_legacy_flows` / `find_dead_code`
- [x] Write `docs/BETA_DOGFOOD.md` with counts + caveats

## Release hygiene

- [x] CHANGELOG `[Unreleased]` notes for this docs/beta pass
- [x] Tests still green *(639 passed, `-m "not embeddings"`)*
