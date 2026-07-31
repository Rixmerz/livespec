# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [SemVer](https://semver.org/).

## [Unreleased]

### Changed — license MIT → GNU AGPL v3.0 (`AGPL-3.0-only`)

Project is now licensed under the **GNU Affero General Public License v3.0**
only. Adds `LICENSE` (official AGPLv3 text), updates `pyproject.toml`
classifiers, and documents the network-copyleft obligation in the README.
Prior PyPI artifacts tagged under MIT remain as published; **new builds**
from this tree are AGPL-3.0-only.

### Docs — README badges, Spec Explorer screenshot, for/not-for

README hero: PyPI / Python / AGPL / tests badges; real Spec Explorer
screenshot (`docs/assets/spec-explorer.png`); short “Who this is for /
not for” table for human adopters.

### Docs — public beta surface alignment (post-0.29.0)

Honest product posture across README, CLAUDE, HANDOFF, ROADMAP, AGENT_* ,
presentation deck, Skill framework list, and self-dogfood:
`docs/BETA_CHECKLIST.md` + `docs/BETA_DOGFOOD.md`. Package story =
`uvx livespec@0.29.0`; maturity = public beta (not 1.0); graph ≠ traffic;
FTS-only; polyrepo/`find_legacy_flows` documented as shipped.
AGPL first-contact templates: `docs/AGPL_COMPLIANCE_CONTACT.md`.
Competitive deferral vs Graphify: `docs/COMPETITIVE_GRAPHIFY.md` (no Leiden /
multimodal / provenance-column clone).

## [0.29.0] - 2026-07-31

### Improved — plugin Skill + subagent (polyrepo / legacy safety)

`plugin/skills/livespec/SKILL.md` and `plugin/agents/livespec.md`: document
`group_db` cross-repo tools, `find_legacy_flows` traps (graph ≠ traffic;
orphan_client often = missing SA), honest Jest orphan zeros, docs-plugin
Explorer tools, and agent rules that forbid delete recommendations without
APM caveats.

### Improved — tool-value audit follow-ups (propose / orphan / git_diff / JSDoc)

- **`propose_specs_from_codebase`**: `skip_already_covered` now skips a module
  group when **any** symbol is already Spec-linked (was ≥50%). Payload adds
  ``skipped_covered_count``. Cuts duplicate proposals on brownfield hubs that
  already have Specs but sparse links.
- **`find_orphan_tests`**: always reports ``test_files_count`` /
  ``test_function_symbols``; when Jest/vitest leave only anonymous ``test()``
  callbacks (no function symbols), ``count: 0`` carries an honest ``hint``.
- **`git_diff_impact`**: when the diff is only unindexed/non-code paths and
  ``changed_symbols=0``, returns a ``hint`` (run ``index_project`` / expect no
  symbols from ``.dockerignore`` etc.).
- **`scan_docstrings_for_spec_hints`**: no longer soft-skips TS/JS-only repos —
  JSDoc already lives on ``symbol.docstring``. Dogfood grep seed uses the
  ``find_symbol`` query (was hardcoded ``route_ref``).

### Improved — `find_dead_code` auto-enables non-Python on TS/JS-only repos

When the workspace has zero indexed Python files, ``include_non_python``
turns on automatically (``auto_enabled`` in the payload). Avoids the
silent ``count: 0`` + ``not_swept: ["non-python"]`` trap on Express hubs.

### Improved — Spring `find_endpoints` hint under ``group_db``

``framework='spring'`` with ``count: 0`` on a hub workspace that shares a
group DB now lists sibling projects that have Java files and tells the
agent to call with ``workspace=<sibling>``.

### Fixed — group_db symbol lookup spans the whole shared DB

``find_symbol``, ``who_calls``, ``who_does_this_call``, ``quick_orient``,
``get_symbol_source``, and ``analyze_impact(target_type=symbol)`` resolve
qualified names across every project in a ``[workspace] group_db`` (home
project preferred). NetworkX still loads the **owning** project's graph;
``route_callers`` / ``invokes_endpoints`` remain DB-wide. Fixes the audit
failure where Composer could not resolve a HotelSvc controller qname.

### Improved — `find_legacy_flows` / Flow Explorer infra filter

Also exclude docs/UI operator paths: ``/api-docs``, ``/v3/api-docs``,
``/openapi.yaml``, ``/ui``, ``/playground``, ``/info``, plus prefixes
``/metrics/…``, ``/actuator/…``. Shared helper ``is_infra_route_path``.

### Improved — Express/Hono in default `find_endpoints` sweep

``framework=None`` now includes call-style Express/Hono routes (no more
``count: 0`` + ``not_swept: ["express"]`` trap). Explicit
``framework='express'|'hono'`` still filters.

### Changed — `scan_docstrings_for_spec_hints` uses JSDoc too

~~Earlier Unreleased note soft-skipped non-Python.~~ Superseded: the tool
now scans all languages; JSDoc on TS/JS symbols is included (see audit
follow-ups above).

### Removed — `agent_scratch*` + demoted Explorer from always-visible core

- Dropped MCP tools ``agent_scratch``, ``agent_scratch_get``,
  ``agent_scratch_clear`` (and ``scratch_note`` on ``quick_orient``).
  Table ``agent_scratch`` remains (migrations append-only).
- ``export_explorer`` / ``export_flow_explorer`` move into the
  ``livespec-docs`` plugin (5 tools with generate/list/export docs).
  Unlock via ``LIVESPEC_PLUGINS=docs|all``, Spec/docs rows, or an
  on-disk ``.mcp-docs/explorer/`` bundle (`index_project(explorer=True)`).
- Surface: **27** always-visible core + **12** Spec + **5** docs = **44**
  registered (tools/list still grows with plugins).

### Added — `find_legacy_flows` (likely-unused HTTP flows)

New aggregator over ``route_ref`` + ``invokes_route`` (works best with
``[workspace] group_db``). Reports **server** routes with no indexed client
hop (`legacy_server`) and **client** calls with no matched server
(`orphan_client`). Graph evidence only — not production traffic; payload
carries an explicit hint. Paginated (`limit`/`cursor`/`summary_only`).

### Improved — Tier-B noise reductions (dead_code / orphan / audit / grep / search)

- **`find_dead_code`**: protect Hono/Express handlers with the same
  import-map resolution as `find_endpoints`; new
  `include_ts_framework_routes` (FS-routing skip no longer tied to
  `include_infrastructure`); `skipped_fs_routing_count`; optional
  `min_weight` on inbound edges.
- **Framework DI / entry points**: Spring stereotype classes
  (`@Service`/`@Repository`/`@RestController`/…) protect all methods;
  Angular `@Injectable` protects service methods (not only lifecycle);
  FastAPI `include_router` / `add_api_route` / `FastAPI(lifespan=…)`
  registration + `@on_event` entry-point lastseg.
- **`find_orphan_tests`**: skip harness files (FastMCP Client / TestClient /
  supertest) and fixture/conftest paths by default; per-row `confidence` +
  `reasons`; optional `min_weight`.
- **`audit_coverage`**: per-list `cursors` input; content-aware empty
  `index.ts` markers; `modules_truly_orphan_sample` on `summary_only`;
  surface snapshot write failures as `warning`.
- **`grep_in_indexed_files`**: `match_mode`; ReDoS-prone / overlong patterns
  fall back to literal; `per_file_limit`; optional `fts_prefilter`.
- **`search`**: `keyword_search` (+ `hybrid_search` alias); quoted phrase
  mode; `index_fresh` / `query_mode` in payload.

### Fixed — mig v19 DROP of ``vec0`` tables without sqlite-vec

Opening a DB that still has ``chunk_vec_*`` virtual tables (e.g. the
flow-group ``group_db``) failed with ``no such module: vec0`` when the
extension is not loaded. DROP is now best-effort; orphan vec tables are
left in place (FTS path ignores them).

Vector embeddings and Reciprocal Rank Fusion are gone. ``search`` is
**FTS5-only** (AST-aware chunks unchanged). Dropped: MCP tool
``embed_chunks``, ``index_project(embed=…)`` / CLI ``--embed``, optional
extra ``[embeddings]`` (fastembed + sqlite-vec), CI embeddings job.
Migration **v19** drops ``chunk_vec_*`` tables and ``chunk.embedded_at``.
Plugin ``.mcp.json`` launches bare ``livespec@…`` (no extras).

### Added — thin HTTP wrapper client routes (`makeRequest(url)`)

Same-file helpers whose first param is forwarded to `fetch`/`axios`/`got`
are treated as HTTP wrappers. Callers like a client's
`makeRequest(\`${baseUrl}/search\`, body)` emit a client `route_ref` on the
caller (not the wrapper). Non-forwarding helpers are ignored.

### Fixed — `await axios.get<T>(url)` dropped as client route

tree-sitter-typescript puts an `await_expression` in the call's function
field when generics combine with `await`. Client route detection now unwraps
that so a client's `await axios.get<Hotel[]>(requestUrl)` emits `route_ref`.

### Fixed — JS ternary `?` truncated multi-segment template URLs

`_path_from_template_raw` stripped on the first `?`, which broke
`${cond ? `x/` : ""}` builders (composer-service→HotelSvc `/list/{}/{}/{}`).
Query/fragment are stripped only after `${...}` collapse (or on plain
literals). Nested-ternary paths collapse to match Spring
`/list/{arrival}/{departure}/{hotels}`.

### Fixed — axios client routes discarded when 2nd arg is an identifier

`_TS_HANDLER_ARG_TYPES` included `identifier`, so a client's
`axios.post(url, body)` was treated as an Express server registration and
emitted no client `route_ref`. Client detection now only treats function/
arrow args as handlers; `router.post('/x', ctrl.fn)` stays on `_server_route`.

### Added — Express/Hono server `route_ref` + template/ident URL clients

Indexing persists Express/Hono `router.verb('/path', handler)` as server
routes. Client axios/fetch/got resolve `` `${base}/path` `` templates and
same-function `const url = …` bindings. Java `@GetMapping("/x")` emits
server routes too.

### Added — Flow Explorer HTTP filter + `route_edges`

`export_flow_explorer` keeps only concrete HTTP routes (drops Angular
`@Component` noise), exports resolved `route_ref` / `invokes_route` hops as
`route_edges` + Mermaid edges, and reports an accurate `route_ref` count.
Infra paths (`/health`, `/liveness`, …) are omitted so cross-service noise
does not drown product hops.

### Added — `got` client routes

TS/JS extractor treats `got("/path")` and `got.get/post/...` like axios/fetch
for `route_ref` client rows.

### Added — Java Javadoc `@spec:` linking

`java` is in `ANNOTATION_SUPPORTED_LANGUAGES`. Leading `/** @spec:ID */` on
methods links Specs on index (same pipeline as JSDoc).

### Added — Jest/Vitest coverage report ingestion

`coverage/coverage-final.json` and `coverage/lcov.info` feed Spec test-coverage
as an OR with graph-derived / explicit `tests` links (`coverage_source` may
include `report`).

### Added — `export_flow_explorer` (cross-repo Flow Explorer)

New docs-plugin tool writes `.livespec-group/flow-explorer/{data.json,index.html}`
(or `.mcp-docs/flow-explorer/` when ungrouped). Aggregates every project in the
shared `group_db`: repo cards, mirrored `xrepo-*` specs with per-repo symbols,
Mermaid flow (project→spec / spec→spec), and per-repo API endpoints. Spec edges
plus resolved HTTP `route_edges` when the indexer matched client→server routes.

### Fixed — Spec Explorer blank Overview (broken template literals)

Two viewer template literals ended with `';` instead of `` `; `` (`call-shape`
MCP try-it block and trend “Verified Specs” meta). The SyntaxError aborted the
whole script so only the header painted. Regenerated explorers need a hard
refresh. Regression: `node --check` on the inlined viewer JS in
`test_export_explorer_writes_both_files`.

## [0.28.1] - 2026-07-28

### Fixed — Express/Hono handlers resolve via import bindings

`wrap(details)` no longer links to a colliding `services.suppliers.details`.
Route files' import map prefers the imported module; anonymous
`export default async () => {}` mints a basename symbol (`liveness`) so
default-export controllers link. Member handlers keep
`handler_import=healthController` for the same scoping.

### Docs — reload MCP after local checkout edits

`mcp_auth` alone may not restart a long-lived `uv run livespec-mcp` process.
Kill the local process (or toggle the server off/on) so the host respawns
from the checkout.

## [0.28.0] - 2026-07-28

### Added — stable `spec_id` round-trip for OpenSpec export/sync

`export_openspec` writes `<!-- livespec:id=SPEC-001 -->` under each
`### Requirement:`. `parse_openspec_markdown` / `sync_openspec` prefer that
marker over title-slug ids, so create → export → sync no longer duplicates
`SPEC-001` as `indexing-indexing-workspace-walk`.

### Added — `@spec:` OpenSpec slug allowlist

`parse_annotations(..., known_ids=...)` / `scan_annotations` match kebab
ids already in the store (`@spec:auth-user-login`, `implements auth-user-login`)
without an open kebab regex over prose.

### Added — Express member / wrap handler resolution

`scan_hono_routes` resolves `healthController.check` → `check` and
`wrap(liveness)` → `liveness`, so `find_endpoints(framework="express")`
links those routes to symbols.

### Changed — `create_spec` / `propose_specs_from_codebase` OpenSpec-first ids

Default auto-ids are OpenSpec-shaped slugs (`auth-user-login`). `SPEC-NNN`
remains the collision fallback (MAX numeric, not last-inserted). Dogfood
specs live under `openspec/specs/` with stable `<!-- livespec:id=SPEC-NNN -->`
markers; `docs/requirements/livespec-specs.md` is legacy import-compat.

### Added — `find_endpoints(framework="express")`

Call-style Express routes (`router.get/post/...`) were invisible: the Hono
scanner required the string `hono` in source. Express a client APIs
(`composer-flight-service`, `composer-service`) therefore reported
`count: 0`. `framework="express"` reuses the same AST scanner for files that
mention `express`, and the default-sweep zero-hint now suggests
`framework='express'` when those files are indexed.

Receiver allowlist (`app` / `router` / `*Router` / …) drops false positives
from `axios.get` / `cache.get` / `headers.get`.

### Changed — OpenSpec is the preferred authoring nomenclature

Guidance and defaults now treat **OpenSpec markdown** (`### Requirement:` /
`#### Scenario:` under `openspec/`) as the authoring source of truth. Livespec
remains the code-graph / Spec↔code engine beneath it. The native
`## SPEC-NNN:` catalog is documented as **legacy import-compat** only.

- Skill + AGENT_PLAYBOOK + README: OpenSpec-first loops; stop forcing
  `fmt="livespec"` on OpenSpec files (`fmt="auto"` / `openspec`).
- `detect_spec_format`: if `### Requirement:` is present, prefer `"openspec"`
  even when `## SPEC-NNN:` headers also appear.
- Config example / tool docstring: `openspec_dir` first; `sync_from` native
  catalogs demoted.

## [0.27.0] - 2026-07-27

### Fixed — `SPEC` was hardcoded as the only valid spec-ID prefix

Any project using its own ID scheme (`BE-RF-102`, `FE-RF-119`, `ADR-007`) could
never use `@spec:` annotations. Two regexes and the normalizer all assumed
`SPEC-NNN`:

- `_SPEC_TOKEN_RE` and `_VERB_RE` (level-2, verb-anchored) each hardcoded
  `SPEC[-_]?\d{1,6}`, so `@spec:BE-RF-102` parsed the verb and then dropped the
  token — the annotation linked to nothing, silently.
- `_normalize_spec` extracted digits from the *whole* string and re-emitted
  `SPEC-NNN`, so even a matching token collapsed namespaces: `BE-RF-56` and
  `FE-RF-56` both became `SPEC-056`.

Measured on a real TS backend + frontend: **1022 annotations, 0 linkable.**

