# Self-dogfood — livespec on livespec-mcp (public beta 0.30)

**Date:** 2026-07-31  
**Workspace:** this repo root (absolute path passed as `workspace=`)  
**MCP:** `user-livespec` (local checkout)  
**Product pin:** `livespec==0.31.1`

## What we ran

1. `index_project` (already warm: ~165 files / ~1270 symbols / ~1972 edges)
2. `get_project_overview`
3. `list_specs`
4. `audit_coverage(summary_only=True)`
5. `find_legacy_flows(summary_only=True)`
6. `find_dead_code(summary_only=False, limit=10)`

## Snapshot

| Signal | Value |
|--------|------:|
| Languages | 8 (python heavy + fixtures) |
| Specs total | 14 |
| Specs linked | 12 |
| Modules truly orphan | 97 |
| Specs without implementation | 2 |
| Legacy servers | 0 |
| Orphan clients | 25 |
| Dead-code candidates | 2 |

## Specs without implementation

- **SPEC-013** (OpenSpec interop) — `link_count: 0` in this DB (seed/links may be stale vs current tree; product still ships the tools).
- **audit-audit-probe-only** — temporary audit probe Spec; ignore for product health.

## Orphan modules (expected noise)

Sample includes `bench/`, `scripts/`, and newer modules such as
`domain/legacy_flows.py` / `domain/openspec_export.py` that are not yet
wired into the self-Spec catalog. Orphans ≠ safe deletes.

## `find_legacy_flows` caveat (this repo)

All 25 “orphan clients” are **test/explorer HTTP probes** (e.g. GET
`/explorer/…` from `tests/test_explorer_asgi.py`). There is no production
HTTP service in this monorepo DB, so **graph ≠ traffic** and confidence is
`low`. Do not treat as deletion candidates.

## `find_dead_code` (2)

| Symbol | Note |
|--------|------|
| `extractors._route_handler_name` | Possible true unused helper — verify before delete |
| `openspec_export.export_openspec` | Likely false positive (CLI/tool call path not in graph) |

## Product posture validated by dogfood

- Cold-open tools work on the product’s own tree.
- Self-Specs exist and mostly link (12/14).
- Legacy tool correctly refuses to invent “dead servers” when none exist.
- Beta disclaimer holds: aggregator counts need human/APM judgment.

## Gaps to track (not blockers for beta)

1. Re-link SPEC-013 after OpenSpec surface churn.
2. Link or document intentional orphans (`legacy_flows`, scripts).
3. Drop or archive `audit-audit-probe-only` Spec from the dogfood DB.
4. Retitle DB-backed SPEC-006 title via re-import if `list_specs` still shows “Hybrid search & RAG” until next `import_specs` / sync.