The accepted prefix set is now **derived from the spec IDs already in the
store** (`derive_spec_prefixes`, always unioning `SPEC`) rather than configured.
Deriving it means zero config surface, no drift from reality, and no false
positives — a generic `[A-Z]+-[A-Z]+-\d+` shape was rejected precisely because
it would eat `RFC-2119` and `ISO-8601` out of prose. `scan_annotation_verbs`
builds its token shape from the same derived set, so the diagnostic and the
linker can no longer disagree about what counts as a valid token.

Prefix-preserving normalization keeps the store's existing padding:
`BE-RF-56` → `BE-RF-056`, `SPEC-1` → `SPEC-001`, `SPEC-901` unchanged.
`parse_annotations` defaults to `prefixes=("SPEC",)`, so every existing caller
is byte-identical.

## [0.26.0] - 2026-07-27

### Fixed — the test-file detector was blind to JavaScript/TypeScript

`_is_test_file_path` matched only Python/Go/Rust conventions (`tests/` segment,
`test_` prefix, `*_test.py`, `*_test.go`). Measured on a real TS backend +
frontend: **93 test files, 0 detected.** Two tools were silently wrong as a
result — `find_orphan_tests` returned `count: 0`, which reads as "no orphan
tests" but actually meant "no tests found at all", and `compute_spec_test_coverage`
BFSes forward from the test-symbol set, so an empty seed gave every Spec a 0.0
ratio on a repo with a real suite.

The detector now also matches `__tests__/` and `test` / `spec` path segments,
the `.test.` / `.spec.` / `_spec.` basename suffixes for `.ts .tsx .js .jsx
.mjs .cjs`, and Ruby's `_spec.rb`. Matching is segment- and suffix-anchored, so
`src/contest/index.ts`, `src/latest.ts`, `src/protest.ts` and
`src/attestation.ts` do not match. The duplicate copy of the heuristic nested
inside `find_orphan_tests` was deleted in favour of the shared helper.

Note for trend history: `spec_coverage_snapshot` rows keep the same shape, but
`avg` / `verified_count` will now be non-zero on JS/TS repos where they were 0.0,
so pre-fix snapshots are not comparable with post-fix ones.

### Changed — `audit_coverage` test-coverage counts renamed (response shape)

`specs_with_test_coverage: 17` next to `specs_with_any_test_coverage: 0` read as
corrupt data. They are not contradictory — they measure different mechanisms.
Renamed so the distinction is legible:

| old | new | measures |
|---|---|---|
| `specs_with_test_coverage` | `specs_with_linked_tests` | Specs with ≥1 explicit `relation='tests'` link |
| `specs_with_any_test_coverage` | `specs_with_derived_test_coverage` | Specs whose auto-derived call-graph ratio is > 0 |

No back-compat aliases. The Spec Explorer bundle and README were updated to match.

### Changed — `get_project_overview` no longer ranks test scaffolding as the core

On a real TS backend, 5 of the top 10 PageRank symbols were test infrastructure
(`createMockDb`, `makeModel`, `signTestToken`, `bearerToken`,
`fakeAuthMiddleware`), which defeats the tool's purpose. Symbols in test files
are now excluded from `top_symbols`, in the same spirit as the existing
bundler-output and structural-pattern filters. Nothing is silently dropped: the
new `test_symbols_filtered` field lists them by qualified name. Unlike
`structural_patterns_filtered` (a full DB query) it is collected inside the
ranking loop, so it holds the test symbols that outranked the last returned
entry — the actionable set, not a census.

### Fixed — the same blindness in two sibling detectors

`_is_test_scaffold_path` (endpoint filtering) and the nested `_is_test_path` in
`specs.py` each kept their own narrower copy of the heuristic, so widening the
main one left them Python-only. Verified live on a Hono backend before fixing:
`find_endpoints` listed `POST /register`, `/login`, `/refresh` and `/logout`
from `src/routes/v1/auth.test.ts` **beside the genuine routes of the same name**
in `auth.ts`, with nothing to distinguish them. Both now delegate; the extras
that are scaffolding-but-not-a-test-file (`conftest.py`, `fixtures/`,
`bin/`, `scripts/`) stay where they were.

## [0.25.0] - 2026-07-27

### Fixed — `grep_in_indexed_files` no longer hides an incomplete answer

The tool greps the bodies of files **in the index**. A file present on disk but
never indexed contributes no matches, so the caller saw a clean empty result and
concluded the pattern does not occur. Same failure shape as `find_endpoints`
returning `count: 0` without saying it never swept Hono.

Every response now carries **`scope_fresh`**. When false it is accompanied by
`unindexed_files` / `unindexed_files_count` (files on disk the index has never
seen — these genuinely hide matches) and `stale_files` / `stale_files_count`
(indexed files whose content changed — their *matches are still returned*, since
grep reads current bytes off disk, but their symbols and edges are outdated), plus
a `hint` naming `index_project(workspace=..., force=false)`. The two are reported
separately so `stale_files` is never misread as "matches were hidden".

The verdict is bound to the `path_glob`/`kind` scope actually searched — hence
`scope_fresh`, not `index_fresh`. Claiming more would repeat the overclaim being
fixed. Existing response fields and all parameters are unchanged.

Cost: the change-detection half is free (re-hashes bytes the grep loop already
read). Detecting never-indexed files needs one workspace walk — measured at 18 ms
on this repo's 155 files, no file reads and no parsing.

## [0.24.0] - 2026-07-26

### Changed — the distribution is now `livespec`

**`pip install livespec-mcp` / `uvx livespec-mcp` no longer resolve.** Use
`livespec`. The `livespec-mcp` console command still exists as an alias inside
the package, so a local checkout or an MCP config that invokes it keeps
working; only the *distribution* name changed.

The package, the console command and the GitHub repo now agree — they had
drifted (`livespec-mcp` on PyPI, `livespec` on GitHub), and since PyPI Trusted
Publishing matches on the repository name, every release silently failed with
`invalid-publisher`. That is why 0.23.0's publish workflow failed and why
0.24.0 could not ship until this landed. `livespec-mcp` cannot be reclaimed —
PyPI permanently blocks a deleted project name.

The import package stays `livespec_mcp`; renaming it would churn every module
path and `@spec` link for no user-visible gain.

### The audit

Audit pass against a production Hono/Deno TypeScript backend. The theme: the
tools returned a plausible result instead of admitting they had not looked.

### Fixed

- **Call edges were discarded when the call site sat inside an anonymous scope.**
  `app.post("/", async (c) => { await svc(...) })` — the dominant shape in any
  Hono/Express/Fastify app — produced no symbol for the arrow, so the edge was
  thrown away rather than attributed. `who_calls` returned
  `{"callers": [], "count": 0}` for a function a plain grep finds instantly.
  Edges are now attributed to the nearest named enclosing symbol, falling back
  to a per-file module symbol, so an edge is never dropped for want of a name.
- **`find_dead_code` inherited it**: 807 "dead" symbols, of which 5 of 5 sampled
  were verified alive by grep. An agent trusting that list deletes the service
  layer.
- Pre-existing double count: a nested `const inner = () => {}` had its calls
  attributed to both itself and its enclosing function.
- **`find_dead_code` returned 0 on a TypeScript repo** and `find_endpoints`
  returned 0 on one with 777 routes — a default filter excluded the entire
  corpus and neither said so. Both now report `not_swept` plus a hint grounded
  in real index counts.
- **Read-only tools created a SQLite DB in whatever directory they were pointed
  at.** A typo in `workspace` left an orphan `.mcp-docs/` anywhere, and
  `{"specs":[]}` was indistinguishable from "indexed but empty". Creation is now
  exclusive to `index_project`; reads fail with a structured hint instead.
- **Build output was indexed**: 250 of 466 files were `_fresh/` bundles, and
  `find_symbol("ErrorBanner")` resolved to minified code instead of the
  component. `deno.json` already declared the exclusion; only `.gitignore` was
  being read. `deno.json`/`deno.jsonc`/`tsconfig.json` `exclude` are now honored.
- `list_specs` and `validate_openspec` could exceed the token limit with no
  argument combination that returned. Both are bounded now; `validate_openspec`
  went 56,238 → 2,531 chars and reports a check firing on 185/185 specs once as
  a project-level gap instead of 185 times. `git_diff_impact` honors
  `summary_only`.
- `export_explorer` had no `framework` parameter, so no correct Explorer could
  be generated for a Hono repo. `agent_scratch` validated nothing and had no
  read counterpart.

### Added

- `scan_annotation_verbs` — 763 `@rf:` annotations linked nothing and no tool
  said so. The matcher accepts only `@spec`/`@implements`/`@tests`/`@see`/
  `@references` with a `SPEC-NNN` token, so every one of them was decoration.
  Reports them with `file:line` and `did_you_mean`, suggesting `@spec` when the
  payload looks like a spec id rather than trusting edit distance (which ranks
  `rf`→`see` above `rf`→`spec`).
- `agent_scratch_get`, plus `cursor`/`summary_only` pagination on `list_specs`.

## [0.23.0] - 2026-07-24

> Follows `0.22.0` (published to PyPI from the `v0.22.0` tag). This section
> covers everything since 0.20.0; 0.21.0 was skipped.

### Fixed
- **OpenSpec sync now matches real repo layout (battle-tested against
  Fission-AI/OpenSpec).** Two ingestion bugs surfaced by running `sync_openspec`
  against the real OpenSpec dogfood tree:
  - Archives are read from `openspec/changes/archive/` (the layout the current
    OpenSpec CLI writes) as well as `openspec/archive/`. Previously the
    `archive/` subdir under `changes/` was mistaken for a change named
    "archive" and the real archived changes were skipped.
  - A change-only tree (no `openspec/specs/`) no longer imports in-flight change
    *deltas* as canonical source-of-truth specs — canonical specs come only from
    `specs/` (or from applying changes). The real OpenSpec content parsed and
    `validate --strict`-passed cleanly; a legacy pre-structured archived spec
    (no `### Requirement:` anchors) degrades to zero requirements without error.

### Added
- **PyPI publish via Trusted Publishing** — new `.github/workflows/publish.yml`
  builds + publishes on every `v*` tag using OIDC (no token/secret in the repo);
  guards that the tag matches the package version. One-time trusted-publisher
  config on pypi.org required.
- **`livespec` console command** — an additive alias of `livespec-mcp` so the
  command matches the product name. After a local install both invoke the same
  entry point. The distribution name and `uvx livespec-mcp` are unchanged (no
  break to existing installs/MCP configs).
- **Self-spec `SPEC-013` (OpenSpec interoperability)** — the OpenSpec interop
  modules/tools are now dogfooded on livespec itself (added to
  `docs/requirements/livespec-specs.md` + the `livespec-spec-links.json` seed;
  38 verified links, all resolving).

- **OpenSpec interop — Tier 2 depth.** Three functional gaps from the v0.22
  compatibility pass are now closed:
  - **`RENAMED` deltas** (`## RENAMED Requirements` FROM/TO). The parser reads
    the `- FROM:`/`- TO:` pairs; `apply_spec_change` performs a real *move* —
    it upserts the new requirement and migrates the old spec's traceability
    (`spec_symbol` + `spec_scenario` links) onto it, then drops the old spec
    (schema migration v18 adds `spec_change_delta.rename_from`).
  - **`## Purpose` round-trip.** A capability's Purpose prose is now stored on
    the module (`module.description`) at import and re-emitted verbatim by
    `export_openspec`, instead of being synthesized.
  - **`apply_spec_change` applicability validation + `dry_run`.** Apply now
    returns `warnings` (MODIFIED/REMOVED/RENAMED targeting a spec that doesn't
    exist, ADDED that would overwrite an existing one); `dry_run=True` returns
    the `plan` + `warnings` without mutating.
  - Note: OpenSpec's `openspec/config.yaml` (schema/context/rules) is
    deliberately not generated on export — it is tool-managed config, not
    derivable from the spec set.

### Changed
- Self-spec dogfood files rebranded to `livespec` (headers, SPEC-013 count).

### Fixed (v0.22 compatibility pass)
- **`audit_coverage` now records coverage-trend snapshots on Python 3.10.**
  `from datetime import UTC` (used to timestamp the snapshot) is 3.11+, so on
  3.10 it raised `ImportError` inside a best-effort `try/except` and the
  snapshot was silently never written — the explorer coverage trend stayed
  empty on 3.10 (the project supports `>=3.10`). Switched to
  `datetime.now(timezone.utc)`, which works on every supported version.
- **Python callback arguments now create real caller edges — kills a
  false-positive dead-code class.** Found by dogfooding livespec on itself: a
  function passed as an argument (`Watcher(on_reindex=cb)`,
  `atexit.register(cleanup)`, `sorted(xs, key=fn)`) was never recorded as a
  reference, so it had zero callers and `find_dead_code` flagged it dead (and
  `who_calls`/`analyze_impact` missed the edge). The extractor now emits a
  `callback_arg` ref — mirroring the TS side — conservatively scoped to known
  registration/scheduling callees (`register`/`connect`/`submit`/…) or
  callback-signalling keyword names (`target=`/`key=`/`on_*`), so plain data
  arguments don't inflate the graph. Improves the accuracy of every call-graph
  tool, not just dead-code.

### Added
- **OpenSpec (Fission-AI) full compatibility — round-trip + change lifecycle
  (v0.22).** livespec could *read* OpenSpec markdown since v0.21; it is now a
  first-class citizen of an `openspec/` repo. Three layers landed:
  - **Scenarios are first-class** (schema migration v15, `spec_scenario`).
    OpenSpec's atomic testable unit — the `#### Scenario:` WHEN/THEN block —
    was previously flattened into `spec.description`; it is now modelled as
    rows, surfaced by `get_spec_implementation` (`scenarios[]`,
    `coverage.scenario_count`) and counted in `list_specs` (`scenario_count`).
  - **Export closes the round-trip.** New `export_openspec(out_dir="openspec",
    include_changes=True)` writes the canonical OpenSpec tree
    (`specs/<capability>/spec.md` with `## Purpose` / `### Requirement:` /
    `#### Scenario:`) plus `changes/` + `archive/`. Capability == the spec's
    `module`; only non-deprecated specs are emitted as canonical requirements.
  - **Structural validation.** New `validate_openspec(strict=False)` mirrors
    `openspec validate [--strict]` — the load-bearing check is OpenSpec's
    invariant (every requirement MUST have ≥1 scenario), plus missing
    titles/bodies and (strict) missing RFC-2119 normative keywords.
  - **Change lifecycle** (schema migration v16, `spec_change` +
    `spec_change_delta`). The `openspec/changes/<name>/` package (proposal/
    design/tasks + ADD/MODIFY/REMOVE/RENAME delta requirements) is modelled and
    driven by new tools: `sync_openspec(openspec_dir=None)` imports an entire
    tree (specs + changes) in one call and reads `openspec.json`;
    `list_spec_changes` / `get_spec_change` inspect proposals;
    `apply_spec_change` folds deltas into the canonical spec set (ADDED/
    MODIFIED/RENAMED upsert+activate, REMOVED deprecate); `archive_spec_change`
    marks a change done.
  - **Scenario-level traceability** (schema migration v17, `scenario_symbol`).
    New `link_scenario_symbol(spec_id, scenario_name, symbol_qname, ...)` links
    code/tests to an individual `#### Scenario:` (not just the whole
    requirement) — OpenSpec reasons per scenario. `get_spec_implementation` now
    returns each scenario's linked `symbols` + a `verified` flag, and
    `coverage.scenarios_verified`. Scenario reconciliation on re-import is an
    upsert (matched by `(spec, name)`) so these links survive a re-sync.
  - **Config:** new `[specs].openspec_dir` re-syncs an OpenSpec tree after every
    `index_project`. **Ergonomics:** `capability` accepted as an alias for
    `module` in `list_specs` and returned by `get_spec_implementation`.
  - **Agent discoverability.** The compatibility is now advertised where an
    agent will find it: a new `openspec_workflow` MCP prompt (sync → trace →
    validate → export loop), an OpenSpec section (§5.8) + tool-tier rows in the
    `agent_playbook`, and a line in the MCP server `instructions`. So a
    consuming agent learns livespec speaks OpenSpec without reading every tool
    docstring.
  - Tool count 36 → 44 (29 core + 12 Spec plugin + 3 docs). New agentic core
    tools: `export_openspec`, `validate_openspec`, `list_spec_changes`,
    `get_spec_change`; new always-visible bootstrap tool `sync_openspec`; new
    Spec-plugin tools `apply_spec_change`, `archive_spec_change`,
    `link_scenario_symbol`. New MCP prompt: `openspec_workflow`.
- **Cross-repo route edges (P2) — the call graph crosses the front↔back
  boundary.** The extractor now records HTTP route "sites" into a new
  `route_ref` table (schema migration v14): `role='server'` for backend
  handlers (`@app.get('/x')`, `@app.route('/x', methods=[...])`) and
  `role='client'` for call sites that hit a route (`fetch('/x')`,
  `axios.get('/x')`, `requests.get('/x')`, `httpx.post('/x')`).
  `_resolve_routes` matches them by a normalized path (every framework's
  param syntax — `<int:id>` / `{id}` / `:id` — and bare numeric segments
  collapse to `{}`) and writes `symbol_edge` rows with
  `edge_type='invokes_route'` (weight 0.9 on an exact method match, 0.8 when
  one side's method is unknown; no edge on a method mismatch). Matching is
  **DB-wide**, so it is cross-repo under a shared `group_db` (P1) and
  intra-repo in a monorepo. Surfaced by `who_calls` (new `route_callers`:
  frontend callers of a backend endpoint) and `who_does_this_call` (new
  `invokes_endpoints`). Answers *"I changed this endpoint — what frontend
  breaks?"* across repos, which neither LSP nor spec-authoring tools can. HTTP
  only for now; the TS/Hono server side and gRPC/queues follow. INSERT OR
  IGNORE / weight-MAX resolver invariant preserved.
- **Cross-project Spec membership via a shared group DB (P1).** A new
  `.livespec.toml` `[workspace] group_db = "..."` key routes several repo
  roots into one database (each keeps its own `project_id`), so a Spec in one
  repo can `link_spec_symbol` / `bulk_link_spec_symbols` a symbol that lives
  in another repo of the group, and `get_spec_implementation` surfaces code
  from every repo. Symbol resolution is home-project-first, then the rest of
  the group. Fully backward-compatible: absent `group_db` → byte-identical
  single-repo behaviour (the DB stays at `<repo>/.mcp-docs/docs.db`, resolution
  stays home-only). Docs/Explorer remain per-repo. First half of the
  polyrepo/microservices pillar; the cross-repo *call graph* (route edges)
  follows.
- **OpenSpec (Fission-AI) interop in `import_specs_from_markdown`.** The tool
  now auto-detects and ingests the OpenSpec markdown dialect
  (`### Requirement: <name>` anchors with SHALL prose + `#### Scenario:`
  WHEN/THEN blocks) alongside the native `## SPEC-NNN:` format. The
  requirement name becomes the spec title, a deterministic slug becomes the
  `spec_id`, and requirements under `## REMOVED Requirements` import as
  `deprecated` (else `active`). A `fmt` parameter (`"auto"` default |
  `"livespec"` | `"openspec"`) forces the dialect, and pointing `path` at an
  `openspec/` **directory** walks its whole `specs/`/`changes/` tree
  (capability = folder name → `module`). Positions livespec as the
  traceability/graph layer beneath spec-driven-development authoring tools.

## [0.20.0] - 2026-07-16

### Breaking — RF → Spec nomenclature + taxonomy (hard cut, no aliases)
- **Taxonomy:** `RF` (Functional Requirement) generalized to `Spec`, a
  broader concept with a `kind` column: `functional_requirement`,
  `non_functional_requirement`, `adr`, `design`, `constraint`, `epic`,
  `other`. RF was too narrow — a `Spec` can now model ADRs, NFRs, and
  other non-functional artifacts alongside functional requirements.
- **Schema (migration v11):** `rf` → `spec`, `rf_symbol` → `spec_symbol`,
  `rf_dependency` → `spec_dependency`, `rf_coverage_snapshot` →
  `spec_coverage_snapshot`; `rf_id` columns → `spec_id`; new `spec.kind`
  column (defaults to `functional_requirement` on migrated rows).
  Existing `RF-NNN` string ids are preserved as-is (not renumbered);
  new specs generate `SPEC-NNN` ids.
- **Annotations:** `@rf:` → `@spec:`, `@not_rf` → `@not_spec`. Markdown
  spec imports now parse `## SPEC-NNN: Title` headings (was `## RF-NNN:`).
- **Tools renamed** (no old-name aliases — calling a dropped name is a
  hard error): `list_requirements`→`list_specs`,
  `get_requirement_implementation`→`get_spec_implementation`,
  `propose_requirements_from_codebase`→`propose_specs_from_codebase`,
  `bulk_link_rf_symbols`→`bulk_link_spec_symbols`,
  `import_requirements_from_markdown`→`import_specs_from_markdown`,
  `create_requirement`→`create_spec`, `update_requirement`→`update_spec`,
  `delete_requirement`→`delete_spec`, `link_rf_symbol`→`link_spec_symbol`,
  `link_rf_dependency`→`link_spec_dependency`,
  `unlink_rf_dependency`→`unlink_spec_dependency`,
  `get_rf_dependency_graph`→`get_spec_dependency_graph`,
  `scan_rf_annotations`→`scan_spec_annotations`,
  `scan_docstrings_for_rf_hints`→`scan_docstrings_for_spec_hints`.
  `analyze_impact(target_type="requirement")` → `target_type="spec"`.
  `generate_docs(target_type="requirement")` → `target_type="spec"`.
- **`list_specs`** gains a `kind` filter alongside `status`/`module`/`priority`.
- **Markdown importer parses `kind`:** `**Kind:** adr` / `**Tipo:** nfr` (Spanish/
  English synonyms for all 7 kinds) sets `spec.kind` on import; defaults to
  `functional_requirement` when omitted, same as before.
- **Explorer bundle surfaces `kind`:** `data.json` specs now carry `kind`; the
  UI renders a compact FR/NFR/ADR/Design/... chip on the spine card and detail
  header, and `kind` is searchable in the spec filter box.
- **Plugin `livespec-rf` renamed to `livespec-spec`.**
- **Config:** `.livespec.toml` `[requirements]` table renamed to `[specs]`
  (`sync_from`, `links_seed` keys unchanged).
- **Resources:** `project://requirements` → `project://specs`,
  `project://requirements/{rf_id}` → `project://specs/{spec_id}`,
  `doc://requirement/{rf_id}` → `doc://spec/{spec_id}`.
- **Prompts renamed:** `audit_requirement_coverage`→`audit_spec_coverage`,
  `extract_requirements_from_module`→`extract_specs_from_module`.
- **Payload keys renamed** across `analyze_impact`, `audit_coverage`,
  `git_diff_impact`, `export_explorer`'s `data.json`, e.g.
  `dependent_requirements`→`dependent_specs`,
  `requirements_touched`→`specs_touched`,
  `rf_coverage`→`spec_coverage`, `requirements`→`specs`,
  `audit_coverage`'s `modules_without_rf`→`modules_without_spec`
  (counts, list, and `next_cursor` key — missed in the initial pass,
  caught during live MCP revalidation after reconnect).
- **Explorer bundle:** rebuilt HTML/JS labels, ids, routes and CSS classes
  (`#rfnav`→`#specnav`, `.spine .rf`→`.spine .spec`, etc.) — re-run
  `export_explorer` (or `index_project(explorer=True)`) to regenerate.
- **Scripts renamed:** `scripts/apply_rf_links.py`→`scripts/apply_spec_links.py`,
  `scripts/sync_livespec_rfs.py`→`scripts/sync_livespec_specs.py`.
  `docs/requirements/livespec-rfs.md`→`livespec-specs.md`,
  `livespec-rf-links.{md,json}`→`livespec-spec-links.{md,json}`.
- No temporary aliases or deprecation shims — single breaking release.
  Migration v11 handles existing databases transparently on next
  `index_project`; callers must update tool/field names in one pass.

### Fixed — packaging & boot (audit batch P1)
- **Package builds again**: the duplicate `force-include` of `templates/`
  (already shipped via `packages`) hard-failed `uv build` / any install
  from sdist or git. The force-include now ships only
  `docs/AGENT_PLAYBOOK.md` into the wheel. CI gained a `ruff + uv build`
  job that verifies wheel contents (schema.sql + playbook) so packaging
  regressions fail fast.
- **`LIVESPEC_PLUGINS` no longer bricks fresh sessions**: with the env var
  set and no cached session workspace, `tools/list` used to construct
  state with `workspace=None` and die with `WorkspaceRequiredError`
  before the override was ever read — which also blocked every
  subsequent `tools/call`. The override is now parsed directly without
  touching state.
- **Unknown `LIVESPEC_PLUGINS` values fall back to DB detection** (with a
  logged warning) instead of silently hiding every plugin on a typo like
  `specs`.
- **`agent_playbook` prompt works on installed (non-checkout) deployments**:
  it now loads the packaged copy via `importlib.resources`, falling back
  to `docs/` in checkouts. Previously site-packages installs always got
  the "playbook file missing" stub.
- **Instrumentation middleware no longer masks workspace errors**: calls
  without a `workspace` used to trigger an unconditional
  `mkdir .mcp-docs-agent-log-fallback` in the server CWD — replacing the
  actionable "workspace is required" error with `Permission denied` on
  read-only CWDs and littering junk dirs on writable ones. Workspace-less
  calls now dispatch untouched; a malformed `.livespec.toml` also no
  longer fails every tool call through the logging-enabled check.
- **CLI**: `--version` flag; `sqlite3.DatabaseError` (corrupted
  `docs.db`) and `PermissionError` (read-only workspace) now exit with
  actionable one-line errors instead of tracebacks.
- **Dependencies**: dropped unused `rank-bm25`; pinned `fastmcp>=3,<4`
  (the middleware imports `fastmcp` internals that a 4.x could move).
- Generic `workspace` parameter examples in every tool schema (the
  author's personal machine paths shipped in the public schema before).
- sdist no longer packages `.coverage`, stray PNGs, or `.playwright-mcp/`.

### Fixed — storage & concurrency (audit batch P2)
- **Migrations are now transactional** (schema migration framework): each
  migration + its `schema_migrations` bookkeeping row commit as one unit.
  A crash mid-`_m011` (the RF→Spec rename) previously left the DB
  half-renamed — with the guard being skip-on-rerun, the populated legacy
  tables were stranded and empty `spec*` shells served every query forever.
- **`UNIQUE(project.root)`** (migration v12): `get_or_create_project` was
  SELECT-then-INSERT with no constraint, so a race (two threads, or the
  MCP server + `livespec-mcp index` CLI in another process) created
  duplicate `project` rows and silently split symbols/specs/coverage
  across two `project_id`s. The migration dedupes existing duplicates
  (repointing child rows), the resolver is now `INSERT OR IGNORE` +
  re-SELECT, and `AppState.project_id` is cached after first resolution.
- **Graph cache no longer serves a half-built graph**: the cache generation
  now keys on the latest *finished* index run with `files_changed > 0`.
  Previously a `load_graph` during an in-flight index could cache partial
  (uncommitted) state under the key that stayed current after the run,
  and every no-op reindex (each watcher tick) needlessly rebuilt the
  ~4s / 183MB graph.
- **Watcher isolation**: `index_project(watch=True)` now reindexes on a
  dedicated WAL connection instead of the shared tool connection —
  removing dirty reads and the "unlocked write joins the indexer
  transaction and gets silently rolled back" hazard. On LRU eviction /
  `reset_state`, a workspace's watcher is stopped before its connection
  is closed (it used to keep firing reindexes against a closed DB).
- **`needs_reextract` survives a failed run**: the forced-re-extract flag
  is now cleared inside the commit transaction only after a successful
  index, instead of up front. A crashed forced run no longer leaves
  migration-added columns (`visibility`, `decorators`) NULL forever.
- **Deletion-only reindex prunes chunks**: `git rm`-ing a file now rebuilds
  chunks so FTS + vector search stop returning hits for deleted files.
- **Index run is atomic**: ref resolution, annotation scan, manual-link
  restore, counts, and the run-finish `UPDATE` now run inside the same
  transaction as the symbol writes (previously autocommit — thousands of
  per-statement WAL commits, and a crash mid-resolve left content hashes
  updated but edges unresolved, which the next incremental run skipped).
- **`PRAGMA busy_timeout = 30000`**: cross-process writers no longer hit
  `database is locked` after 5s during a cold index.
- **schema.sql now defines** `symbol_ref.scope_module`,
  `spec_coverage_snapshot`, and `agent_scratch` so a fresh install and a
  migrated DB converge (they were migration-only before).
- **`chunk_au` FTS trigger** only fires on real text changes
  (`WHEN old.text IS NOT new.text`), ending the FTS write-amplification
  where every `embed_pending` timestamp UPDATE rewrote the chunk in FTS5.
- Guarded the `mtime` `stat()` against a file vanishing mid-index (it was
  outside the read try/except and could abort the whole run).

### Fixed — domain correctness (audit batch P3)
- **Impact traversal is BFS, not DFS** (`descendants_within` and the two
  inline copies in `compute_spec_test_coverage` and the spec dependency
  walk): a node reachable within `max_depth` via a short path is no longer
  dropped because a longer path discovered it first. `who_calls`,
  `who_does_this_call`, `analyze_impact`, `find_orphan_tests`, and coverage
  were under-reporting blast radius / falsely flagging specs untested.
- **Multi-chunk symbols keep all their chunks**: `upsert_chunks` grouped
  the delete-then-insert per source instead of deleting inside the loop —
  previously a symbol that split into N chunks kept only the last one
  (and returned aliased rowids), making large symbols searchable by only
  their final fragment. Unchanged symbols now reuse their rows (preserving
  embeddings) instead of re-inserting.
- **Transient parse errors no longer destroy Spec↔code links**: when a
  Python file fails to parse (saved mid-edit), the indexer preserves its
  existing symbols (and the `spec_symbol` links riding on them) and retries
  on the next index instead of wiping them via cascade.
- **Conditionally-defined Python symbols are extracted**: functions/classes
  under `if TYPE_CHECKING:`, `try/except` import shims, `with`, `for`,
  `while`, and `match` bodies were invisible (missing symbols + false
  dead-code positives on their callers).
- **Chained method calls each record an edge**: `promise.then(h).catch(e)`
  now emits refs for both `then` and `catch` (the old text-split dropped
  every non-first segment of a call chain — pervasive in JS/TS/Java).
- **`.tsx` files get import scoping and exported visibility** (they were
  excluded from the JS/TS import + visibility scans, so every ref in a
  `.tsx` file was unscoped and no React component was ever `exported`).
- **`@spec:` annotation verb boundary**: `@specifically`, `@testsuite`,
  `@seed` no longer create confidence-1.0 Spec links (the verb matched as
  a prefix of a longer word). Negation list gained
  `cannot/can't/won't/isn't/shouldn't/wouldn't`.
- **Nested-def calls attributed once**: a call inside an inner function/
  method is no longer also attributed to every enclosing symbol (a class no
  longer appears to call what its methods call), shrinking edge inflation.
- **Markdown spec importer** ignores `## SPEC-NNN:` headers inside fenced
  code blocks (no more phantom specs) and only treats a line as metadata
  when a key sits at its start (prose like "must show status: active" is no
  longer swallowed and mis-parsed).
- **`body_hash` drifts on whitespace inside string literals** (`"pad "` vs
  `"pad"`) — it was stripping string-content tokens.
- **Vector search over-fetches proportionally** in a multi-project DB so
  the KNN's post-JOIN project filter can't starve results.
- **Edge weights refresh monotonically** (`ON CONFLICT DO UPDATE SET
  weight = MAX(...)`) so a disambiguated edge upgrades from 0.5 to 1.0
  without ever downgrading or deleting (resolver stays INSERT-only).
- **Watcher relevance check is workspace-relative**: a workspace under a
  path segment like `build/` or `.work/` no longer rejects every event and
  silently goes dead. Off-by-one in split-chunk line ranges corrected;
  seeded-link `confidence` is preserved instead of forced to 1.0.

### Fixed — tools layer (audit batch P4)
- **`analyze_impact(target_type="spec")` honors the pagination contract**:
  it returned three FULL unpaginated symbol lists (depth-5 cones unioned
  over every linked and dependent-spec symbol) — the 4-7M-char payload
  class the v0.7 contract exists to prevent. Now paginated
  (`limit`/`cursor`), `summary_only` returns counts, and `min_weight` is
  applied like the symbol/file branches.
- **`git_diff_impact` detects renames**: it now uses
  `git diff --name-status -M` and includes a rename's OLD path (where the
  indexed symbols live), so the caller cone / affected specs / suggested
  tests for a renamed+edited file aren't silently dropped.
- **git ref option injection closed**: `--end-of-options` guards the
  caller-supplied `base_ref`/`head_ref` range so a ref beginning with `-`
  can't be parsed as a git option (e.g. `--output=` → file write).
- **Unbounded `IN (...)` queries chunked**: a depth-5 cone larger than
  SQLite's host-parameter cap (999 / 32766) no longer raises a raw
  `OperationalError`; the seven affected queries run in parameter-safe
  chunks.
- **Workspace errors are shaped**: a missing / non-existent `workspace`
  now returns `{error, isError, hint}` (a new pre-validating middleware)
  instead of a raw protocol error — the most common agent mistake.
- **`create_spec`**: auto-numbering uses MAX(spec_id)+1 (was
  last-inserted, which collided on out-of-order/imported ids), and a
  duplicate id returns a shaped error instead of leaking
  `sqlite3.IntegrityError`.
- **Stable cursor ordering**: `find_orphan_tests` and
  `grep_in_indexed_files` now `ORDER BY` a stable key so a paginated walk
  spanning a re-index can't skip/duplicate rows.
- **Explorer per-Spec `endpoints`** are the intersection of a Spec's
  symbols with the real endpoint surface (the old filter was always true,
  labeling every linked symbol an owned endpoint and inflating
  `dashboard.with_endpoints`). `compute_endpoints` is now computed once
  per bundle build instead of twice.
- **`agent_scratch` notes are readable**: `quick_orient` now surfaces a
  symbol's scratch note (the tool was write-only — nothing ever read the
  notes back).
- **`audit_coverage` stops writing on read**: the coverage trend snapshot
  is recorded only on the primary (first-page, full) fetch, not on
  `summary_only`, cursor pages, or every Explorer bundle rebuild.
- **`list_specs` `has_implementation`** is filtered in SQL before the
  LIMIT (a page could silently shrink to 0 while matching specs existed
  beyond the limit).
- **`find_symbol`** escapes LIKE wildcards (a query with `%`/`_` matches
  literally) and clamps a negative `limit` (was `LIMIT -1` = unbounded).
- **`bulk_link_spec_symbols`** rejects an invalid `relation` (a typo like
  `implement` used to store silently, invisible to every
  `relation='implements'` query) and a non-numeric `confidence` with a
  shaped per-mapping error instead of a raw `ValueError`.

### Performance (audit batch P5)
- **Chunk rebuild preserves embeddings**: `rebuild_chunks` no longer
  `DELETE`s every chunk up front (which reset every `embedded_at` to NULL
  and forced a full re-embed on any edit). It upserts each source — reusing
  a source's rows verbatim when unchanged — then deletes only chunks whose
  source disappeared, and reads each file ONCE (all its symbols share the
  read) instead of once per symbol. Orphaned `chunk_vec_*` rows (vectors for
  deleted chunks) are pruned so the vec index stops growing monotonically.
- **Coverage audit is O(V+E), not per-symbol cones**: the implicit-coverage
  split in `audit_coverage` now runs a single multi-source forward BFS from
  the spec-linked symbols instead of a depth-10 backward cone per symbol per
  orphan file (minutes → sub-second on a spec-adopting Django), and batches
  the file→symbol lookup into one scan instead of a query per file.
- **PageRank cached per graph**: `quick_orient`, `get_project_overview`, and
  `propose_specs_from_codebase` share a memoized PageRank on the (already
  run-cached) `GraphView` — it was recomputed on every call (measured 5.4s
  on Django's 465K-edge graph).
- **`find_dead_code` parses each file once**: the five Python dead-code scan
  helpers now share one `(path, mtime)`-keyed AST-parse cache (was ~5 parses
  per file, ~11K per call on Django) and re-parse after an edit + re-index
  (the old path-only cache went stale).
- **sqlite-vec loads once per connection** instead of on every `vec_search`
  and lanes-payload call.

## [0.19.0] - 2026-06-30

### Changed — brownfield RF bootstrap
- **`import_requirements_from_markdown`** always visible in core menu (no
  chicken-and-egg: RF rows no longer required to see the import tool).
- **`[requirements].sync_from`** + **`links_seed`** in `.livespec.toml` — post-
  `index_project` hook re-imports markdown specs and optional
  `bulk_link_rf_symbols` seed (`domain/requirements_sync.py`).
- **`scripts/sync_livespec_rfs.py`** — run sync without full re-index.
- **`find_endpoints`**: pytest fixtures and `tests/**` scaffolds excluded from
  default sweep (`framework='pytest'` still lists fixtures).
- **`import_requirements_from_markdown`**: warns on duplicate `## RF-*` headings
  across different markdown paths.
- **`bulk_link_rf_symbols`**: actionable `hint` when qname looks like a test
  module (`tests.pkg.mod`) instead of a test function.
- **`embed_chunks`**: install hint (`uv pip install -e ".[embeddings]"` + MCP
  reconnect) when extras missing.
- **Explorer**: `coverage_source === 'derived'` shows **integration-only** badge.
- **`agent_scratch_clear(qname=...)`** — clear one note; omit to clear all.
- **Resource URIs** documented in `resources.py` (`project://`, `doc://`,
  `code://`; no `livespec://` alias).

### Added — FastAPI onboarding installer
- **`livespec-mcp fastapi init [path]`** — index + Explorer bundle + autowire +
  installs `.cursor/rules/livespec-fastapi.mdc`, `.cursor/skills/livespec-fastapi/`,
  and `.livespec/SESSION_PROMPT.md` from shipped templates.
- Templates live under `src/livespec_mcp/templates/fastapi/`.

### Added — FastAPI HTTP routes in Explorer
- **Python HTTP route extraction** (`domain/extractors.py`): AST parse of
  `@app.get("/path")`, `@router.post(...)`, `api_route`, Flask `route` →
  `http_method` / `http_path` on `compute_endpoints` entries (Explorer + MCP).
- **Explorer HTTP Try-it**: base URL input + **Execute** (`fetch`) for
  GET/POST/PUT/PATCH/DELETE when `method` + `path` are known; CORS note in UI.
- **`livespec_mcp.explorer.fastapi`**: `enable_explorer(app)`,
  `explorer_lifespan`, `LivespecExplorerMiddleware` — mount at runtime without
  patching `main.py`.
- **Autowire** emits `mount_explorer(app, prefix="<mount_path>")` from
  `[explorer].mount_path` in `.livespec.toml`.

### Added — FastAPI explorer autodetect
- **`index_project` FastAPI autodetect**: `_should_build_explorer` builds the
  RF Explorer bundle on first index when `find_fastapi_entrypoints` finds
  `app = FastAPI(...)` in `main.py` / `app.py` (in addition to
  `explorer=True` or an existing `.mcp-docs/explorer/` bundle).

### Added — agent tooling
- **`grep_in_indexed_files`**: pattern search limited to indexed workspace
  files (paginated).
- **`agent_scratch` / `agent_scratch_clear`**: per-project agent notes in SQLite
  (schema migration v10).
- **Optional agent call log**: `[agent] log_calls = true` in `.livespec.toml`
  → `.mcp-docs/agent_log.jsonl` (`instrumentation.py`).
- **`payload_warning`** on `who_calls`, `analyze_impact`, `git_diff_impact`
  when estimated payload is large and `summary_only` was not used.

### Changed — tool surface
- **`export_explorer`** promoted to always-visible core (not gated by docs
  plugin). Default core menu **24** tools (+ plugins when workspace qualifies).

### Added — CI PR RF impact comment
- **`.github/workflows/livespec-pr-comment.yml`**: on `pull_request`, runs
  `scripts/pr_diff_impact.py` (indexes + `compute_diff_rf_impact`) and posts
  a markdown table of touched RFs via `gh pr comment` when `GITHUB_TOKEN` is
  available; skips posting gracefully when the token is absent.

### Changed — Explorer Overview trend UX
- **Overview landing** shows a compact coverage trend (SVG sparkline + last-3
  snapshot table for `avg_test_coverage`) when `DATA.trend` has snapshots;
  the Changes tab keeps the full bar strip.

### Added — docs
- **README** "FastAPI integration" section (install → index → `/explorer` +
  Try-it HTTP).

## [0.18.0] - 2026-07-01

### Added — per-workspace plugin menu
- **`PluginVisibilityMiddleware`**: RF mutation (10) and docs plugin (4) tools
  stay registered at boot but are **hidden from `tools/list`** and **blocked on
  `tools/call`** until the workspace has `rf`/`doc` rows (or an explorer bundle
  on disk for docs) or ``LIVESPEC_PLUGINS`` includes the plugin. Session caches
  the last ``workspace=`` from tool calls so the menu updates after the first index.
- **`livespec-mcp explorer serve [path]`**: local preview at
  `http://127.0.0.1:8765/explorer/` (`/` and `/index.html` redirect there).
- **`src/livespec_mcp/explorer/`** package: `mount_explorer`, `serve_explorer`,
  `create_explorer_host_app`, FastAPI autowire (`autowire.py`).

### Changed — RF Explorer UX
- **FastAPI `/explorer` mount**: `from livespec_mcp.explorer import mount_explorer;
  mount_explorer(app)` serves the static bundle at `/explorer` (SPA fallback for
  sub-routes). On `export_explorer` / `index_project(explorer=True)`, livespec
  **auto-wires** `mount_explorer(app)` into the first ``app = FastAPI(...)``
  module (`main.py` / `app.py`) when `[explorer] auto_mount = true` (default).
- **Landing at `/explorer`**: client-side router + Overview home; API tab uses
  Swagger-style collapsible operations. Hash routing when the bundle is opened
  from `file://` or a plain static server at bundle root (no `/explorer` prefix
  in the URL path).

### Fixed — search
- Vector lane falls back to FTS-only when the embedder is offline.
- Snake_case queries (`index_project`) tokenize correctly for FTS5 (`index OR project`).

## [0.17.0] - 2026-06-25

### Added — Diff→RF "Changes" view
- `export_explorer(base?, head?)` bakes a **Changes** section: which RFs a
  diff touches + their test coverage, via a new
  `compute_diff_rf_impact(st, base, head)` (reuses the existing
  `git_diff_impact` walk — factored into a shared `_git_diff_changed_files`
  helper). Default range: `main..HEAD`, falling back to `HEAD~1..HEAD`;
  omitted when not a git repo. The PO/reviewer's "what does this branch
  touch" view.

### Added — coverage drill-down (uncovered symbols)
- `audit_coverage`'s per-RF `rf_coverage` now includes `uncovered_symbols`
  (+ `uncovered_symbols_count`): the implementing symbols with no test
  (derived or explicit), capped at 50. The explorer renders it as a
  collapsible "Uncovered symbols" block per RF — actionable "write a test
  for these".

### Added — coverage trend over time
- New `rf_coverage_snapshot` table (migration v9) + `storage/trends.py`
  (`record_snapshot` / `read_trend`). `audit_coverage` records a snapshot,
  **deduped on change** (an identical avg + verified_count since the last
  snapshot is a no-op, so repeated explorer exports don't spam the series).
  The explorer shows a coverage sparkline + per-snapshot strip (`trend` in
  data.json).

### Added — explorer freshness on index
- `index_project(explorer=False)` auto-regenerates the explorer bundle when
  `.mcp-docs/explorer/` already exists (or `explorer=True`), best-effort (a
  failure never breaks indexing). Payload gains `explorer_regenerated`.
  Keeps the static page current without a manual `export_explorer`.

### Added — reproducible RF links
- `docs/requirements/livespec-rf-links.json` (the implements/tests links) +
  `scripts/apply_rf_links.py` make the self-RF *links* reproducible from a
  clone, completing the RF-definition reproducibility from v0.16:
  `index_project` → `import_requirements_from_markdown` →
  `python scripts/apply_rf_links.py`.

## [0.16.0] - 2026-06-25

### Added — automatic RF test coverage (call-graph derived)
- `audit_coverage` now **derives** per-RF test coverage from the call
  graph: a requirement's implementing symbol counts as tested when a
  test symbol's forward call-cone reaches it (bounded depth 3), unioned
  with explicit `relation='tests'` links. New additive payload — a
  per-RF `rf_coverage` list `{rf_id, title, test_coverage_ratio,
  tested_symbols, total_symbols, coverage_source}` (`coverage_source` ∈
  derived / explicit / both / none) plus rollups `avg_test_coverage`
  and `rfs_with_any_test_coverage`. The legacy explicit-link fields
  (`rf_test_coverage`, `rfs_with_test_coverage`) are unchanged. RF
  test-coverage now works automatically for any project whose tests
  call the code directly — no hand-linking required; explicit links
  remain the fallback for in-process MCP/RPC suites the call graph
  cannot follow.

### Changed — RF Explorer surfaces real test coverage
- The explorer's per-RF view shows a **Test coverage %** meter (distinct
  from the existing **Link confidence %** meter) with a `coverage_source`
  badge ("auto-derived (call graph)" vs "explicit test links"); the
  dashboard gains an **Avg test coverage** KPI; `dev_state` flips to
  `verified` whenever an RF has any test coverage. Computed from a single
  `compute_coverage` call (no extra graph load).

### Fixed — RF Explorer polish (Mermaid render + endpoints)
- **Dependencies tab no longer errors.** Mermaid v10 rejects CSS
  `var(...)` inside `classDef`; the topology emitted
  `classDef … fill:var(--mm-*)` → "Syntax error in text". Colours are now
  resolved to concrete values at build time (light/dark still tracked),
  and node/edge labels are HTML-entity-escaped (`& < > "`) so titles like
  "Dead-code & coverage" / "RF↔code" render.
- **Endpoints tab cleaned up + grouped.** `pytest.fixture` entries (test
  infrastructure, not API surface) are split into a separate collapsed
  list and excluded from the endpoint count (57 → 50: 33 tools + 9
  resources + 8 prompts). The tab groups by kind (Tools / Resources /
  Prompts) Swagger-style with per-endpoint signature + RF chips, a
  **copy-call** snippet per endpoint (MCP tool-call skeleton; static spec
  view — no live execution without a server), and a note saying so.

### Added — reproducible self-RFs
- `docs/requirements/livespec-rfs.md` ships livespec's own 12 requirements
  (RF-001..RF-012) in the `import_requirements_from_markdown` format, so a
  fresh clone can regenerate the RF definitions deterministically
  (`index_project` → `import_requirements_from_markdown`).
  `docs/requirements/livespec-rf-links.md` documents the symbol-link seed
  honestly (links are bulk-seeded / `@rf:`-annotatable, not committed
  per-symbol).

## [0.15.0] - 2026-06-25

### Added — RF Explorer (`export_explorer`): auto-generated web view
- New **`export_explorer`** tool (in the `livespec-docs` plugin) writes
  a self-contained static bundle to `.mcp-docs/explorer/`: `data.json`
  (the machine-readable "spec") + `index.html` (a zero-dependency,
  no-server viewer opened straight from `file://`). It is
  Swagger-for-your-codebase but **organised by Requirement** — an RF
  spine with each RF's implementing symbols (+ signatures), owned
  endpoints, RF→RF dependency topology (Mermaid), a framework-aware
  endpoint lens, and a coverage-gaps view. Pure projection of the RF
  graph: re-run to refresh, no daemon, local-first preserved. Tool
  count 32 → 33. The viewer is light/dark, keyboard-navigable, and
  inlines its data so it works fully offline (except the Mermaid CDN,
  which degrades gracefully).
- **Stakeholder / Product-Owner framing.** The view leads with a
  project **status dashboard** (counts per development state, %
  implemented, # verified, # exposing endpoints) and a per-RF
  **`dev_state` derived from code evidence** — `not_started` (no
  implementing symbols) → `in_progress` → `implemented` → `verified`
  (has test-relation links) — rather than a hand-maintained field.
  Each RF leads with its plain-language description and state; the
  symbol/signature table is collapsed under "Technical detail". The
  declared `status` is shown alongside and **flagged when stale**
  (e.g. declared "draft" but the code shows it implemented). The
  per-RF `coverage` number is labeled honestly as **link confidence**
  (mean confidence of the RF↔symbol links), never as test coverage.

### Added — `find_symbol` typo suggestions
- `find_symbol` with zero matches now includes a `did_you_mean` list
  (same suggester as the not-found errors) instead of a bare empty
  result — found dogfooding v0.14.0 through the the workflow runner proxy: a typoed
  query left the agent at a dead end. Key absent when there are
  matches or no close candidates.

### Fixed — `find_endpoints` now detects plugin-registered tools
- `find_endpoints` missed tools registered through the `livespec-rf`
  plugin (decorated via `mutation_tool`/`agentic_tool` aliases of
  `mcp.tool`). It now unions the decorator-alias set — the same
  mechanism `find_dead_code` already used — so the full tool surface
  is reported, not just direct `@mcp.tool` decorations.

### Changed — `propose_requirements_from_codebase` default depth 2 → 3
- The default `module_depth` was 2, which collapsed deep packages
  (e.g. an entire `src/<pkg>` subtree, hundreds of symbols) into one
  giant, auto-mislabeled RF. The default is now **3** for sub-module
  granularity; the parameter remains fully overridable.

### Added — `find_orphan_tests` caveat for in-process MCP suites
- The payload now carries a `caveat` field flagging that the count is
  an upper bound: tests that exercise code through FastMCP
  `Client(mcp)` indirection reach zero production symbols in the
  static call graph and therefore look orphaned when they are not.

### Fixed — package imports cleanly from source
- `livespec_mcp.__version__` no longer raises when the distribution
  metadata is absent (running straight from `src/`); it falls back to
  `0.0.0+source`. `pytest` config gained `pythonpath = ["src"]` so the
  suite is runnable without an editable install (CI/from-source).

## [0.14.0] - 2026-06-12

Personal-fit sprint (multi-repo friction) + v0.13 framework sprint —
v0.13 was never tagged, so this release ships both batches.

### Fixed — embedding model cache survives reboots
- fastembed's default cache is `$TMPDIR/fastembed_cache` — wiped on
  every reboot on tmpfs systems, silently re-downloading ~200MB of
  model weights. Models now cache in
  **`~/.cache/livespec-mcp/fastembed`** (XDG-aware, shared across all
  workspaces). An explicit `FASTEMBED_CACHE_PATH` env var still wins.
  The unused per-workspace `Settings.models_dir` field was removed —
  per-workspace model copies were never the right design.

### Added — index-freshness recipe for Claude Code
- README documents a `PostToolUse` hook that runs
  `livespec-mcp index "$CLAUDE_PROJECT_DIR"` in the background after
  every Edit/Write — incremental hash-skip makes no-op runs cheap.
  This is the recommended freshness mechanism for single-user
  sessions; the watcher remains for multi-agent-free environments.

### Added — closure-capture detection for TS/JS/TSX + Rust
- **`find_dead_code(include_non_python=True)` no longer flags nested
  named functions referenced in their parent's body** — the
  `new Watcher(onEvent)` / `let cb: fn() -> i32 = on_event;` callback
  pattern. Port of the Python v0.8 fix #11 scan to tree-sitter; open
  since v0.8. Go is exempt by design (no named nested functions exist —
  closures are anonymous and never become symbols).

### Fixed — resources broken under multi-tenant (since v0.12)
- **Every `project://` / `doc://` / `code://` resource raised
  `WorkspaceRequiredError` in real use** since v0.12 made `workspace`
  per-call (resources have no parameter channel; the in-repo test
  fixture masked it). Resources now bind to the **most recently used
  workspace** (whatever the last tool call touched) — correct by
  construction for single-repo sessions. Before any tool call, JSON
  resources return an `mcp_error`-shaped payload with a hint; text
  resources a one-line explanation.
- **Resource error payloads now use the `mcp_error` shape**
  (`{error, isError, hint?}`) instead of ad-hoc `{"error": ...}` JSON —
  closes the v0.6 P4 contract gap for the resource surface.

### Changed — explicit unsupported-language reporting
- Files whose extension maps to a language **without an extractor**
  (c, cpp, c_sharp, kotlin, swift, scala) are no longer indexed as
  silent zero-symbol rows. They're skipped at walk time and counted in
  a new `languages_unsupported` payload key on `index_project`
  (`{"cpp": 12, ...}`), so agents see the coverage gap instead of
  inferring it. `.livespec.toml [index].languages` now validates
  against extractor-supported labels only.
- `index_project(force=True)` restores manual RF links with one
  set-based `INSERT..SELECT` instead of two queries per link.

### Fixed — watcher reindex failures now logged
- The debounced watcher worker swallowed every reindex exception
  (`except Exception: pass`). Still survives bad runs, but now logs the
  traceback under the `livespec.watcher` logger.

### Added — headless CLI subcommands
- **`livespec-mcp index <path> [--force] [--embed]`** and
  **`livespec-mcp status <path>`** run the exact same pipeline as the
  `index_project` tool / `project://index/status` resource and print
  JSON to stdout — index from cron, systemd timers, pre-commit hooks or
  CI without an MCP host in the middle. Errors go to stderr with exit
  code 1. `livespec-mcp` with no arguments (and the explicit
  `livespec-mcp serve`) remains the stdio MCP server, so existing
  mcp.json entries are untouched.

### Added — `.livespec.toml` per-repo config
- **Optional `.livespec.toml` at the workspace root** tunes indexing
  per-repo, loaded fresh on every `index_project` call (no restart).
  `[index]` table: `ignore` (gitignore-syntax patterns that **outrank**
  `.gitignore`, negations included), `languages` (extractor-label
  allow-list, e.g. `["python", "typescript"]`), `max_file_bytes`
  (default 2 MB). Malformed config fails the call with an actionable
  message instead of being silently ignored. `index_project` payload
  gains a `repo_config` echo key (`null` when no file). New dependency:
  `tomli>=2` on Python < 3.11 only (3.11+ uses stdlib `tomllib`).

### Added — gitignore-aware indexing
- **`index_project` now honours `.gitignore`** (root and nested files,
  `!negation` patterns included) via the `pathspec` library, layered on
  top of the existing hardcoded `DEFAULT_IGNORES` baseline. Git
  precedence semantics: the deepest `.gitignore` with an opinion wins;
  ignored directories are pruned (never descended), so re-includes
  inside an ignored directory don't apply — same limitation git
  documents. Workspaces without any `.gitignore` behave exactly as
  before. New dependency: `pathspec>=0.12`.

### Added — Hono framework support (call-style routing)
- **`find_endpoints(framework='hono')`**: scans indexed TS/JS files whose
  source mentions Hono for `app.get('/users', handler)` /
  `app.on('PURGE', '/cache', h)` / `app.route('/api', sub)` patterns.
  Each entry reports `hono_method` + `hono_path`; named handlers resolve
  to their symbol (`qualified_name`), inline arrows fall back to a
  `file:line` pseudo-qname. Opt-in only (reads files on demand, not part
  of the `framework=None` sweep).
- **Named-handler protection**: identifiers passed to registration-style
  calls (`get/post/put/delete/patch/options/all/route/use/on/once/
  subscribe/register/connect/addEventListener/addListener`) now emit
  `callback_arg` refs at extract time (inside symbol bodies → `who_calls`
  answers), and `find_dead_code(include_non_python=True)` runs a TS/JS
  module-level registration scan mirroring the Python
  `_runtime_registered_names` — the canonical Hono/Express pattern
  registers handlers at module top-level where no symbol owns the call.
  Also benefits Express-style apps and event emitters.

### Added — Spring Boot + Angular framework support
- **`find_endpoints(framework='spring')`**: surfaces `@RestController` /
  `@Controller` classes and `@GetMapping` / `@PostMapping` /
  `@PutMapping` / `@DeleteMapping` / `@PatchMapping` / `@RequestMapping` /
  `@ExceptionHandler` / `@EventListener` / `@Scheduled` methods.
- **`find_endpoints(framework='angular')`**: surfaces `@Component` /
  `@Injectable` / `@Directive` / `@Pipe` / `@NgModule` classes.
- **`find_dead_code` Spring/Angular awareness**: Spring stereotype +
  mapping annotations count as entry points (DI container invokes them).
  Angular template-bound classes (Component/Directive/Pipe) protect ALL
  their methods — HTML templates are invisible to the indexer
  (`(click)="save()"`); lifecycle hooks (`ngOnInit` & friends) are
  protected on any Angular-decorated class.

### Added — TS decorator + Java annotation extraction
- **`symbol.decorators` now populates for TS/JS/TSX and Java**, not just
  Python. TS/JS: `decorator` nodes on the declaration, on a wrapping
  `export_statement` (`@Component() export class Foo`), or as preceding
  siblings of class members (`@HostListener() onResize()`). Java:
  `annotation` / `marker_annotation` inside `modifiers`
  (`@RestController`, `@GetMapping("/x")`, dotted forms preserved).
  Foundation for Angular / Spring Boot framework detection.
- **Schema migration v8** (`ts_java_decorators_reextract`): no schema
  change; queues `needs_reextract` so existing DBs backfill decorators on
  the next `index_project` without `force=True`.

### Fixed — dual-decorator alias false positives in `find_dead_code`
- **Decorator aliases now recognized as entry points.** The plugin-framework
  pattern `agentic_tool = mcp.tool if X else _noop_decorator` hid the real
  decorator from the entry-point matcher — the stored decorator name is the
  alias, whose last segment is not in the known set. New cached per-file AST
  scan (`_entry_point_decorator_aliases`) collects assignment targets whose
  value (directly or through either branch of a conditional expression)
  resolves to an entry-point dotted name; works for assignments inside
  function bodies (`register()` pattern). Both IfExp branch names are also
  protected (`_noop_decorator` was only referenced inside the expression).
  Wire-validated: `find_dead_code` on livespec-mcp itself **22 → 0**.
- **Genuinely dead code removed** instead of suppressed: `graph.subgraph_edges`
  (orphaned by the v0.8 `get_call_graph` drop) and the watcher registry
  helpers `get_watcher` / `unregister_watcher` / `all_watchers` (orphaned by
  the v0.8 watcher-tool drops; `register_watcher` + `stop_all_watchers`
  remain — they serve `index_project(watch=True)` and atexit cleanup).

## [0.12.0] — 2026-05-29

### Added — multi-repo workspace support (require workspace on every call)
- **`workspace` is now required on every tool call.** No more
  `LIVESPEC_WORKSPACE` env var dependency or per-session state.
  Pass the absolute repo root on every invocation; the LRU cache
  (`get_state`) makes repeated calls to the same workspace cheap.
  One MCP server instance can now serve multiple repos concurrently
  by varying only the `workspace` argument — no restart required.
- **`index_project` docstring fixed** (B021). The docstring was an
  `f"""..."""` expression — Python treats it as a runtime expression,
  not `__doc__`, so MCP clients saw `None` as the tool description.
  Fixed by restoring a plain docstring and appending
  `WORKSPACE_DOCSTRING_NOTE` via `index_project.__doc__ =` after the
  function definition. Closes the "None description" report for the
  flagship tool.

### Fixed — JSDoc banner-with-text + manual-links data loss on force=True
- **`_is_separator_only` extended** to skip banner-style line comments
  with internal text wrapped in ≥2 separator chars (`// --- Token
  Management ---`, `// ============= Tool Execution Dispatcher
  =============`). Previously only pure separator lines (`// ---`)
  were dropped, so banner sections still won `docstring_lead` over
  the JSDoc immediately below. Locked in by
  `test_ts_jsdoc_skips_banner_with_internal_text`.
- **Data-loss fix on `index_project(force=True)`**: re-extraction
  cascade-deleted every `rf_symbol` row through the `symbol` FK,
  silently wiping links created by `bulk_link_rf_symbols` /
  `link_rf_symbol`. The indexer now snapshots non-annotation
  rf_symbol rows before re-extract and restores them by symbol qname
  after the new symbols are inserted. `index_project` payload gains
  `manual_links_restored`. Annotation-sourced links (`source =
  'annotation'`) intentionally NOT snapshotted — they are re-derived
  from fresh docstrings by `scan_annotations`, so preserving them
  would shadow legitimate edits to `@rf:` tags. Locked in by
  `test_manual_links_survive_force_reindex`.

### Added — JS/TS JSDoc support + agentic bulk-link + extractor-aware audit
- **JSDoc annotations now extracted on JS/TS.** `_ts_extract` reads the
  comment(s) immediately preceding a declaration (walking through wrapping
  `export` / `export default` statements) and stores the cleaned text as
  the symbol's `docstring`. Both `/** ... */` block comments and runs of
  `//` line comments are accepted, so any `@rf:RF-NNN` sitting above a
  TS/JS function/class/method now wires up automatically through the
  existing `scan_annotations` matcher — Python parity for the
  Deno/Node/React/Vue/Svelte stacks. Each comment is stripped
  individually before joining (so a `/** @rf:... */` adjacent to `//`
  line comments still has its tag at line-start where the matcher
  anchors), and pure ASCII separator lines (`// ---`, `// ===`) are
  dropped so they don't crowd out the meaningful JSDoc as
  `docstring_lead`. Locked in by `test_ts_jsdoc_docstring_populated`
  and `test_ts_jsdoc_wins_over_adjacent_separator_line_comment`.
- **`bulk_link_rf_symbols` promoted to the agentic surface.** Previously
  only registered by the `livespec-rf` plugin (which loads on DB-state
  signal), it is now part of the default tool tier so agents always
  have an escape hatch for languages or file types where the in-source
  annotation extractor can't reach (configs, SQL, YAML, languages
  without docstring extraction yet). Plugin no longer re-registers it.
- **`audit_coverage` distinguishes extractor gaps from real orphans.**
  New `modules_unsupported_language` bucket lists files whose language
  has no annotation extractor today (everything outside Python / JS /
  TS / TSX). They are removed from `modules_truly_orphan` so the
  actionable list is no longer drowned by false positives — the gap is
  in the extractor, not the project.

### Added — v0.12 P1 RAG layer wired end-to-end
- **`search` MCP tool** (new, default surface). Hybrid retrieval over
  AST-aware chunked symbols + RFs. FTS5 lane always live; vector lane
  fuses via Reciprocal Rank Fusion (k=60) when `[embeddings]` extra is
  installed and chunks are embedded. Reverses the v0.8 drop: this
  iteration the tool is wired to a real chunk pipeline and locked in
  by 9 tests, not the orphan stub from v0.7.
- **`embed_chunks` MCP tool** (new). Populates `chunk_vec_code` /
  `chunk_vec_text` vec0 tables for any chunks missing embeddings.
  No-ops cleanly when extras aren't installed (returns `mcp_error`
  with install hint).
- **`index_project` payload** gains `{chunks, embeddings}` fields and
  a new `embed=False` flag. Rebuilds chunks idempotently after the
  symbol/edge pass; skipped when no files changed and a chunk set
  already exists. With `embed=True`, also runs `embed_pending` so
  vector lane activates without a separate call.
- **9 tests** locking the contract: 6 default (chunk population, FTS
  keyword hit, scope filter, error shape, no-vec without embed,
  rebuild skip on no-change) + 3 `@pytest.mark.embeddings` (vec0
  populated, hybrid query lights up `lanes.vector`, `embed_chunks`
  idempotent). Suite total **243 default + 3 embeddings = 246**.
- **`.gitignore`** hardened for personal + ML artefacts: `.claude/`
  whole, model weight extensions (`.onnx|safetensors|gguf|bin|pt`),
  numpy dumps, fastembed/HF cache dirs, debug dumps, editor/OS files.

No schema migration (`chunk` + `chunk_fts` + `embedded_at` already
existed). No new dependencies (`fastembed` + `sqlite-vec` remain
opt-in via `pip install -e ".[embeddings]"`).

## [0.11.0] — 2026-05-01

The "TS framework readiness" release. Closes session-05 bugs #18-#20
(Deno Fresh / TS over-reporting) plus the last big bucket of Django
runtime-registration false-positives. Every win lands behind the same
default surface — no new tools, no breaking changes.

**Wire-validation against `SpeedRunners-landing` (217 files / 2532 symbols / 16567 edges, Deno Fresh + TS + TSX):**

| Tool | v0.10 | v0.11 | Delta |
|---|---:|---:|---:|
| `find_dead_code` (default) | 974 | **0** | −100% |
| `find_dead_code` (`include_non_python=True`) | 974 | **118** | −88% |
| `find_endpoints(framework="fresh")` | 0 (n/a) | **340** | new tool branch |
| `top_symbols` from `_fresh/` or `dist/` | 18/20 | **0/20** | clean |

The default-mode 974 → 0 reflects the combined effect of P0 (bundler dirs filtered), P1 (islands/routes recognised as entry points) and P2 (JSX edges connect islands to their renderers). The non-Python opt-in still drops 88% (974 → 118) because the bundler filter alone removes most of the noise even when entry-point heuristics are bypassed by `include_non_python=True`.

### Added — v0.11 P0 bundler/build output dir filter
- New module-level helper `_is_bundler_output_path(path)` recognises
  generated artefact dirs (`_fresh/`, `dist/`, `build/`, `.next/`,
  `out/`, `node_modules/`, `.svelte-kit/`, `target/`, `__pycache__/`,
  `.turbo/`, `.vite/`, `.cache/`, `.parcel-cache/`) plus minified
  artefacts (`*.min.js`, `*.min.mjs`, `*.min.css`, `*.bundle.js`).
- Applied in `find_dead_code` (skips bundler-generated symbols from
  the dead-code report) and `compute_project_overview` (filters
  `top_symbols` so generated noise no longer dominates project
  overview). Closes bug #18 surfaced by session 05 (Deno Fresh / TS).
- Tests: `tests/test_bundler_filter.py` covers the helper plus
  end-to-end behaviour for `find_dead_code` and `get_project_overview`.

### Added — v0.11 P1 TS framework entry-point detection
- New helpers `_ts_framework_entry_point_kind(path)` and
  `_is_ts_framework_entry_point(meta)` detect TS framework
  filesystem-routing files: Fresh `islands/`, Next.js `pages/` (pages
  router) and `app/{page,layout,loading,...}.tsx` (app router), SvelteKit
  `routes/+{page,layout,server,error}.*`, and Remix `app/routes/`.
- `find_dead_code`: symbols in those files are skipped from the dead-code
  report (guarded by `include_infrastructure=False` default). Closes
  bug #19 surfaced by session 05 (Deno Fresh / TS over-reporting).
- `find_endpoints`: extended `framework` literal to accept `"nextjs"`,
  `"fresh"`, `"sveltekit"`, `"remix"`. These frameworks are surfaced
  via a path-based scan (no decorators needed), returning symbols with a
  `ts_framework` field (`"fresh"`, `"nextjs_pages"`, `"nextjs_app"`,
  `"sveltekit"`, `"remix"`).
- Tests: `tests/test_ts_framework_entry_points.py` — 24 unit tests on
  path-matching helpers + 8 MCP integration tests (find_dead_code
  suppression + find_endpoints surfacing). 32 new tests total.

### Added — v0.11 P2 JSX element refs as call-graph edges
- `_ts_collect_calls` in `domain/extractors.py` now walks
  `jsx_opening_element` and `jsx_self_closing_element` nodes inside TSX
  function/component bodies and emits an `ExtractedRef` (kind `"jsx"`) for
  each component-typed JSX usage. Uppercase identifiers (`<Counter />`) and
  member-expression leftmost segments (`<Form.Field />` → `Form`) become
  ref targets; lowercase HTML elements (`<div>`, `<span>`) are skipped.
- The resolver's existing `_resolve_refs` path handles JSX refs identically
  to call refs — no schema change required.
- `find_dead_code` no longer over-reports React/Preact components that are
  only used as JSX elements (edge exists → not flagged as dead). Closes
  bug #20 surfaced by session 05 (Deno Fresh / TSX).
- Tests: `tests/test_tsx_jsx_refs.py` — 10 cases covering extractor-level
  ref emission (self-closing, paired, member-expression, HTML skip,
  multiple components) and integration-level call-graph edges + dead-code
  integration win.

### Added — v0.11 P3 runtime-registration name protection
- New `_runtime_registered_names(file_path_abs)` helper walks ALL function/
  method/class bodies for method calls whose attribute name is in a
  conservative set of registration verbs: `register`, `register_lookup`,
  `register_function`, `register_view`, `register_filter`, `register_tag`,
  `register_serializer`, `register_admin`, `connect`, `add_handler`,
  `subscribe`, `add_middleware`, `add_listener`, `on`, `use`.  Collects
  positional `Name` args and keyword `Name` values; ignores string args and
  lambdas to keep false-skip risk minimal.
- Wired into `find_dead_code`'s per-file loop alongside
  `_module_level_referenced_names` and `_publicly_exported_names`. Closes
  the last major bucket of Django false-positives from runtime registrations
  like `Field.register_lookup(MyLookup)` in `AppConfig.ready()`,
  `pre_save.connect(my_handler)` at module level, and
  `app.add_middleware(MyMiddleware)` in setup functions.
- Tests: `tests/test_runtime_registration.py` (13 cases: helper unit tests
  for all patterns + negative cases for non-registration verbs and string
  args + end-to-end `find_dead_code` integration tests).

## [0.10.0] — 2026-05-01

The "library codebase" release. v0.9 dropped Django `find_dead_code`
824 → 514 (−38%). v0.10 drops it further to **348** (−32% additional,
**−58% cumulative from v0.8**). Plus the language-coverage closeout:
session 05 against a Deno Fresh app validates the agentic flow on
TypeScript / TSX / JS, locking 5 profiles into the tier signal.

| Tool on Django (40K symbols) | v0.8 | v0.9 | v0.10 | Cumulative |
|---|---:|---:|---:|---:|
| `find_dead_code` count | 824 | 514 | **348** | **−58%** |
| `find_dead_code` classes | 293 | 251 | 164 | −44% |
| `find_dead_code` methods | 81 | 74 | 24 | −70% |
| `find_dead_code` functions | 450 | 189 | 160 | −64% |

### Added — v0.10 P1 publicly-exported names protect from dead-code
- New `_publicly_exported_names(file_path_abs)` walks each .py file's
  top-level for two patterns and adds them to `find_dead_code`'s
  `global_module_refs`:
  - **`from .impl import Foo, Bar as Baz`** in any module — the
    imported names (and their aliases) are recorded. Critical for
    library `__init__.py` re-exports: `django/contrib/auth/__init__.py`
    re-exporting `authenticate`, `Argon2PasswordHasher`, etc.
  - **`__all__ = ['Foo', 'Bar']`** module-level list/tuple — each
    string's trailing identifier is recorded.
  - `import x.y as z` recognized: bound name (or head segment for
    bare `import x.y`) recorded.
- Closes the largest remaining false-positive bucket on Django
  (~166 of the v0.9 514 candidates).

### Added — v0.10 P0 README lift
- v0.9 Django wins lifted above-the-fold to a four-row pull-out
  table (`find_dead_code`, `find_endpoints(django)`, `quick_orient`
  p95, partial reindex).
- New "30-second tour" section under the headline shows the agentic
  flow as runnable code with realistic JSON output sourced from
  Django session 04 logs.
- `docs/AGENT_QUICKSTART.md` now linked as a callout — existed
  since v0.8 P4 but was never surfaced.
- Plugin auto-detect framing tightened: "fresh repos get a 16-tool
  surface, RF-active repos get 27, with no config".

### Added — v0.10 P2 language coverage closeout (session 05)
- Battle-test session 05 against `SpeedRunners-landing` (Deno Fresh
  app, 217 files / 2532 symbols / 16,525 edges across TypeScript +
  TSX + JS). Validates the agentic flow on the most common non-Python
  stack. **5/5 profiles now covered**: exploration (the workflow runner), refactor
  (livespec-mcp), RF flow (demo-app), Django bugfix
  (Django), TS feature (SpeedRunners-landing).
- Confirmed working clean on TypeScript: `find_symbol`,
  `quick_orient`, `who_calls(max_depth=2)` (paginated to 10 of 27),
  `get_symbol_source`, `analyze_impact(summary_only=True)`,
  `audit_coverage(summary_only=True)` on a 0-RF TS repo.
- Three new TS-specific bugs surfaced (#18-#20):
  - **#18** `get_project_overview.top_symbols` polluted by bundler
    output (`_fresh/`, `dist/`, etc.) — top 18 of 20 symbols on a
    Fresh app live in minified bundles.
  - **#19** `find_dead_code` over-reports on Fresh apps (974
    candidates: 630 in `_fresh/`, 222 in `islands/` referenced via
    JSX from `routes/*.tsx`).
  - **#20** JSX element references not captured as call-graph
    edges. The TSX extractor would need to walk `JSXElement` nodes
    and emit refs.

### Tooling
- Default surface: **16 tools** (unchanged from v0.9). Plugin tier:
  14. Total max active: 30.
- Tests: 175 → **179** (+4 from `tests/test_exports_protect.py`).
- Schema: v7 (no migration in v0.10).

### Deferred to v0.11+
- Bug #18 — bundler-output dir filter on `top_symbols` and
  `find_dead_code` (`_fresh/`, `dist/`, `build/`, `.next/`, `out/`).
  Trivial fix, queue for next cycle.
- Bug #19 — TS framework entry-point detection (Fresh `islands/`,
  Next.js `pages/` + `app/`, SvelteKit `routes/`). Mirrors v0.9 P5
  Django CBV detection for the JS frameworks.
- Bug #20 — JSX element refs as edges in the TSX extractor.
- Out-of-tree runtime registration (Django `Field.register_lookup()`
  runtime calls). The remaining 348 Django candidates are largely
  this pattern.
- Closure-capture detection in non-Python languages.
- Optional LLM-assisted RF refinement on
  `propose_requirements_from_codebase`.

---

## [0.9.0] — 2026-05-01

The "Django readiness" release. Drives the v0.8 P2 battle-test bugs
(#12-#16) to closure end-to-end. The primary signal: same Django
codebase, same queries, dramatically cleaner answers.

| Tool | v0.8 | v0.9 | Delta |
|---|---:|---:|---:|
| `find_dead_code` count | 824 | 514 | −38% noise |
| `find_dead_code` functions | 450 | 189 | −58% |
| `find_endpoints(django)` | 20 | 162 | +8× |

### Removed (breaking) — v0.9 P6
- **`get_index_status` tool**. Honors the v0.8 P3.2 deprecation
  contract. Read the `project://index/status` resource for the
  same payload.

### Added — v0.9 P0 perf
- **Targeted `_resolve_refs` walk** on partial reindex. Closes the
  v0.7 deferred item. When a re-index changes only a subset of
  files (no `force`, no deletions, prior index run exists), the
  resolver walks only refs whose src is in a changed file OR whose
  `target_name` matches a name re-inserted in a changed file. Refs
  from unchanged files to unchanged files keep their existing
  edges (INSERT OR IGNORE on the same `(src, dst)` is a no-op).
  Measured on `requests`: partial reindex 25.3ms → 12.3ms (−51%).
  On Django the relative win is larger (refs scale superlinearly
  with symbols).

### Added — v0.9 P2 pagination on traversals
- **`who_calls`, `who_does_this_call`, `analyze_impact`** now accept
  the v0.7 B3 pagination contract — `limit` (default 200), `cursor`,
  `summary_only`. Closes session-04 bugs #12 and #13. At
  `max_depth=2` on `BaseBackend.authenticate` the unpaginated
  response was 102 KB (400 callers / 71 files); `analyze_impact`
  at `max_depth=3` was 332 KB (664 callers + 848 calls_into).

### Added — v0.9 P3 weight filter on traversals
- **`who_calls` / `who_does_this_call` / `quick_orient` /
  `analyze_impact`** default to `min_weight=0.6`, dropping the
  resolver fan-out edges (weight 0.5 — short-name candidates the
  static analyzer couldn't disambiguate). Closes session-04 bugs
  #14 and #17. Pass `min_weight=0.0` to recover the legacy
  unfiltered cone. The internal correctness tools (`find_dead_code`,
  `audit_coverage`) continue to count every edge so an ambiguous
  caller still proves the symbol is reachable.

### Added — v0.9 P4 Django dead-code accuracy (#16)
- **Skip non-Python files in `find_dead_code` by default**. The
  module-level reference scanner is Python-only — JS/Go/Java
  callsites are invisible to it. Vendored xregexp.js helpers
  (~70 of them) used to be over-reported on Django. New
  `include_non_python=True` opt-in restores the legacy behavior.
- **Recognize string-based dotted-path references** in the
  module-level scanner. Django settings register implementations
  as strings: `INSTALLED_APPS = ['app.apps.AdminConfig']`,
  `MIDDLEWARE`, `PASSWORD_HASHERS`, `default_app_config`.
  `_collect_module_refs` now adds the trailing identifier of any
  validated dotted-path string constant to the refs set.
- **Recognize Django framework inner-class hooks**:
  `class Meta:` / `class Migration:` inner classes are read
  reflectively by Django's metaclasses. Guarded by parent-segment
  PascalCase check so a stray module-level `class Meta:` is still
  flagged dead.

### Added — v0.9 P5 Django CBV detection in `find_endpoints` (#15)
- **`find_endpoints(framework='django')`** now scans class
  signatures for inheritance from Django's class-based view bases
  (View, TemplateView, ListView, DetailView, FormView, CreateView,
  UpdateView, DeleteView, RedirectView, archive views), auth
  mixins (LoginRequiredMixin, PermissionRequiredMixin,
  UserPassesTestMixin, AccessMixin), auth views (LoginView,
  LogoutView, PasswordResetView family), MiddlewareMixin,
  AutocompleteJsonView, and DRF-adjacent (APIView, ViewSet
  family). Matched classes ship a `django_cbv_base` field naming
  the responsible parent. Endpoints from both passes (decorator +
  CBV) are merged on `qualified_name` and sorted by
  `(file_path, start_line)` for stable cursor pagination.

### Tooling
- Default surface: 17 → **16 tools** after dropping
  `get_index_status`. Plugin tier unchanged (11 RF + 3 docs).
  Total max active: **30**.
- Tests: 157 → **175** (+18 net: +4 targeted resolver, +6
  traversal pagination, +4 weight filter, +4 dead-code Django,
  +4 CBV detection; −4 deprecation tests deleted with the tool).
- Schema: v7 (no migration in v0.9).

### Deferred to v0.10+
- Out-of-tree runtime registration detection (Django
  `PASSWORD_HASHERS` + `DATABASES` backend dotted-paths,
  `Field.register_lookup()` runtime calls). The remaining 514
  Django dead-code candidates are largely this pattern.
- Closure-capture detection in non-Python languages (TS arrow
  callbacks, Rust closures). Still open from v0.8.
- Optional LLM-assisted RF refinement on
  `propose_requirements_from_codebase`. Still open from v0.7.
- Session 05 (TS/JS feature flow) for language coverage closeout.

---

## [0.8.0] — 2026-05-01

The "curation" release. v0.7 piled on tools (39 + 4 deprecated aliases);
v0.8 cuts the surface to **17 default tools** plus two auto-loading
plugins (RF mutation = 11 tools, doc management = 3 tools). The
curation is data-driven: 3 sessions of real-agent battle-test logged
40 calls across 3 codebases (the workflow runner, livespec-mcp, demo-app)
and 24 of 39 tools never got called. The drops follow the data, not
the prior intuition. Stakeholder posture stays locked in: RF
traceability is the differentiator (RF agentic tools stay tier-1),
agent UX is the product (4 quick-win composites added before the
battle-test).

### Removed (breaking) — tier-4 drops (v0.8 P3.3)
- **8 tools dropped** based on zero or near-zero agent calls in
  3 sessions across 3 profiles:
  - `list_files` — Grep/ripgrep host with path glob covers it
  - `start_watcher`, `stop_watcher`, `watcher_status` — race-condition
    trap for editing agents; re-run `index_project` on demand
  - `rebuild_chunks` — auto-runs inside `index_project`
  - `get_call_graph` — `who_calls` + `who_does_this_call` cover both
    cones with cleaner output
  - `get_symbol_info` — `quick_orient` (composite) +
    `get_symbol_source` (body) cover both modes
  - `search` — FTS5 lane logged 0 agent calls; `find_symbol` +
    `quick_orient` are the canonical lookup path
- **Deprecated v0.6 RF tool aliases** are gone (P3a):
  - `link_requirement_to_code`     → use `link_rf_symbol`
  - `link_requirements`            → use `link_rf_dependency`
  - `unlink_requirements`          → use `unlink_rf_dependency`
  - `get_requirement_dependencies` → use `get_rf_dependency_graph`

### Changed (breaking) — plugin auto-detect (v0.8 P3.1, P3.4, P3.5)
- New `tools/plugins/` framework: at server startup the active
  workspace's DB is probed; plugins auto-load based on table state.
  `LIVESPEC_PLUGINS=none|all|rf,docs` env var overrides the soft
  default.
- **`livespec-rf` plugin** (auto-on when the `rf` table has rows for
  the active project, or when `LIVESPEC_PLUGINS` includes `rf`):
  registers the 11 RF mutation/linking tools — `create_requirement`,
  `update_requirement`, `delete_requirement`, `link_rf_symbol`,
  `bulk_link_rf_symbols`, `link_rf_dependency`, `unlink_rf_dependency`,
  `get_rf_dependency_graph`, `scan_rf_annotations`,
  `scan_docstrings_for_rf_hints`, `import_requirements_from_markdown`.
- **`livespec-docs` plugin** (auto-on when the `doc` table has rows,
  or when `LIVESPEC_PLUGINS` includes `docs`): registers the 3 doc-
  management tools — `generate_docs`, `list_docs`, `export_documentation`.
- The agentic-read RF tools (`list_requirements`,
  `get_requirement_implementation`, `propose_requirements_from_codebase`,
  `audit_coverage`) stay in the default surface — they answer questions
  an agent ASKS during work.

### Deprecated (non-breaking, drops in v0.9) — v0.8 P3.2
- **`get_index_status` tool**. Use the `project://index/status`
  resource (parity since P3b prep). The tool now ships
  `deprecated`/`replacement`/`removal` keys in its payload and emits
  a one-time stderr warning per process.

### Added — v0.8 P0 quick wins
- **`get_symbol_source(qname)`** — body slice extraction. Lighter than
  `get_symbol_info(detail='full')` when only the source text is needed.
  Returns `{qualified_name, file_path, start_line, end_line, source,
  body_hash}`.
- **`who_calls(qname, max_depth=1)`** — agentic alias for the backward
  cone of `analyze_impact`. Returns only the caller list, no forward
  cone, no RF rollup. Use when the agent's question is "what would
  break if I touched this?".
- **`who_does_this_call(qname, max_depth=1)`** — forward-direction
  counterpart of `who_calls`.
- **`quick_orient(qname)`** — composite first-contact snapshot.
  Combines symbol metadata, the first non-empty docstring line, the
  top-5 direct callers and top-5 direct callees ranked by PageRank, and
  any linked RFs. Replaces a typical `find_symbol` → `get_symbol_info`
  → `analyze_impact` → `get_requirement_implementation` chain with a
  single call.

### Added — v0.8 P2 prep (battle-test harness)
- **`bench/agent_log_analyze.py`** — aggregator over one or more
  `agent_log.jsonl` streams. Per-tool call count, errors, latency
  p50/p95, result_chars p50/max; top follow-up pairs (`A → B` within
  a session — surfaces 3-tool chains that a composite tool could
  collapse); silent-tool list (registered but never called — drop
  candidates). Markdown by default, `--json` for diffing across runs.
  Pre-fills the input feed for the v0.8 P3 curation pass.
- **`docs/AGENT_USAGE_DATA.md`** — skeleton for the field log. Lists
  target codebases, methodology notes, and the Findings template
  to fill once P2 sessions complete.

### Added — v0.8 P1 instrumentation
- **Agent dispatch logging middleware**
  (`src/livespec_mcp/instrumentation.py`). Writes one JSONL line per
  `tools/call` to `<workspace>/.mcp-docs/agent_log.jsonl` with
  `{ts, tool_name, args_redacted, latency_ms, result_chars, error,
  session_id, workspace}`. Args are redacted: any string containing
  the absolute workspace path is rewritten to `<workspace>/...` so
  logs are shareable. `LIVESPEC_AGENT_LOG=0` disables. Failures
  writing the log are swallowed — instrumentation never breaks
  dispatch. Sets up the v0.8 P2 battle-test (5 codebases × 3-5
  sessions) and feeds the v0.8 P3 data-driven curation pass.

### Added — v0.8 P2 battle-test sessions
- **3 sessions logged** across 3 codebases (the workflow runner 1173 syms, livespec-mcp
  itself 495 syms, demo-app 23 syms / 6 RFs), 40 calls total.
  Surfaces 11 bugs (#1-11), all fixed in this release.

### Fixed — v0.8 P2 bug batch (#1-11)
- **#1 Edge resolver same-name fan-out** (`_resolve_refs`). Multiple
  symbols sharing a short name (`list_tools` x3, `_cosine` x2)
  matched against a single call site, polluting `who_calls` and
  `quick_orient.top_callees`. Same-file fallback weight 0.7 when scope
  doesn't disambiguate. livespec-mcp edge count 969 → 752 (−227,
  ~22% reduction in false positives).
- **#2 Entry-point flag** in `quick_orient`. `@mcp.tool` / `@app.route`
  / etc. with 0 callers no longer reads as "dead". Output now ships
  `is_entry_point: bool` + `framework_decorators: [...]`.
- **#3 Structural-pattern noise** in `get_project_overview`. Top
  symbols dominated by `.get` x4 modules, `add_parser` x6 CLI
  subcommands, `run` x5 etc. — high PageRank but zero "what is this
  codebase about" signal. New filter excludes names that appear in
  ≥3 distinct files; opt-out via `include_structural_patterns=True`.
- **#4 `__main__` guards** as entry points. `bench.run.main`,
  `server.main` etc. flagged dead despite being called from
  `if __name__ == "__main__":` blocks. Module-level AST walk now
  collects refs from those guards.
- **#5 List/tuple-stored function refs**. `_m001_drop_dead_tables`
  through `_m007_visibility` flagged dead despite being referenced
  in the `MIGRATIONS = [(version, name, fn), ...]` list literal.
  Module-level walk now picks up bare-name refs in collection
  literals.
- **#6 Cross-file middleware lifecycle hooks**.
  `AgentLogMiddleware.on_call_tool` flagged dead despite being
  registered cross-file via `mcp.add_middleware(AgentLogMiddleware())`.
  Detection extended to recognize classes passed as arguments to
  `add_middleware` / similar registration calls.
- **#7 Test-fixture leakage** in `git_diff_impact.suggested_tests`.
  Files under `tests/fixtures/`, `tests/data/`, `__fixtures__/` now
  excluded from suggestions (they are not tests, they are inputs).
- **#8 `__init__.py` orphan flag** in `audit_coverage`. Package-marker
  files (`__init__.py`, `mod.rs`, `package-info.java`, `lib.rs`,
  `index.{ts,js}`) excluded from `modules_truly_orphan`.
- **#9 RF test-coverage signal** in `audit_coverage`. New
  `rf_test_coverage` field + `rfs_with_test_coverage` count surfaces
  test edges (`relation='tests'`) as a positive signal distinct from
  `relation='implements'`.
- **#10 Test-file proposals** from `propose_requirements_from_codebase`.
  No more "RF-009 Test Shortener" groupings: paths under `tests/`,
  `test/`, `__tests__/`, `fixtures/` skipped.
- **#11 Closure-callback nested fns** in `find_dead_code`.
  `start_watcher._do_reindex` flagged dead despite being passed as
  a callback (`Watcher(on_reindex=_do_reindex)`). Per-file
  `_used_nested_def_names` walk recognizes nested-def references in
  the parent scope's body.

After all 11 fixes wire-validated against livespec-mcp itself,
`find_dead_code` reports 0 (vs 18 pre-fix) — 100% noise reduction on
the dogfood repo.

### Added — v0.8 P4 pitch alignment
- `README.md` rewrite: new headline framing RF traceability as the
  defensible differentiator (not "(optional)"), tool surface restructured
  by tier (default / livespec-rf plugin / livespec-docs plugin),
  Performance section with battle-test numbers, "Agent vs human user"
  section explaining the surface split.
- `docs/AGENT_QUICKSTART.md` documents the canonical brownfield flow.
- `docs/AGENT_USAGE_DATA.md` captures the field log behind the
  curation decisions (40 calls / 3 sessions / 3 profiles).

### Tooling
- Default surface: **17 tools** (down from 39 in v0.7). Plugins add
  11 (rf) + 3 (docs) = **31 max active** when both plugins are loaded.
  Removed 4 deprecated v0.6 aliases for a true wire-count of 31 with
  no deprecated surface.
- Tests: 118 → **157**. Net +39 (+10 quick wins, +5 instrumentation,
  +8 analyzer, +12 plugin autoload, +4 deprecation, +others;
  −9 search/watcher/embeddings tests, −1 alias-compat).
- Schema: v7 (no migration in v0.8).

### Deferred to v0.9
- Drop the deprecated `get_index_status` tool (resource has been
  parity-equivalent since v0.8 P3b prep).
- Closure-capture detection in non-Python languages (TS arrow
  callbacks, Rust closures).
- `_resolve_refs` targeted re-walk (Django partial 7s → 1s) — still
  open from v0.7.
- Optional LLM-assisted RF refinement on `propose_requirements_from_codebase`.

---

## [0.7.0] — 2026-05-01

The "brownfield" release. Closes the friction gap between "fresh project
with livespec from day 1" and "existing 50K-symbol Rust monorepo,
adopting livespec on Tuesday afternoon". Three new agent-facing tools
(bulk_link_rf_symbols, scan_docstrings_for_rf_hints,
propose_requirements_from_codebase) plus correctness fixes that the
a large Rust monorepo stress test surfaced.

### Added — brownfield onboarding flow
- **`propose_requirements_from_codebase()`** (B2) — the headline feature.
  Heuristic RF discovery: groups symbols by qname prefix at
  `module_depth`, ranks each group by PageRank-weighted score, proposes
  one RF candidate per actionable group with humanized title +
  description from the top symbol's docstring + suggested_symbols list.
  Pair with create_requirement + bulk_link_rf_symbols to convert from
  "no RFs" to "fully traced" in N rounds instead of N×M.
- **`bulk_link_rf_symbols(mappings)`** (B1) — batch-link N RF↔symbol
  pairs in one transaction. Returns per-entry result so failures don't
  abort the batch. Idempotent (re-link returns ok=True linked=False).
- **`scan_docstrings_for_rf_hints()`** (B6) — surfaces RF candidates
  from existing docstrings that aren't yet linked. First sentence +
  leading verb extraction; verb_histogram_top output gives the agent
  the input signal for B2.

### Added — tool quality
- **Pagination on aggregator tools** (B3) — `find_dead_code`,
  `audit_coverage`, `find_orphan_tests`, `find_endpoints`,
  `git_diff_impact` now accept `limit` (default 200) + `cursor` +
  `summary_only`. Triggered by the a large Rust monorepo stress test where
  `audit_coverage` produced 286K chars, `find_dead_code` 4.4M chars,
  `git_diff_impact` 7.3M chars — all over the MCP 25K-token budget.
- **`find_dead_code` skips Rust `pub` items** (B4) — symbols whose
  visibility is `pub` / `exported` / `public` are excluded by default
  (they have invisible callers from outside the indexed crate).
  `pub(crate)` and `pub(super)` are NOT skipped (scope-bounded).
  Override with `include_public=True`. The 23K dead-flagged symbols on
  a large Rust monorepo dropped to a manageable list.
- **Schema migration v7**: `symbol.visibility` column populated by the
  extractor for Rust (`pub`/`pub(crate)`/`pub(super)`/`private`),
  TS/JS (`exported`), Java/PHP (`public`/`private`/`protected`).
- **`find_symbol` is separator-agnostic** (B5) — query
  `SyncQueue::push` matches Rust qnames stored as
  `app.src.server.sync_queue.SyncQueue::push`. Works in both
  directions (`Type.method` query also reaches `Type::method` qnames)
  and accepts `/` as a separator (path-style searches).

### Tooling
- Tools: 32 → 35 (+ 4 deprecated v0.6 aliases still present through
  v0.7 → wire count 39).
- Tests: 97 → 118 (+3 find_symbol normalization, +6 pagination,
  +2 visibility, +3 bulk_link, +3 rf_hints, +4 propose_requirements).

### Deferred to v0.8
- Drop the v0.6 deprecated aliases (`link_requirement_to_code`,
  `link_requirements`, `unlink_requirements`,
  `get_requirement_dependencies`) — they were promised through v0.7.
- `_resolve_refs` targeted re-walk (partial reindex on Django: 7s → ~1s).
- LLM-assisted RF refinement: optional sampling layer on top of B2's
  heuristic to refine titles + descriptions with the agent's reasoning.

---

## [0.6.0] — 2026-05-01

The "hardening" release. Stops the feature treadmill to pay down debt:
explicit migration framework, unified error shape, performance baseline on
a 40K-symbol repo with the obvious hotspot patched, deprecated tools
removed, ambiguous tool names disambiguated. Pitch reframed honestly —
"living traceability + on-demand docs" instead of overclaiming on the docs
side.

### Removed (breaking)
- **`use_workspace` MCP tool** (deprecated since v0.2). Pass
  `workspace=<path>` to every tool, or set `LIVESPEC_WORKSPACE` in the env.

### Renamed (deprecated aliases retained through v0.7)
- `link_requirement_to_code`     → `link_rf_symbol`
- `link_requirements`            → `link_rf_dependency`
- `unlink_requirements`          → `unlink_rf_dependency`
- `get_requirement_dependencies` → `get_rf_dependency_graph`

The old names still work — they delegate to the new implementations and
will be removed in v0.7. Naming disambiguates the two link concepts:
`link_rf_symbol` (RF → code) vs `link_rf_dependency` (RF → RF).

### Added
- **Explicit migration framework** (P2) — replaces ad-hoc
  `_migrate_v1_to_v2` with `schema_migrations(version, name, applied_at)`
  table backing an ordered, append-only migration list. Each migration
  is a small idempotent function; once applied, the version is recorded
  so subsequent connects skip already-applied work. Six migrations
  registered, retroactively covering every v0.1→v0.5 schema change.
- **Unified error payload helper** (P4) — `tools/_errors.py:mcp_error()`
  enforces a single shape across every tool error site:
  `{error, isError, did_you_mean?, hint?}`. Refactored ~15 sites in
  analysis, requirements, docs, and search tools. Removed the legacy
  `warning` field on `analyze_impact`.
- **Hints on actionable errors** — RF-not-found, symbol-not-found,
  file-not-indexed, cycle-detected, embeddings-missing, git-not-on-PATH,
  git-timeout. Each one now ships with a one-line `hint` field telling
  the agent what to run next.
- **Graph cache** (P3) — `domain/graph.py` now caches the loaded
  `GraphView` keyed by `(db_path, project_id, last_index_run_id)`.
  Building the NetworkX object from SQL costs ~4s on a 40K-symbol repo
  and was repeated on every analysis call; cache hits drop to µs and
  invalidate automatically when a new index run lands.
- **Django stress test** (P3) — `bench/run.py --large` runs against
  Django 5.1.4 (~40K symbols, 1M edges). Numbers documented in
  `bench/README.md`.

### Fixed
- **Duplicate (qname, start_line) crash** — Django's compatibility shims
  (`def cached_property(...)` defined twice under a Python-version `if`)
  produced symbols that tripped the v0.6 schema's UNIQUE constraint.
  `_replace_symbols` now deduplicates by `(qname, start_line)` before
  insert, keeping the first occurrence (source order).

### Changed
- **README pitch** — was "living documentation"; now "living
  traceability + on-demand docs" with an explicit table calling out
  what is/isn't auto-maintained. Drift is detected, not fixed —
  auto-doc-on-drift is a deferred v0.7+ candidate.

### Deferred to v0.7
- **`_resolve_refs` targeted re-walk** — partial reindex on Django
  takes 7s because the resolver re-walks all 1M `symbol_ref` rows. Filter
  to refs whose `target_name` matches a name in the changed file.
- **Auto-doc-on-drift watcher mode** — optional, opt-in, with a clear
  cost UX (LLM calls implicit).
- **Multi-tenant memory pressure handling** — current LRU=8 doesn't
  consider per-workspace RSS; a Django-scale cache could hit ~5GB worst
  case across 8 workspaces.
- **Drop deprecated v0.6 aliases** (`link_requirement_to_code`,
  `link_requirements`, `unlink_requirements`,
  `get_requirement_dependencies`).

### Tooling
- Tests: 83 → 97 (+4 migrations, +6 error shape, +3 graph cache, +1
  alias-still-works).
- Tool count: 33 → 32 (use_workspace removed) plus 4 deprecated aliases
  through v0.7. Wire count during the deprecation window: 36.

---

## [0.5.0] — 2026-05-01

The "self-improvement from real-world usage" release. Bug fixes from a
demo-project run, two new agent-modeling features (decorators + RF
dependency graph), and a hardened matcher with a regression-locked golden
dataset. Closes the last multi-language scoped-resolution gap (Rust).

### Added
- **`find_endpoints(framework=None)`** — list symbols decorated with
  framework entry-point markers (route, command, fixture, tool, task, etc).
  Per-framework presets: flask, fastapi, click, pytest, fastmcp, celery,
  django.
- **RF dependency graph** (P2):
  - `link_requirements(parent_rf_id, child_rf_id, kind)` with cycle
    detection on insert. `kind` ∈ {requires, extends, conflicts}.
  - `unlink_requirements(parent, child, kind=None)` — drops one specific
    edge or every edge between the pair.
  - `get_requirement_dependencies(rf_id, direction='both', max_depth=5)` —
    walk the RF graph forward / backward / both.
  - `analyze_impact(target_type='requirement')` now cascades through
    dependents — changing RF-001 surfaces every RF that transitively
    depends on it as `dependent_requirements`.
- **Multi-RF, confidence override, and explicit negation in the matcher**:
  - `@rf:RF-001, RF-002` — multi
  - `@rf:RF-001:0.85` — per-line confidence override (range [0.0, 1.0])
  - `@not_rf:RF-001` / `@!rf:RF-001` — cancel any L1+L2 hit on the listed
    RF in this docstring (overrides verb-anchored false positives that
    the negation-window heuristic missed).
- **Golden-dataset regression test** (`tests/data/matcher_golden.jsonl`,
  35 cases) — locks every supported annotation form against silent
  regression.
- **Rust scoped resolution** via `use` declarations (P4.A3) — closes the
  last common-language gap from P0.4. `use crate::module::Item`,
  `Item as alias`, brace groups, and recursive paths all populate the
  imports map. Cross-module Rust calls now resolve to weight=1.0 edges.
- **`audit_coverage`: `modules_implicitly_covered` + `modules_truly_orphan`**
  (P0.A1) — splits `modules_without_rf` into "reached transitively by
  rf-linked symbols" vs "actually orphan". The truly_orphan list is the
  actionable subset. Real bug surfaced on the demo-app run.
- **`symbol.decorators` (JSON)** — schema migration v3 adds the column
  and queues a forced re-extract via `_migration_state.needs_reextract`.
  Python `_py_extract` populates from `decorator_list`. Tree-sitter langs
  ship with `[]` until per-language extractors land.

### Changed
- **`find_dead_code` skips framework-decorated symbols by default**
  (P1.B1). A `@app.route` handler, `@click.command`, `@pytest.fixture`,
  `@mcp.tool`, etc. is no longer flagged as dead even with zero
  in-project callers — they have hidden callers (HTTP routers, CLI
  dispatchers, pytest collection, MCP). Pass `include_infrastructure=True`
  to bypass.
- **`git_diff_impact` clean error messages** (P0.A2) — previously dumped
  the full `git diff --help` output (~80 lines) into the error field
  when the workspace wasn't a git repo or the ref was unknown. Now
  classifies common stderr signatures and returns a single line.
- **body_hash invariant under reformat** (P0.D2) — Python is unchanged
  (already stable through ast.dump). Tree-sitter languages now seed the
  body hash from a leaf-token walk that ignores whitespace, blank lines,
  and comment nodes. Reformat (autoformat run, indent change, blank-line
  shuffle, comment add/remove) no longer drifts the doc; real semantic
  change still does.
- **`call_target_and_leftmost`** treats `::` as a path separator alongside
  `.`, enabling Rust `Item::method()` and PHP `Class::method()` to extract
  rightmost target + leftmost identifier correctly.

### Tooling
- Tool count: 30 → 33 (+ link_requirements, unlink_requirements,
  get_requirement_dependencies; find_endpoints replaced an internal helper).
- Tests: 76 → 83 (+1 audit transitive split, +1 git_diff not-a-repo,
  +3 body_hash stability, +2 decorators in dead/endpoints, +1 Rust
  scoped, +5 RF deps, +1 golden dataset runner, +misc).
- Schema migration v3: `symbol.decorators` + `rf_dependency` table.

### Deferred to v0.6
- mkdocs site (C5) — nontrivial setup, not blocking.
- Auto-doc on drift mode in the watcher — needs careful UX around LLM
  cost.
- Streaming graph queries via SQLite recursive CTE — only matters above
  ~50K symbols.

---

## [0.4.0] — 2026-05-01

The "multi-language parity + agent UX" release. Closes the scoped-resolution
debt from P0.4 across 5 more languages, adds three aggregator tools that
reuse the call graph + RF tables for free, and surfaces `did_you_mean`
suggestions on misspelled symbol identifiers.

### Added
- **Scoped resolution for TS/JS, Go, Ruby, PHP** (P1) — closes the multi-language
  parity gap from P0.4 (Python-only). ES6 imports + CommonJS requires for
  TS/JS, package imports + aliases for Go, `require_relative` for Ruby (+
  Const.method receiver lookup), `use` namespaces for PHP (+ `Class::method`
  scoped-call lookup). Cross-file/cross-package calls now emit `symbol_edge`
  rows with `weight=1.0`.
- **`find_dead_code()`** (P2) — symbols with zero callers and zero RF links;
  filters entry-point paths (`tests/`, `scripts/`, `bin/`, `__main__.py`,
  `manage.py`) and implicit entry points (dunders, FastMCP `register` outers,
  DI helpers).
- **`audit_coverage()`** (P2) — three RF coverage signals:
  `modules_without_rf`, `rfs_without_implementation`, `rfs_low_confidence`
  (avg confidence < 0.7).
- **`find_orphan_tests()`** (P2) — test functions whose forward call cone
  never reaches a non-test symbol.
- **`did_you_mean` field** (P2) on every `Symbol '<x>' not found` error
  across 5 tools (`get_symbol_info`, `get_call_graph`, `analyze_impact`,
  `link_requirement_to_code`, `generate_docs`). Two-pass matcher: SQL
  substring + difflib edit-distance.
- **`stop_all_watchers()` + `atexit` hook** (P2) — server shutdown flushes
  WAL files cleanly.

### Changed
- `_resolve_module_path()` for TS/JS converts relative paths and strips
  `.ts/.tsx/.js/.jsx/.mjs/.cjs` plus trailing `/index`.
- `call_target_and_leftmost()` now reads `receiver` (Ruby), `scope` (PHP),
  `object` (JS member access) fields. Strips PHP `$` and namespace
  backslashes when computing the leftmost identifier.

### Tooling
- Tool count: 26 → 29.
- Tests: 53 → 71 (59 default + 2 embeddings + 10 new in this batch).
- New language fixtures: TS / JS / Go / Ruby / PHP cross-module dirs.

### Fixed (CI)
- `.github/workflows/ci.yml` switched from `uv pip install --system`
  (PEP 668: externally managed `/usr` Python on Ubuntu runners) to per-matrix
  `uv venv --python X.Y` + `uv run pytest`. Matrix now actually runs each
  Python version; embeddings job also fixed.

---

## [0.3.0] — 2026-04-30

The "honesty + agent-loop" release. Closes the multi-language coverage debt
from v0.2 and adds the killer demo tool: `git_diff_impact`.

### Added
- **`git_diff_impact(base_ref, head_ref, max_depth)`** — changed files →
  callers → impacted RFs → suggested test files. The CI/PR-review entry
  point. (P1)
- **`delete_requirement(rf_id)`** — cascade-removes `rf_symbol` links. (P1)
- **`import_requirements_from_markdown(path)`** — bulk-create RFs from
  `## RF-NNN: Title` markdown with `**Prioridad:**` / `**Módulo:**`
  metadata. Idempotent. (P2)
- **`code://symbol/{qname}` resource** — fetch the source body of a symbol
  by qualified name. (P2)
- **`watch=True` flag on `index_project`** — start the file watcher in the
  same call. (P1)
- **Hypothesis property tests** — 4 properties covering matcher invariants,
  resolver weights, and indexer idempotence. (P2)
- **Memory benchmark** — RSS sampling during index of `requests` repo,
  baseline in `bench/results-baseline.json`. (P2)
- **GitHub Actions CI** — matrix Python 3.10/3.11/3.12 + dedicated
  embeddings job. (P2)
- **Ruby + PHP fixtures + extractor tests** — upgrades both languages from
  "untested" to "tested" in the language-support table. (P2)
- **Embeddings smoke test** — guarded by `pytest -m embeddings`, validates
  fastembed + sqlite-vec end-to-end when extras are installed. (P1)

### Changed
- **Auto-scan `@rf:` annotations after every `index_project`** — traceability
  stays fresh without requiring a separate `scan_rf_annotations` call. (P0)
- **PageRank infrastructure filter** in `get_project_overview` —
  `_is_infrastructure` heuristic excludes DI helpers, dunders, FastMCP
  `register` outers, and 1-line wrappers from the top-N by default. Opt-in
  with `include_infrastructure=True`. (P0)
- **Scoped resolution by imports for Python** — `symbol_ref.scope_module`
  populated from `import` / `from … import …` statements. Edges resolve to
  weight=1.0 when the target is in scope, weight=0.5 only as global
  fallback. (P0)
- **Migration `_migration_state.needs_reextract`** consumed correctly so
  stats reflect post-upgrade reality. (P0)

### Tooling
- **26 MCP tools** (was 23 in v0.2). Net additions: `git_diff_impact`,
  `delete_requirement`, `import_requirements_from_markdown`.
- **53 tests** total (51 default + 2 `embeddings`-marked).
- **8 languages with passing extractor tests:** Python, Go, Java,
  JavaScript, TypeScript, Rust, Ruby, PHP.

---

## [0.2.0] — internal

Multi-tenant + tool consolidation. Tagging skipped; rolled into v0.3.

### Added
- `use_workspace(path)` runtime workspace switching, then per-call
  `workspace=` argument. LRU cache (size=8) of DB connections.
- `start_watcher` / `stop_watcher` / `watcher_status` (watchdog wrapper).
- Bench suite (`bench/run.py`, `bench/results-baseline.json`).
- Large-repo procedural fixture (100+ symbols).
- Regression test suite locking in 4 prior bugs (edge wipe on idempotent
  re-index, FTS5 score corruption, signature drift, lost edges from
  unchanged files during incremental).

### Changed
- **Tool consolidation 25 → 23.** Six v0.1 tools were removed in favor of
  parameterized variants (see migration table below).
- Stateless server: workspace is resolved per-call (env →
  `LIVESPEC_WORKSPACE` → cwd) instead of held as global state.
- Persistent `symbol_ref` table (replaces in-memory ref dict from earlier
  experiments).

### Removed (breaking)
| v0.1 tool | Replacement |
|-----------|-------------|
| `find_references(identifier)` | `analyze_impact(target_type='symbol', target=qname, max_depth=1)` — read `impacted_callers` |
| `suggest_rf_links(rf_id)` | `search(query=<rf.title + rf.description>, scope='code')` + post-filter |
| `embed_pending()` | `rebuild_chunks(embed='yes')` |
| `generate_docs_for_symbol(identifier)` | `generate_docs(target_type='symbol', identifier=…)` |
| `generate_docs_for_requirement(rf_id)` | `generate_docs(target_type='requirement', identifier=rf_id)` |
| `detect_stale_docs(target_type)` | `list_docs(target_type, only_stale=True)` |

---

## [0.1.0] — internal

Bootstrap. Phases 0–6 of the original design.

### Added
- FastMCP 2.x server with stdio transport, `fastmcp.json` entry.
- SQLite schema (`project`, `file`, `symbol`, `edge`, `rf`, `rf_symbol`,
  `doc`, `chunk` + FTS5 + optional `vec0` virtual table). WAL mode,
  `foreign_keys=ON`.
- Tree-sitter + `tree-sitter-language-pack` parsing for the multi-language
  generic extractor.
- Python `ast`-based extractor for high-precision Python (functions,
  classes, methods, decorators, calls).
- NetworkX call graph + PageRank with pure-Python fallback.
- xxhash content/body/signature hashing for incremental re-index.
- Two-level `@rf:` annotation matcher (`@rf:RF-NNN` weight 1.0,
  verb-anchored phrase weight 0.7) with negation guard.
- BM25 (`rank-bm25`) + FTS5 keyword search; optional `[embeddings]` extra
  with `fastembed` + `sqlite-vec` and Reciprocal Rank Fusion.
- `generate_docs` (dual-mode: caller-supplied vs MCP sampling), drift
  detection on body + signature hashes, `export_documentation` to markdown
  or JSON.
- 7 user-facing prompts: `onboard_project`, `analyze_change_impact`,
  `audit_requirement_coverage`, `extract_requirements_from_module`,
  `document_undocumented_symbols`, `refresh_stale_docs`, `explain_symbol`.
- Resources: `project://overview`, `project://index/status`,
  `project://requirements`, `project://requirements/{rf_id}`,
  `project://files/{path*}`, `project://symbols/{qname*}`,
  `doc://symbol/{qname*}`, `doc://requirement/{rf_id}`.

[0.12.0]: https://github.com/Rixmerz/livespec/compare/v0.11.0...v0.12.0
[0.23.0]: https://github.com/Rixmerz/livespec/releases/tag/v0.23.0
[0.11.0]: https://github.com/Rixmerz/livespec/releases/tag/v0.11.0
[0.7.0]: https://github.com/Rixmerz/livespec/releases/tag/v0.7.0
[0.6.0]: https://github.com/Rixmerz/livespec/releases/tag/v0.6.0
[0.5.0]: https://github.com/Rixmerz/livespec/releases/tag/v0.5.0
[0.4.0]: https://github.com/Rixmerz/livespec/releases/tag/v0.4.0
[0.3.0]: https://github.com/Rixmerz/livespec/releases/tag/v0.3.0
