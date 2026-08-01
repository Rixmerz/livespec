# livespec — Session Handoff

> **Para reanudar el trabajo después de `/clear`:** abrí este archivo y pedile al agente "leé HANDOFF.md y continuá". Contiene todo el contexto necesario para seguir sin re-explicar.

---

## 1. Identidad del proyecto

- **License:** GNU AGPL v3.0 only (`LICENSE`, SPDX `AGPL-3.0-only`).
  Relicensed from MIT in the post-0.29 docs/license pass. Network copyleft
  applies when a modified livespec is offered over a network (MCP/SaaS/API).
- **Repo remoto:** https://github.com/Rixmerz/livespec (branch `main`)
- **Repo local:** `<repo>` (Arch box,
  desde 2026-06; el path histórico `<repo>`
  era la máquina macOS anterior — referencias viejas en este doc pueden
  mencionarlo)
- **Demo project:** `<demo-repo>` (4 archivos Python con `@rf:` annotations en docstrings, ya tiene RFs persistidas en su `.mcp-docs/docs.db`)
- **MCP server:** Claude Code plugin `livespec@livespec` **0.30.0**
  (`uvx livespec@0.30.1`) and/or Cursor `user-livespec` pointing at a local
  checkout. **Every tool requires `workspace=/abs/repo`** — there is no
  `LIVESPEC_WORKSPACE` env fallback (removed).
- **Git user:** Juan Pablo Díaz S.
- **GitHub user:** Rixmerz (auth via gh CLI con scopes `repo, gist, read:org, workflow`).

---

## 2. Stack técnico (no cambiar sin razón)

- **FastMCP 3.x** — stdio transport, src-layout, `fastmcp.json`
- **SQLite** (un único `docs.db` en `.mcp-docs/`), WAL, ACID, foreign_keys=ON, single-file backup con `cp`
- **tree-sitter + tree-sitter-language-pack** (1.6.2 — pinned `<1.6.3` por falta de wheel macOS arm64 en versiones más nuevas)
- **Python `ast`** para extracción Python de alta precisión + scoped resolution por imports
- **NetworkX 3.x** para call graph + PageRank (con fallback pure-Python si scipy no está)
- **xxhash** para content/body/signature hashing
- FTS5 BM25 lane usa el bm25 nativo de SQLite (la dep `rank-bm25` se removió en v0.20 — nunca se importaba)
- **watchdog>=4.0** para file watcher
- **hypothesis + psutil** en `[dev]` extras
- ~~fastembed + sqlite-vec~~ **eliminados** (**v0.29.0**) — `search` es FTS5-only; mig v19 limpia `chunk_vec_*` / `embedded_at`

Todo el stack es local-first: 0 servicios externos, 0 API keys obligatorias, 0 Docker.

---

## 3. Estado actual

**HEAD: backlog del barrido cerrado (puntos 1, 2 y 3) + anotaciones huérfanas visibles + specs fantasma + vocabulario unificado + ids OpenSpec como único dialecto. Tests 660.**
Sin tag nuevo — todo bajo `[Unreleased]`.

Qué landeó, con evidencia sobre repos reales (antes → después):

- **IDs de rutas call-style navegables.** Una ruta Express/Hono con arrow
  inline se reportaba como `swagger.js:459` y toda tool que toma símbolo
  respondía `Symbol not found`. El arrow no tiene símbolo propio, pero
  `_ts_collect_calls` ya atribuye sus llamadas al scope que lo contiene
  (`__module__` si está a nivel de archivo): ese scope es el target honesto,
  sus edges *son* los del handler. Nuevo campo `handler_resolution`
  (`handler` | `enclosing_scope` | `unresolved`). 13 repos reales:
  **93 endpoints call-style, IDs muertos 14 → 0**, sin cambiar el conteo.
- **Los ids OpenSpec ya son el único dialecto, y migrar sale gratis.** El
  dogfooding sobre el propio repo mostró que nuestro árbol seguía clavando
  `SPEC-001…013` con marcadores `<!-- livespec:id=… -->`, porque soltarlos
  costaba todos los links. Ahora, misma capability + mismo heading con id
  distinto = la misma requirement: la fila conserva su PK (links, escenarios,
  historia) y pasa a responder al slug; `sync_openspec` los lista en
  `adopted`. Migramos nuestro árbol: 13 specs a slugs conservando los **191**
  links (antes forkeaba las 13 y retiraba las originales). Un heading
  cambiado sigue siendo requirement nueva. Además: una colisión de slug se
  resuelve numerando el slug (`ui-theme-selection-2`) en vez de saltar a
  `SPEC-NNN`, y `validate_openspec` avisa (warning, no gate) cuando una spec
  venida del árbol sigue con id legacy — 13 en nuestro árbol pre-migración,
  0 en los 14 repos reales de hoy.
- **Dos callejones sin salida que encontró la propia auditoría.** Borrado
  `_route_handler_name` (wrapper sin llamadores que detectó `find_dead_code`
  sobre nosotros mismos) y `find_orphan_tests` ya no reporta
  `tests/__init__.py` como archivo ciego.
- **Un solo vocabulario en los payloads.** `count` es el total exacto en todas
  las tools (`list_specs` decía `total`; ahora devuelve ambos). `find_symbol`
  cumple el contrato de paginación: aceptaba `limit` sin devolver conteo ni
  cursor, así que una respuesta truncada parecía completa — en un repo real
  `limit=5` devolvía 5 de **1812** sin decirlo. `grouped`/`group_db` se
  reportan siempre juntos y desde toda tool group-aware, incluida
  `get_project_overview` (en un group_db sus números son solo del proyecto
  local, y eso antes era mudo). `include_infra` → `include_infra_routes` en
  `find_legacy_flows`: parecía abreviatura de `include_infrastructure` y
  significa otra cosa (rutas vs símbolos). Rename duro, sin alias.
- **Renombrar una requirement OpenSpec ya no deja una spec fantasma.** Como el
  ID se deriva del encabezado, el rename creaba una spec nueva y dejaba la
  vieja con sus links, idéntica a una viva en `list_specs` (servicio real: 10
  specs antes, 11 vivas después). `sync_openspec` ahora pasa a `deprecated`
  las specs que el árbol dejó de declarar y las reporta en `specs.retired`;
  no las borra, porque su traceability es la evidencia de qué hay que
  reapuntar. El barrido se limita por procedencia (**migración v20**, columna
  aditiva `spec.source`, sin re-extract): las specs hechas a mano con
  `create_spec` y las filas anteriores a la columna nunca se tocan. 14 repos
  reales con DB existente: **0 specs retiradas**, idempotente en la segunda
  pasada.
- **`scan_spec_annotations` nombra los IDs que no existen.** Una requirement
  OpenSpec no tiene ID propio: se deriva del encabezado, así que renombrarlo
  cambia el slug y todo `@spec:` que lo usaba deja de matchear. Antes el payload
  decía `links_created: 0` y nada más. Ahora trae `unknown_annotation_ids`,
  un `unknown_annotation_sample` capado con símbolo y archivo, y un hint que
  apunta a `export_openspec` (el marcador `<!-- livespec:id= -->` fija el ID
  ante renames). Servicio Node real sin marcadores: de `{links_created: 0}` a
  3 IDs nombrados con su archivo. 14 repos reales en estado normal: **0 falsos
  positivos**. `scan_annotations` devuelve `ScanResult` en vez de int (solo
  llamadores internos).
- **`find_symbol` en grupo dice qué DB respondió y dónde vive el repo.**
  Devolvía `grouped: true` sin `group_db`, y el `file_path` de un match
  cross-repo es relativo al repo dueño, no al workspace pasado. Ahora emite
  `group_db` y `project_root` por match; ungrouped mantiene el payload magro.
  Grupo real de 13 repos: 20 de 20 matches en 5 proyectos, abribles.

- **Spring con ruta.** `compute_endpoints` joinea `route_ref` (role `server`) y
  emite `http_method`/`http_path`/`http_framework` como express/hono/python.
  La ruta ya estaba indexada; solo la leía `find_legacy_flows`. Servicio Spring
  real: endpoints con ruta **0 → 4 de 8**. Sin migración ni re-extract.
  Sigue faltando (extractor + re-extract): prefijo `@RequestMapping` de clase,
  `@RequestMapping(method=…)` y mapping sin path.
- **Flow Explorer dejó de inventar rutas Spring.** `_spring_route_from_source`
  tomaba la *primera* anotación de una ventana de 25 líneas, así que cada
  handler heredaba la ruta del anterior. Borrada.
- **`find_orphan_tests` declara los archivos ciegos por archivo**
  (`test_files_without_symbols` + sample capado). El hint de 0.29 se apagaba en
  cuanto otro lenguaje aportaba un test nombrado. Repo Node real: campo ausente
  → `1`, hint `False` → `True`.
- **CI verde otra vez.** El job `ruff + package build` estaba rojo en `main`
  (reglas nuevas de ruff: `I001`, `RUF100`, `B007`, `RUF001`); los tests
  siempre pasaron.
- **`CONTRIBUTING.md`** — la barra de evidencia: nada se sube sin antes/después
  contra una superficie de aplicación real, y borrar una tool que la evidencia
  no sostiene es contribución de primera clase.

**Rechazado por evidencia (no reintentar sin datos nuevos):** frenar el picking
de handler de Express en la flecha inline final. Contra un servicio Express real
convirtió `/health` → `healthController.check` (correcto) en un centinela, y no
arregló ningún caso medible. Las rutas reales cierran con error handler
`(err, req, res, next)` o una flecha de logging; la aridad tampoco discrimina
(el tail de logging es de 2). Detalle en `CHANGELOG.md` `[Unreleased]`.

Backlog del barrido: cerrado. Lo que queda es extractor puro y pide
re-extract: prefijo `@RequestMapping` de clase en Spring, y emitir un símbolo
propio para el arrow inline (hoy resuelve al scope contenedor, que es correcto
para impacto pero no distingue dos rutas inline del mismo archivo).

### v0.30.0 / v0.30.1 resumen (referencia)

**Release `v0.30.0` on `main`.** Tests **639**. PyPI `livespec==0.30.1`
(Trusted Publishing via tag). Plugin pin `livespec@0.30.1`.

Highlights: Spec Explorer MCP playground (Try it / `call_tool`); product-only
orphan KPIs (`modules_non_product` vs `modules_truly_orphan`); typed Explorer
`parameters` + Cursor schema honesty (`SchemaCompat` + `param_descriptions`);
OpenSpec self-tree strict-valid; harness test-credit for verified Specs;
AGPL-3.0-only. Surface **27** core + **12** Spec + **5** docs = **44**.

**Public beta** (not 1.0): README + `docs/BETA_DOGFOOD.md` /
`docs/BETA_CHECKLIST.md`. Pin `uvx livespec@0.30.1`.

**OpenSpec self-dogfood:** `validate_openspec(strict=True)` green; SPEC-013
linked; Explorer **13/13 verified**; product orphans **0**.

**Barrido beta 23 repos (31-jul, post-0.30):** re-index con `force` + las 23
tools read-only sobre 13 repos de un grupo `group_db` y 10 repos sueltos.
17 027 símbolos / 30 103 edges / **31,5 s**; 500 llamadas, 21 fallos (4,2 %).
El script y los datos viven **fuera de este repo** (workspaces privados; nunca
commitear paths ni nombres de repos de terceros).

Backlog que salió del barrido (no bloquea):
1. `find_endpoints` devuelve IDs `archivo.js:linea` en rutas call-style; pasarlos
   a `who_calls`/`analyze_impact` da `Symbol not found` (6/23 repos).
2. `find_endpoints(framework="spring")` no puebla `http_method`/`http_path`
   (solo `decorators`), aunque `route_ref` sí resuelve esas rutas.
3. `find_symbol` responde `grouped: true` con `group_db: null`.

### v0.29.0 resumen (referencia)

**Release `v0.29.0`.** Tests **627**. FTS-only (vectors out, mig v19);
`find_legacy_flows`; group_db symbol lookup; Tier-B noise; Express/Hono
default endpoints; audit IMPROVE; Skill/agent polyrepo + legacy-safety.
Docs/beta surface alignment + AGPL relicense landed in the 0.30 cut.

### v0.28.1 resumen (referencia)

**HEAD was `e02c424` / `565718f` pre-0.29.** Checklist flow-group PASS.
P0 group lookup; legacy infra filter; dead_code auto non-python; Spring hint.

### v0.23+ / OpenSpec resumen (referencia)

**Nomenclatura (authoring):** OpenSpec markdown (`openspec/specs/...` con
`### Requirement:` + `#### Scenario:`) es la **SSoT de autoría**. Livespec es el
motor de grafo/trazabilidad debajo. El catálogo nativo `## SPEC-NNN:` queda como
**import-compat / legacy** — no mezclar dialectos en el mismo repo. Ver
`CHANGELOG` `[0.23.0]`/`[0.29.0]`/`[0.30.0]` + Skill `livespec` + `docs/AGENT_PLAYBOOK.md` §5.5.

**Shipped through `v0.23.0` then `v0.29.0`/`v0.30.0`:** OpenSpec round-trip + change
lifecycle + scenario traceability; product/`livespec` command (PyPI package
`livespec`); cross-repo routes + `group_db`; FTS-only curation; Explorer
playground. Pin is **0.30.0**.

<details><summary>OpenSpec batch detail (v0.22→0.23 era)</summary>

Tier 2: `RENAMED` FROM/TO, Purpose round-trip, apply warnings/`dry_run`, mig v18.
Tier 3/4: `livespec` command alias; SPEC-013 dogfooded on self.

</details>


### 3.0 Histórico (post-0.20): cross-project + SDD interop — **landed**

Stream que cerró en v0.23–v0.29 (OpenSpec + `group_db` + route edges +
`find_legacy_flows`). El texto abajo es archivo de batches; el estado vivo
está en §3 arriba y en `docs/BETA_DOGFOOD.md`.


- **Interop OpenSpec** (LANDED `27257a7`): `import_specs_from_markdown`
  auto-detecta e ingiere el formato OpenSpec (`### Requirement:` +
  `#### Scenario:`), con param `fmt` y soporte de árbol `openspec/`. Wedge: ser
  la capa de trazabilidad/grafo debajo de los tools SDD (OpenSpec/spec-kit/
  Kiro), en vez de competirles la autoría.
- **OpenSpec compat FULL — round-trip + change lifecycle (v0.22)** (branch
  `claude/openspec-compatibility-mf1igv`): de "leer OpenSpec" a "ser ciudadano
  de primera clase de un repo `openspec/`". 3 capas:
  1. **Scenarios first-class** (mig v15 `spec_scenario`): el `#### Scenario:`
     (WHEN/THEN) deja de aplastarse en `description`; `get_spec_implementation`
     devuelve `scenarios[]` + `coverage.scenario_count`, `list_specs` cuenta
     `scenario_count`.
  2. **Export** (`export_openspec`) escribe el árbol canónico
     `specs/<capability>/spec.md` (+ `changes/`, `archive/`) — cierra el
     round-trip. **Validate** (`validate_openspec`, mirror de
     `openspec validate --strict`): invariante "todo requirement ≥1 scenario".
  3. **Change lifecycle** (mig v16 `spec_change` + `spec_change_delta`):
     `sync_openspec` importa specs+changes en una llamada (lee `openspec.json`);
     `list_spec_changes`/`get_spec_change` inspeccionan; `apply_spec_change`
     funde deltas en las specs canónicas (ADDED/MODIFIED/RENAMED upsert+activa,
     REMOVED deprecia); `archive_spec_change` cierra. Config
     `[specs].openspec_dir` re-sincroniza tras cada `index_project`.
  4. **Scenario-level traceability** (mig v17 `scenario_symbol`):
     `link_scenario_symbol` liga código/tests a un `#### Scenario:` puntual;
     `get_spec_implementation` devuelve `symbols`+`verified` por scenario y
     `coverage.scenarios_verified`. `_sync_spec_scenarios` pasó a upsert
     (matchea `(spec,name)`) para que los links sobrevivan un re-import.
  - Tools 36→44 (29 core + 12 spec + 3 docs). Módulos nuevos:
     `domain/openspec_{export,validate,changes,discover}.py`;
     `test_openspec_interop.py` (16 tests). **CI verde** en py3.10/3.11/3.12 +
     ruff/build (primer run destapó un bug pre-existente py3.10:
     `datetime.UTC` es 3.11+ → `audit_coverage` nunca grababa snapshot en 3.10;
     fix `timezone.utc`). NOTA: los 62 fallos locales de tree-sitter eran del
     entorno sandbox (CI los corre limpios, 445 passed).
- **P1 grouped DB** (LANDED): `.livespec.toml [workspace] group_db` rutea varios
  roots a una DB compartida (cada uno su `project_id`); un Spec de un repo
  linkea/surface símbolos de otro repo del grupo. `st.resolve_symbol` es
  home-first→grupo. Guard total: sin `group_db` → comportamiento idéntico.
  Tests **413 passed**. Ver `state.py:resolve_symbol/group_project_ids`,
  `config.py:group_db`, `test_grouped_db.py`.
- **P2 route edges cross-repo** (LANDED): segunda mitad del pilar polyrepo — el
  call graph cruza front↔back. `route_ref` (mig v14, `role` client/server),
  extractor emite sitios de ruta (`fetch`/`axios`/`requests` + `@app.get`),
  `_resolve_routes(conn)` matchea por `norm_path` DB-wide → `edge_type=
  'invokes_route'`. Surface: `who_calls.route_callers` / `who_does_this_call.
  invokes_endpoints` por query directa a `symbol_edge` (sin tocar el grafo
  NetworkX). HTTP-only; server TS/Hono + gRPC/colas = P2b. Tests **431 passed**.
  Orquestado con la flota vise (`vise:tester` +8 casos; `vise:reviewer`
  confirmó los contratos y destapó 1 defecto de precisión — routers TS
  `api`/`client` con arg handler se mal-clasificaban como client; arreglado con
  discriminador de arg-handler + test de regresión). Ver
  `indexer.py:_resolve_routes`, `extractors.py:normalize_route_path` +
  `_TS_HANDLER_ARG_TYPES`, `test_route_edges.py`.
- **Dogfood fix — callback edges Python** (LANDED): dogfooding livespec sobre sí
  mismo marcó `index_project._do_reindex` (pasado como `Watcher(on_reindex=…)`)
  como falso dead-code. Causa: el extractor Python no capturaba funciones
  pasadas como argumento (el lado TS sí). Fix: `_emit_py_callback_refs` emite
  `callback_arg` refs, scopeado a callees de registro/scheduling o keywords
  callback-ish (`target=`/`key=`/`on_*`) para no inflar el grafo. Mejora todo
  el call-graph, no solo dead-code. Tests **436 passed**
  (`test_callback_edges.py`). NOTA: el MCP conectado corre `uvx livespec-mcp`
  (v0.20 de PyPI), así que el dogfood en vivo no refleja fixes locales hasta
  publicar — validado por la suite local.
- **Diferido con criterio:** P3 curación (`search`/`embed_chunks` → plugin) — el
  ROADMAP prohíbe curar sin battle-test data. Hardening del resolver: el
  `min_weight` ya modela confianza; exponer peso por-arista en el grafo pide
  cambiar la API de traversal (invasivo, valor incremental).

### 3.1 v0.20.0 — RF→Spec + auditoría exhaustiva aplicada (referencia)

**HEAD:** ver `git rev-parse --short HEAD` (batch de auditoría landeado sobre
`46167e4`). pyproject **0.20.0**. Schema **v13**. Tests **403 passed**
(`-m "not embeddings"`) + tests embeddings-gated (requieren descarga de modelo).
CHANGELOG promovido a `[0.20.0] - 2026-07-16`.

**Qué landeó:** además del hard-cut RF→Spec (nomenclatura + taxonomía `kind`,
migración v11, ids `RF-NNN` preservados), se corrió una **auditoría exhaustiva
de 8 dimensiones** y se aplicaron sus ~50 hallazgos en seis tandas commiteadas
(un commit por fase, direct push a `main`):

- **P1 — packaging & boot** (`5a97979`): arreglado el build (`force-include`
  duplicado hacía fallar `uv build`); `LIVESPEC_PLUGINS` ya no brickea
  `tools/list`; instrumentación no enmascara el error de workspace; el prompt
  `agent_playbook` se empaqueta y carga vía `importlib.resources`; CLI
  `--version` + errores shaped; `fastmcp<4`; drop `rank-bm25`; CI con job
  `ruff + uv build` que verifica el contenido del wheel.
- **P2 — storage & concurrencia** (`80e0d95`): migraciones transaccionales;
  `UNIQUE(project.root)` (mig v12) + dedup; cache del grafo keyed en runs
  *finished* con `files_changed>0`; watcher aislado en su propia conexión +
  parado al evictar; `needs_reextract` limpiado solo tras éxito; `busy_timeout`;
  `chunk_au` con guard (mig v13); schema.sql completo (converge fresh↔migrado).
- **P3 — correctness domain** (`3cf740f`): BFS real (deque) en
  `descendants_within` + 2 copias inline; `upsert_chunks` sin DELETE-en-loop;
  parse fallido preserva símbolos+links; extractor Python entra a
  if/try/except/with/for/while/match; cadenas de métodos; `.tsx` en
  imports/visibility; boundary del verbo en matcher; md_specs fences +
  meta anclada; watcher path relativo; body_hash con whitespace en strings.
- **P4 — capa tools** (`4e2439d`): `analyze_impact(spec)` paginado; git
  `--name-status -M` (renames) + `--end-of-options`; `IN(...)` chunkeado;
  middleware que shape-a errores de workspace; `create_spec` MAX+1 +
  IntegrityError shaped; `ORDER BY` estable en cursores; endpoints por spec
  intersectados; `agent_scratch` leíble vía `quick_orient`; snapshot solo en
  fetch primario; `list_specs.has_implementation` en SQL; find_symbol LIKE
  escape + limit clamp; bulk_link valida relation/confidence.
- **P5 — performance** (`9d3ad2a`): rebuild_chunks preserva embeddings +
  lee cada archivo una vez + limpia vectores huérfanos; coverage O(V+E)
  (BFS invertido); PageRank cacheado en GraphView; find_dead_code parsea
  cada archivo una vez (cache por (path,mtime)); sqlite-vec una vez por conn.
- **P6 — tests & docs** (este commit): correcciones de superficies que mentían
  (CLAUDE.md workspace obligatorio, AGENT_PLAYBOOK tool gated, README install
  desde source, fastmcp.json, conteos de tools 24/stale); tests de contrato
  faltantes (update_spec/delete_spec CRUD, resiliencia a source malformado,
  C4 e2e); ROADMAP §0 con el pilar **cross-project** (features polyrepo/
  microservicios — ver ROADMAP para el plan en 2 fases A/B).

**Regresiones cubiertas:** `tests/test_audit_p{3,4,5,6}_regressions.py` +
adiciones a `test_migrations.py`, `test_plugin_visibility.py`,
`test_plugin_autoload.py`.

**Gate:** `uv run pytest -q -m "not embeddings"` → **403 passed**.

**Pendiente:** tag `v0.20.0` + GitHub release (bookkeeping); opcional PyPI.

### v0.20.0 batch RF→Spec (referencia — landeó en `46167e4` y anteriores)

Hard cut RF→Spec en 6 fases mecánicas (schema v11, 13 tools renombradas,
payload keys, explorer, tests, docs). Ids `RF-NNN` preservados; specs nuevas
generan `SPEC-NNN`. Ver CHANGELOG `[0.20.0]`.

### v0.19.0 resumen (referencia)

**HEAD:** `67d2f14` on `main`. Tests **368 default + 3 embeddings = 371**. Schema **v10** (`agent_scratch`
table). pyproject **0.19.0**. Core menu **24** tools always visible
(incl. `import_requirements_from_markdown` + `export_explorer`); full menu
**36** after index on repo with RFs + explorer.

Lo que landeó en v0.19.0:
- **FastAPI** — HTTP route extraction, Explorer Try-it, `fastapi init`,
  runtime `enable_explorer`, autodetect on index.
- **Agent tooling** — `grep_in_indexed_files`, `agent_scratch`×2,
  `payload_warning`, optional `[agent] log_calls`.
- **Brownfield RF** — `[requirements].sync_from` / `links_seed`,
  `scripts/sync_livespec_rfs.py`, duplicate-spec warnings, pytest fixture
  filter in `find_endpoints`, `embed_chunks` install hints, Explorer
  integration-only badge.
- **CI** — `.github/workflows/livespec-pr-comment.yml` + `pr_diff_impact.py`.

**Gate:** `uv run pytest -q -m "not embeddings"` → **368 passed**.

**Post-release:** reconectar MCP (`/mcp`); battle-test PR workflow; omitido PyPI.

### v0.18.0 resumen (referencia)

Tests **342 default + 3 embeddings = 345**. Schema **v9** (sin migración nueva).
pyproject **0.18.0**. Tool surface **33** (19 core + 10 RF plugin + 4 docs) —
**menú dinámico**: ~19 visibles en `tools/list` hasta el primer `workspace=`;
tras index en repo con RFs + bundle explorer → 33 en la sesión MCP.

Lo que landeó (batch post-v0.17, sin tag previo en remoto):
- **A Plugin visibility** (`plugin_visibility.py` + `PluginVisibilityMiddleware`):
  plugins registrados al boot; `on_list_tools` / `on_call_tool` filtran por
  `detect_active_plugins` (filas `rf`/`doc`, bundle `.mcp-docs/explorer/`, o
  `LIVESPEC_PLUGINS`). Session cache del último `workspace=`.
- **B Explorer serve + FastAPI mount** (`src/livespec_mcp/explorer/`):
  `mount_explorer(app)`, `create_explorer_host_app`, autowire en `main.py`;
  CLI `livespec-mcp explorer serve` → `http://127.0.0.1:8765/explorer/`
  (`/` y `/index.html` → redirect). Router: landing Overview, tab API Swagger,
  hash fallback para `file://` y `python -m http.server` en raíz del bundle.
- **C Search hardening** (`domain/rag.py`): vector lane → FTS-only si embedder
  cae; snake_case (`index_project` → `index OR project`); `IntegrityError` en
  bm25 absorbido.

**Gate:** `uv run pytest -q -m "not embeddings"` → **342 passed**. Dogfood
livespec-mcp: explorer UI OK en `:8765/explorer/`; plugin menu 19→33 in-process;
`search("index_project")` 15 hits tras fix FTS.

**HEAD:** `fa74c08` (v0.18.0 — plugin menu + explorer serve + search FTS fix)

**Pendiente:** battle-test en repos ajenos; Cursor no refresca `tools/list` sin
reconectar MCP; tests `embeddings` dependen de ONNX/hub en esta máquina.
Epic B (C#/Kotlin/Swift, PyPI) sin cambios.

### v0.17.0 resumen (referencia)

Tests **330 default + 3 embeddings = 333**. Schema **v9** (migración
`rf_coverage_snapshot`). pyproject **0.17.0**. Tool surface **33** (sin
tools nuevos; `audit_coverage`/`export_explorer`/`index_project` ganan
campos/params). Ejecutado vía super-workflow the workflow runner
`rf-explorer-dynamic-v017-graph` con **wave de 3 agentes en paralelo**
(file-disjoint) + cola serial del explorer.

Lo que landeó (5 features):
- **A Diff→RF "Changes"**: `export_explorer(base?,head?)` + nuevo
  `compute_diff_rf_impact` (reusa `git_diff_impact` vía helper compartido).
  Vista "qué toca esta rama" + cobertura de los RFs tocados.
- **B drill-down**: `rf_coverage` gana `uncovered_symbols[]` (símbolos sin
  test); explorer los lista por RF.
- **D trend**: tabla `rf_coverage_snapshot` (mig v9) + `storage/trends.py`;
  `audit_coverage` graba snapshot **deduped-on-change** (abrir el explorer
  N veces NO infla la serie); explorer dibuja sparkline.
- **E freshness**: `index_project(explorer=)` auto-regenera el bundle si
  existe; `explorer_regenerated` en payload.
- **F links reproducibles**: `docs/requirements/livespec-rf-links.json` +
  `scripts/apply_rf_links.py`.

Paralelización: 3 agentes disjuntos (analysis+trends+db / indexing /
docs+scripts) + 1 serial (explorer.py es un solo archivo, no se parte).

**Gate:** `validate` usa solo `lint_pass`; `tests_pass` de the workflow runner falsea fuera
del venv `uv` (corrido manual: 330 passed). E2E: data.json con
changes/trend/uncovered, trend deduped a 1 pt, sin `var()` leak.

**Pendiente (histórico):** push acumulado v0.15–v0.17 — superseded por v0.18.
Epic B (extractores C#/Kotlin/Swift, PyPI) = workflow aparte.

### v0.16.0 resumen (referencia)

Tests **316 default + 3 embeddings = 319**. Schema **v8**. pyproject
**0.16.0**. Tool surface **33** (sin tools nuevos; `audit_coverage` y el
explorer ganan cobertura derivada). Ejecutado vía super-workflow the workflow runner
`rf-coverage-v016-graph` (orient→design→implement-coverage→
implement-explorer→reproducible-rfs→test→validate→commit), waves a
subagentes `general-purpose`.

Lo que landeó:
- **Cobertura de tests por RF DERIVADA del call-graph**
  (`compute_rf_test_coverage` en `tools/analysis.py`): un símbolo
  `implements` cuenta como testeado si el cono forward de un símbolo-test
  lo alcanza (depth 3) ∪ link explícito `relation='tests'`.
  `audit_coverage` gana `rf_coverage[]` + `avg_test_coverage` +
  `rfs_with_any_test_coverage` (additive; legacy
  `rf_test_coverage`/`rfs_with_test_coverage` intactos). Hace que la
  cobertura RF funcione **automática** en cualquier proyecto cuyos tests
  llamen al código directo; links explícitos = fallback para indirección
  (MCP `Client`).
- **Explorer**: meter "Test coverage %" (distinto de "Link confidence %")
  + badge `coverage_source` + KPI Avg test coverage; `dev_state=verified`
  cuando ratio>0. Una sola call a `compute_coverage`.
- **Self-RFs reproducibles**: `docs/requirements/livespec-rfs.md` (12 RFs,
  import verificado 12/12) + `livespec-rf-links.md` (nota del link-seed).
- **Explorer polish** (post-corte): fix Mermaid (classDef rechazaba
  `var()` de CSS → colores resueltos en build-time + labels escapados);
  endpoints 57→50 (fixtures pytest separados, agrupados Tools/Resources/
  Prompts estilo Swagger + copy-call por endpoint). 318 tests.

Dogfood en vivo (MCP recargado): export_explorer → dashboard **10 verified
/ 2 implemented, avg_test_coverage 0.48**, RF-001/002/003
`coverage_source: derived` (el call-graph detecta cobertura real de
extractors/resolver/indexing). Spread honesto: 2 RFs sin cobertura.

**Gate:** `validate` usa solo `lint_pass` (ruff `.`); el `tests_pass` de
the workflow runner falsea fuera del venv `uv` (corrido manual: 316 passed).

**Pendiente:** `git push origin main && git push origin v0.16.0` (sandbox
bloquea) + release. Epic B (extractores C#/Kotlin/Swift, PyPI) = workflow
aparte.

### v0.15.0 resumen (referencia)

Tests **312 default + 3 embeddings = 315**. Schema **v8**. pyproject
**0.15.0**. Tool surface real: **33 (19 core + 10 RF + 4 docs)** — nuevo
`export_explorer`. Ejecutado vía workflow the workflow runner `rf-explorer-v015-graph`
(orient→fixes→dogfood→explorer→test→validate→commit), delegando waves a
subagentes `general-purpose`/`livespec-specialist`.

Lo que landeó (Epic A, F1–F6):
- **`export_explorer`** (`tools/explorer.py` + registro en
  `plugins/docs.py`): genera `.mcp-docs/explorer/{data.json,index.html}`
  — viewer estático Swagger-like **organizado por RF** (spine +
  símbolos/firmas + endpoints + topología Mermaid + gaps). Proyección
  pura del RF graph, sin servidor, abre desde `file://`. Reusa
  `compute_endpoints`/`compute_coverage` extraídos a nivel módulo en
  `tools/analysis.py` (sin duplicar SQL). **Reframe PO-first**
  (post-corte, mismo release): dashboard de estado + `dev_state`
  derivado de evidencia (not_started→in_progress→implemented→verified)
  + lenguaje llano con detalle técnico colapsado + flag declarado-vs-
  evidencia. `coverage` etiquetado honesto como "link confidence" (no
  es cobertura de tests). "Verified" sale 0 hasta linkear tests↔RF
  (mismo gap que el caveat F6). data.json gana `dashboard` + `dev_state`.
- **F4** `find_endpoints` detecta tools del plugin RF (union de
  decorator-aliases, mismo mecanismo que `find_dead_code`).
- **F5** `propose_requirements_from_codebase` default `module_depth` 2→3
  (no colapsar paquetes profundos).
- **F6** `find_orphan_tests` payload con campo `caveat` (cuenta es cota
  superior por la indirección FastMCP `Client(mcp)`).
- **F1 dogfood**: livespec tiene sus **12 RFs propias** (59 symbol links,
  12 edges `requires`) — el dataset demo del explorer.
- Portabilidad: `__init__` guarda `version()` (PackageNotFoundError →
  `0.0.0+source`) + `pytest pythonpath=["src"]`.

**Gate del workflow (nota honesta):** el validator `tests_pass` del nodo
`validate` quedó en **falso-negativo** — corre `pytest` fuera del venv
`uv` (sin deps networkx/fastmcp), no es regresión de código. Suite real
`uv run pytest -m "not embeddings"` = **312 passed**, `ruff check .`
limpio, E2E visual del `index.html` confirmado (firefox headless render
del artefacto real: 12 RFs / 57 endpoints). Fix real del gate =
configurar el validator the workflow runner para `uv run pytest` (fuera de este repo).

**Pendiente:** `git push origin main` (sandbox bloquea — correr con `!`) +
`git tag v0.15.0` + `gh release`. Epic B (extractores C#/Kotlin/Swift,
PyPI publish) = workflow aparte (multi-día / creds del usuario).

### v0.14.0 resumen (referencia)

Tests **305 default + 3 embeddings = 308**. Schema **v8**. Tag
**v0.14.0** (incluye batch v0.13 que nunca se taggeó). pyproject
0.14.0. Tool surface real: **32 (19 core + 10 RF + 3 docs, plugins
always-on)** — conteos README/CLAUDE.md corregidos al cortar.

Sesión 2026-06-12, fases landeadas:

| Fase | Qué |
|---|---|
| P0 (`877d878`) | gitignore-aware indexing vía `pathspec` (root+nested+negations, precedencia git real, dirs podados sin descender). 7 tests |
| P1 (`b6471f4`) | `.livespec.toml` per-repo: `[index] ignore` (outranks .gitignore), `languages` allow-list, `max_file_bytes`. Carga fresh por call, malformed → ValueError accionable, payload `repo_config` echo. Dep `tomli` sólo py<3.11. 12 tests |
| P2 (`2ca24c4`) | CLI headless: `livespec-mcp index <path> [--force\|--embed]` / `status <path>` → JSON stdout, errors stderr rc=1. Mismo pipeline que el tool (`run_index_pipeline` compartido en tools/indexing.py). Sin args / `serve` = server stdio intacto. 4 tests |
| P3 (`7e4c725`) | Robustez: **fix resources rotos desde v0.12** (get_state() sin workspace lanzaba — ahora MRU fallback + mcp_error shape en JSON resources); langs sin extractor (cpp/kotlin/etc.) skipped + reportados en `languages_unsupported` en vez de filas 0-symbol silenciosas; batch INSERT..SELECT para restore de RF links manuales; watcher loggea reindex failures (`livespec.watcher`) en vez de tragar. Índices multi-col NO agregados — sin evidencia bench de necesidad. 4 tests |
| P4 (`b0e7f37`) | Closure-capture TS/JS/TSX+Rust: `_treesitter_used_nested_def_names` (mirror tree-sitter de `_used_nested_def_names`), wired al loop non-Python de find_dead_code. Go exento by design (sin nested fns nombradas). Cierra item abierto desde v0.8. 5 tests |
| P5 (`0608630`) | Embed cache persistente: fastembed default era `$TMPDIR/fastembed_cache` (tmpfs → re-download 200MB por reboot) → ahora `~/.cache/livespec-mcp/fastembed` XDG compartido, `FASTEMBED_CACHE_PATH` gana. `Settings.models_dir` muerto eliminado. README: hook PostToolUse freshness + docs cache embeddings. bench/ ruff clean (estaba fuera del lint) |
| P6 | Django re-validation (Arch box, Ryzen 4800H): **dead-code 824→514→348→344** (serie cerrada, sin regresión); cold 148s→54s, partial 7.0s→**1.4s** (targeted resolver confirmado shipped — backlog item 14 era stale), edges 1.05M→465K (machine-independent: precisión scoped), DB 124→71MB, RSS 609→294MB. Baseline `bench/results-large.json` actualizado. ROADMAP §6b: LLM sampling RF refinement DROPPED definitivo (agente caller ya tiene LLM) |

Sesión 2026-06-12: **gitignore-aware indexing** (backlog item 5, tier P1
"fricción uso personal"). `_iter_files` ahora carga `.gitignore` root +
nested vía `pathspec` (`GitIgnoreSpec`, dep nueva `pathspec>=0.12`),
encima del baseline `DEFAULT_IGNORES`. Semántica git: el `.gitignore` más
profundo con opinión gana (`check_file` tri-state para negations
cross-file); dirs ignorados se podan sin descender (re-includes adentro
no aplican — misma limitación que git documenta). Workspaces sin
`.gitignore` = comportamiento idéntico pre-v0.14. 7 tests nuevos
(`tests/test_gitignore.py`). Watcher no tocado: delega a `index_project`,
que ya filtra.

**Backlog 2026-06-11 (18 puntos, 4 tiers): CERRADO.** Items 1-12 y 14-18
shipped o resueltos (14 ya estaba implementado — stale); item 13 (LLM
sampling) DROPPED con racional en ROADMAP §6b. Copia del backlog en
memoria persistente del proyecto
(`~/.claude/projects/-home-rixmerz-Projects-mcp-livespec-mcp/memory/backlog-v013-analysis.md`).
Post-release dogfood vía the workflow runner proxy sobre livespec mismo: index 111
files/723 symbols, dead-code 0, quick_orient/who_calls/analyze_impact/
search OK en código nuevo de la sesión. Gap encontrado y cerrado:
`find_symbol` con typo devolvía `{matches: []}` pelado → ahora incluye
`did_you_mean` (2 tests).

**Pendiente próximo:** ninguno comprometido. Candidatos naturales para
v0.15: extractores C/C++/C#/Kotlin/Swift/Scala (hoy reportados como
`languages_unsupported`), battle-test de la superficie v0.14 en repos
personales reales, PyPI publish si se quiere instalación sin clone.

### v0.13 resumen (referencia)

Sesión 2026-06-11 (Arch box `<repo>`):

| Fase | Commit | Qué |
|---|---|---|
| P0 | `4046578` | Dual-decorator alias detection (`agentic_tool = mcp.tool if X else _noop`): `_entry_point_decorator_aliases` AST scan per-file, union project-wide; protege también el branch no-ganador del IfExp (`_noop_decorator`). 4 fns genuinamente muertas BORRADAS (graph.subgraph_edges + watcher get/unregister/all — huérfanas de drops v0.8). **find_dead_code sobre livespec: 22 → 0** sin trucos |
| chore | `d62ad32` | ruff clean tests/ (20 autofixes + 2 dead assignments) — `ruff check src/ tests/` = 0 |
| P1 | `84ee868` | `_ts_node_decorators`: TS/JS/TSX `decorator` nodes (en el nodo, en export_statement wrapper, y como preceding-siblings de members en class_body) + Java `annotation`/`marker_annotation` en `modifiers`. Migración v8 flaggea needs_reextract para backfill |
| P2 | `ff22791` | Spring Boot + Angular: `_ENTRY_POINT_DECORATOR_LASTSEG` += stereotypes/mappings Spring + decorators Angular; presets `find_endpoints(framework='spring'\|'angular')`; find_dead_code protege métodos de clases template-bound (Component/Directive/Pipe — HTML invisible al indexer) + lifecycle hooks ngXxx |
| P3 | `63e621e` | Hono: refs `callback_arg` extract-time (handlers nombrados pasados a get/post/.../on/use), scan TS/JS module-level en find_dead_code (mirror de `_runtime_registered_names`), `find_endpoints(framework='hono')` con method+path+handler resolution. Cubre Express-style gratis |
| P4 | `4b05c5e` | Docs sync: CLAUDE.md arch real, HANDOFF estado, README frameworks |

### v0.12 resumen (referencia)

**Tag:** `v0.12.0`. Tests **257/257** default + embeddings (suite unified). Schema v7 (sin migración nueva).

### v0.12 P2 batch 2 (banner-with-text skip + force-reindex preserves manual links — 2026-05-02)

Reportes desde sesión real post-deploy del batch 1:

- **Banner con texto interno no era atrapado.** `_is_separator_only`
  sólo cubría líneas 100% separator (`// ---`). Banners reales tipo
  `// --- Token Management ---` o `// ============= Tool Execution
  Dispatcher =============` pasaban el filtro y ganaban
  `docstring_lead` sobre el JSDoc inmediato. Fix: `_BANNER_RE`
  matchea `^[-=*#_~]{2,}.+?[-=*#_~]{2,}$` (texto wrapeado en ≥2
  separator chars a cada lado).
- **`index_project(force=True)` borra rf_symbol manuales.** El
  cascade `DELETE FROM symbol → rf_symbol` no distinguía source y
  destruía links de `bulk_link_rf_symbols` sin warning. Fix:
  snapshot pre-extract de filas `source != 'annotation'`, restore
  post-`scan_annotations` por (rf_id, symbol_qname). Anotaciones NO
  se snapshotean (se re-derivan de docstrings frescos para reflejar
  edits reales). Payload de `index_project` gana
  `manual_links_restored`.

### v0.12 P2 batch 1 (JSDoc + bulk-link agentic + audit_coverage gap — 2026-05-02, commit `962a00a`)

Triggered por feedback de sesión real sobre proyecto TS/Node con 137
archivos backend + 218 frontend, 60 RFs registrados y 34 anotaciones
in-source en JSDoc — `index_project` reportaba `rf_links_created: 0`
porque el extractor TS dejaba `docstring=None` hardcoded. Tres fixes:

- **`domain/extractors.py:_ts_leading_doc_comment`** (nuevo): para
  declaraciones JS/TS/TSX, lee el comentario inmediatamente anterior
  al nodo (subiendo a través de `export_statement` /
  `export_default_declaration`), soporta JSDoc bloque `/** ... */` y
  runs de `//` o `///`. Se invoca desde `emit_symbol`. Ahora cualquier
  `@rf:RF-NNN` arriba de una función TS lo captura
  `scan_annotations` automáticamente sin cambios al matcher.
- **`tools/requirements.py:bulk_link_rf_symbols`**: cambia
  `@mutation_tool` → `@agentic_tool`. Ya no depende del plugin
  `livespec-rf` para registrarse — siempre disponible como escape
  hatch para configs/SQL/YAML o lenguajes sin extractor de
  anotaciones. Plugin actualizado para no re-registrar.
- **`tools/analysis.py:audit_coverage`**: nuevo bucket
  `modules_unsupported_language` separa archivos cuyo lenguaje no
  tiene extractor de anotaciones (todo lo que no sea Python / JS / TS
  / TSX hoy). Se extraen de `modules_truly_orphan`. Driven por
  `domain/languages.py:ANNOTATION_SUPPORTED_LANGUAGES`.
- **Tests**: `test_ts_jsdoc_docstring_populated` cubre JSDoc bloque,
  multi-RF y `//` line comments. `test_rf_plugin_registers_mutation_tools`
  actualizado para reflejar el nuevo split (10 mutation tools en plugin,
  bulk_link en agentic).

### v0.12 P1 (RAG wire — 2026-05-01, post-v0.11.0)

La capa RAG en `domain/rag.py` (chunking AST-aware, FTS5, sqlite-vec
opcional con RRF) estaba **completamente implementada pero orphan**:
ningún tool la exponía y cero tests cubrían el extra `[embeddings]`.
Sesión wire-up end-to-end:

- **`tools/indexing.py`**: `index_project` ahora corre
  `rebuild_chunks` después del pase de symbols/edges (idempotente,
  skip cuando no cambian files y ya hay chunks). Flag nuevo
  `embed=False` dispara `embed_pending` para activar lane vectorial
  sin segunda call. Payload gana `{chunks, embeddings}`.
- **`tools/search.py`** (nuevo): expone `search(query, scope, limit)`
  sobre `hybrid_search`, más `embed_chunks()` para población explícita
  de vec0. Validación con `mcp_error()`. Capability de la lane
  reportada en respuesta (`lanes.fts5`, `lanes.vector`).
- **`server.py`**: registra el módulo nuevo.
- **`tests/test_search.py`** (nuevo, 9 tests): 6 default (chunks
  populados, FTS keyword hit, scope=code filter, empty-query error,
  no vec_chunks sin embed, skip on second index) + 3
  `@pytest.mark.embeddings` (vec0 populado, hybrid lights up
  `lanes.vector` para query semántica "authenticate credentials",
  embed_chunks idempotente).

**Sin schema migration. Sin nuevas dependencias** (fastembed +
sqlite-vec siguen opt-in via `pip install -e ".[embeddings]"`).

Commits:
- `b7fbf72` v0.12 P1: wire RAG layer
- `f161492` chore: harden .gitignore for personal + RAG artifacts
  (`.claude/` whole, `*.onnx|safetensors|gguf|bin|pt|npy|npz`,
  `local_cache/`, `models/`, `.fastembed_cache/`, `.huggingface/`,
  dumps debug, editor/OS files)

### v0.11.0 (cortado, referencia)

**Tag:** `v0.11.0`. Tests **237/237**, schema v7.

v0.11 entera ejecutada en una sesión post-v0.10.0 con **paralelización
de subagentes en worktrees aislados** (primera vez en este repo):

- **P0** (Opus, secuencial): bundler/build dir filter (`_fresh/`,
  `dist/`, `build/`, `.next/`, `out/`, `node_modules/`,
  `.svelte-kit/`, `target/`, `__pycache__/`, `.turbo/`, `.vite/`,
  `.cache/`, `.parcel-cache/`) + minified suffixes. Helper
  `_is_bundler_output_path` aplicado en `find_dead_code` +
  `compute_project_overview.top_symbols`. Bug #18 cerrado.
- **P1** (Sonnet, worktree paralelo): TS framework entry-points.
  Helpers `_ts_framework_entry_point_kind` + `_is_ts_framework_entry_point`
  detectan Fresh `islands/`, Next pages router + app router,
  SvelteKit `routes/+page.*`, Remix `app/routes/`. `find_endpoints`
  literal extendido con `nextjs`/`fresh`/`sveltekit`/`remix`. Bug #19
  cerrado. (32 tests nuevos)
- **P2** (Sonnet, worktree paralelo): JSX element refs como edges.
  `_ts_collect_calls` walks `jsx_opening_element` +
  `jsx_self_closing_element`, emite refs a componentes uppercase
  (skip lowercase HTML). Member-expression `<Form.Field />` resuelve
  a `Form` (leftmost). Bug #20 cerrado. (10 tests nuevos)
- **P3** (Sonnet, worktree paralelo): runtime registration name
  protection. Helper `_runtime_registered_names` walks AST por
  registration verbs (`register`, `register_lookup`, `connect`,
  `add_middleware`, `subscribe`, `on`, `use`, +9 más). Cierra el
  último bucket grande de Django false-positives. (13 tests nuevos)

**Wire-validation contra `SpeedRunners-landing` (Deno Fresh, 217 files /
2532 syms / 16567 edges):**
- `find_dead_code` default: **974 → 0** (−100%)
- `find_dead_code` con `include_non_python=True`: 974 → 118 (−88%)
- `find_endpoints(fresh)`: **340 entry points** detectados (era 0)
- `top_symbols` de `_fresh/` o `dist/`: **0/20** (era 18/20)

**Workflow win**: P1 + P2 + P3 implementados en paralelo por 3
sonnet subagents en worktrees aislados (~14 min concurrente vs ~20+
min secuencial en Opus). Token spend total subagents ~236k. Merge
sequential cherry-pick a main, 1 conflicto trivial en CHANGELOG
[Unreleased] resuelto a mano.

### v0.10 resumen (referencia rápida — ya cortado)

**Tag:** `v0.10.0`. Tests **179/179**, schema v7.

v0.10 entera ejecutada en una sesión post-v0.9.0:
- **P0** README lift (Django numbers above fold + 30-sec tour + AGENT_QUICKSTART link)
- **P1** `__init__.py` re-exports + `__all__` protect from dead-code
- **P2** session 05 (Deno Fresh, TS/TSX/JS) — 5/5 profiles cubiertos

**Wire-validation contra Django 5.1.4 (post-`b74e69a`):**
- `find_dead_code`: 514 → **348** (−32% additional, **−58% cumulative desde v0.8 baseline 824**)
- Classes: 251 → 164, methods: 74 → 24, functions: 189 → 160

**Bugs nuevos abiertos (TS-specific, session 05):**
- #18 `top_symbols` polluted by bundler dirs (`_fresh/`, `dist/`, `.next/`, `out/`, `build/`)
- #19 `find_dead_code` over-reports en Fresh apps (islands not entry-points)
- #20 JSX element refs no son call-graph edges

---

## 3a. Plan v0.12 (próxima sesión, post `/clear`)

Para reanudar: `leé HANDOFF.md y continuá`. v0.11 cerró todos los
bugs de session 05; **v0.12 P1 ya merged** (RAG wire). **Para v1.0
quedan items menores + polish + corte de tag v0.12.0**:

### Pendiente para cortar v0.12.0 — COMPLETADO 2026-05-29

- ✅ Bump `pyproject.toml` a `0.12.0`
- ✅ Promover `CHANGELOG [Unreleased]` → `[0.12.0] — 2026-05-29`
- ✅ Fix docstring bug: `index_project` f-string → `__doc__` assignment
- ✅ `ruff check src/` → 0 violations (18 auto-fixed + 7 manual)
- ✅ `fastmcp>=3` tightened in pyproject; `uv lock --upgrade` ran
- ✅ `__init__.__version__` wired via `importlib.metadata.version`
- ✅ `git tag -a v0.12.0` (local only; push when ready)
- ✅ `git push origin main` + tag `v0.12.0` en remoto (verificado 2026-06-11)

### Opciones para v0.12 (elegir 1-2 según tiempo)

0. **v0.12 P0 quick win — dual-decorator alias detection** (½ día,
   bug surfaced post-v0.11 force-reindex). El patrón
   `agentic_tool = mcp.tool if X else _noop_decorator` introducido
   en v0.8 P3.4 (plugin framework) rompe `_has_entry_point_decorator`
   porque el last-seg de `@mutation_tool(...)` no está en
   `_ENTRY_POINT_DECORATORS`. Resultado: 22 false-positives en
   `find_dead_code` sobre el propio livespec-mcp (mostly
   `tools/requirements.py register.*` fns + watcher helpers + rag
   helpers). **Fix**: extender el matcher para reconocer aliases
   asignados a `mcp.tool` (AST scan a nivel de función `register()`
   collecting `name = mcp.tool` o `name = mcp.tool if ... else _noop`
   patterns, agregar al set de entry-point decorators per-file).
   No es regresión de v0.11 — es deuda pre-existente que se
   surfaceó al hacer force re-index. Cierra a 0.

1. **v0.12 wire-validation re-run sobre Django** (½ día, alto valor
   de proof-point). v0.10 reportó 348 candidates Django. v0.11 P3
   debería bajarlo más (Field.register_lookup + signal.connect +
   middleware patterns). **Tarea**: re-correr `find_dead_code` sobre
   Django 5.1.4, comparar contra el 348 baseline, actualizar tabla
   en CHANGELOG/README. Si baja a <200, mencionarlo en headline.
   **Por qué primero**: cierra el loop de proof-points serie
   `824 → 514 → 348 → ?` y nutre la pitch de v1.0.

2. **v0.12 closure-capture detection en non-Python** (1-2 días). El
   per-file `_used_nested_def_names` Python-only se podría portar a
   TS/Rust/Go. TS arrow callbacks son el patrón más común
   (`button.on("click", _handler)` con `_handler` definido en el
   parent scope). Mirror la heurística de v0.8 P2 fix #11 para
   tree-sitter. **Riesgo bajo**: la lógica ya existe, sólo es port.

3. **v0.12 plugin auto-detect refinement** (½ día). El framework
   actual chequea `rf` y `doc` table count una vez en startup.
   Agregar `LIVESPEC_PLUGINS=` env var override más explicit en
   docstring + tests para los 4 estados (rf only / docs only /
   both / neither). Cierre menor, baja prioridad.

4. **v0.12 demo asciicast** (½ día, marketing). Grabar 60s flow:
   `index_project` → `propose_requirements_from_codebase` →
   `link_rf_symbol` → `audit_coverage`. Embed en README.

5. **v0.12 LLM-assisted RF refinement** (1-2 días, optional feature
   sobre `propose_requirements_from_codebase`). Toma los proposals
   y los pasa al cliente vía MCP sampling para mejorar título +
   descripción. **Diferido desde v0.7+** — sigue siendo opcional.

### Camino sugerido a v1.0

- v0.12: items 1 + 2 (Django re-validation + closure-capture port)
- v0.13: item 3 + 4 (plugin polish + demo)
- v1.0: docs lift + CHANGELOG resumen + tag. No nuevas features.

**Para v1.0** falta:
- Django re-validation (v0.11 effect documentado)
- Closure-capture cross-language (opcional pero alto valor)
- Demo asciicast (UX-level)
- Posible CHANGELOG resumen ejecutivo "lo que cambió desde v0.1"

### Estado previo: v0.9.0 cortado. Default surface 16 tools + 14 plugin = 30 max activos.

**Tag:** `v0.9.0`. Tests **175/175**, schema v7.

v0.9 entera ejecutada en una sesión post-v0.8.0:
- **P0** targeted resolver walk (perf, 25→12ms partial en `requests`)
- **P1** session 04 battle-test (Django, 16 calls, 5 bugs surfaceados)
- **P2** pagination en `who_calls`/`who_does_this_call`/`analyze_impact`
- **P3** `min_weight=0.6` filter en traversal tools (mute fan-out)
- **P4** Django dead-code accuracy (skip JS, dotted-strings, Meta inner)
- **P5** Django CBV detection en `find_endpoints`
- **P6** drop deprecated `get_index_status`

**Wire-validation contra Django 5.1.4 (40K syms):**
- `find_dead_code`: 824 → **514** (−38% noise)
- `find_endpoints(django)`: 20 → **162** (+8×)

### Estado previo: v0.8.0 cortado. Default surface 17 tools + 14 plugin = 31 max activos.

**Último commit antes del tag:** `<bump-commit>` (P7 release prep).
**Tag:** `v0.8.0` apunta a este commit. **Tests 157/157**, schema v7.

v0.8 entera ejecutada en una sesión: P0 quick wins → P1 instrumentation
→ P2 (3 battle-test sessions / 11 bug fixes) → P3a alias drop → P3b
prep → P3.1 plugin framework → P3.2 deprecate get_index_status → P3.3
drop 8 tier-4 tools → P3.4 RF mutation plugin → P3.5 docs plugin → P4
pitch alignment → P7 release.

Sesión 2026-05-01 ejecutó **batch completo P2**: 3 sesiones de
battle-test reales (the workflow runner + livespec-mcp + demo-app) → surfacearon
11 bugs → fixados todos → wire-validated 100% noise reduction en
`find_dead_code` sobre el propio livespec (18 → 0 false positives).

### Commits del batch v0.8 (cronológico)

| Phase | Commit | Cambio neto |
|---|---|---|
| **P0** quick wins | `0db55a8` | +4 tools agentic en `tools/analysis.py` |
| **P1** instrumentation | `bab89ba` | middleware logging + JSONL |
| **P2** prep | `fd6b39c` | analyzer + skeleton de data doc |
| **P3a** alias drop | `08315bc` | −4 aliases v0.6 deprecated (breaking) |
| **P3b prep** | `770be36` | resource paridad + helpers compartidos |
| **P2 session 01** | `f7384e0` | the workflow runner exploration → bugs #1-3 surfaced |
| **P2 fixes #1-3** | `bc8ba1d` | resolver fan-out + entry-point flag + structural-noise |
| **P2 session 02** | `44a0dc4` | livespec refactor → bugs #4-7 surfaced |
| **P2 session 03** | `af4f3db` | demo-app RF flow → bugs #8-10 surfaced |
| **P2 fixes #4-10** | `c14e8d4` | find_dead_code accuracy + audit_coverage + git_diff filter + propose tests-skip |
| **P2 fix #6 cross-file** | `a8daf0d` | middleware classes registered cross-file |
| **P2 wire validation** | `e40a693` | 18→1 dead-code false positives doc |
| **P2 fix #11 closures** | `2956bcc` | nested-fn closure callback detection |
| **HANDOFF P2 closeout** | `b564c70` | doc update post P2 |
| **P3.1** plugin framework | `db05bde` | tools/plugins/ + auto-detect, no-breaking |
| **P3.2** deprecate get_index_status | `9278eb2` | payload marker + once-stderr warning |
| **P3.3** drop 8 tier-4 tools | `590a52b` | search/watcher trio/list_files/get_symbol_info/get_call_graph/rebuild_chunks |
| **P3.4** RF mutation plugin | `cdca0e9` | 11 tools moved to livespec-rf plugin |
| **P3.5** docs plugin | `bac0af6` | 3 tools moved to livespec-docs plugin |
| **P4** pitch alignment | `ea171f5` | README rewrite + AGENT_QUICKSTART.md |

### P0 — quick wins (4 tools)

Vivo en `tools/analysis.py` después de `get_symbol_info`. Construidas
ANTES del battle-test para que aparezcan en el log de P2.

- **`get_symbol_source(qname)`** — slice del body sin el payload
  pesado de `get_symbol_info(detail='full')`.
- **`who_calls(qname, max_depth=1)`** — alias slim del backward
  cone de `analyze_impact`. Sólo callers, sin RF rollup.
- **`who_does_this_call(qname, max_depth=1)`** — contraparte forward.
- **`quick_orient(qname)`** — composite: metadata + 1ª línea de
  docstring + top-5 callers/callees por PageRank + RFs vinculados.
  Reemplaza `find_symbol → get_symbol_info → analyze_impact →
  get_requirement_implementation` con 1 sola call.

Tests: `tests/test_quick_wins.py` con 9 cases (happy path + edge
cases + did_you_mean para qname inválido).

### P1 — instrumentation middleware

Archivo nuevo `src/livespec_mcp/instrumentation.py` con
`AgentLogMiddleware`. Registrado en `server.py` antes de los tool
register calls.

- Schema por línea (JSONL): `{ts, tool_name, args_redacted,
  latency_ms, result_chars, error, session_id, workspace}`.
- Output: `<workspace>/.mcp-docs/agent_log.jsonl`. La carpeta
  `.mcp-docs/` ya está gitignored.
- Args redactados: cualquier string que contenga el path absoluto
  del workspace se reescribe a `<workspace>/...`. Logs compartibles
  sin filtrar `$HOME`.
- Opt-out: `LIVESPEC_AGENT_LOG=0`.
- Errores de write se tragan en silencio. La middleware NUNCA debe
  romper dispatch.
- `result_cited_in_final_answer` (mencionado en HANDOFF original NO
  lo escribe la middleware. Es post-hoc — annotation manual o
  heurística que cruza qnames del result vs el texto final del agent.

Tests: `tests/test_instrumentation.py` con 5 cases (schema completo,
multi-call orden, redaction, mcp_error semantics donde error=None,
opt-out por env var).

### P2 — battle-test harness (prep, no ejecución)

P2 mismo es trabajo de campo (correr sesiones reales contra Django/
Next.js/a large Rust monorepo/etc.) y no se puede automatizar sin perder el signal.
Esta sub-phase preparó las herramientas para ejecutarlo después.

- **`bench/agent_log_analyze.py`** — agrega N streams JSONL.
  Output:
  - Tabla por tool: calls, errors, p50/p95 latency, p50/max chars.
  - Top 20 follow-up pairs `A→B` dentro de session_id (cross-session
    no cuenta — sería ruido).
  - Silent tools: registradas pero nunca llamadas → drop candidates.
  - Markdown por defecto, `--json` para diffs entre runs.
- **`docs/AGENT_USAGE_DATA.md`** — esqueleto con tabla de codebases
  objetivo (Django/Next.js/a large Rust monorepo + 2 TBD), notas de metodología,
  secciones de Findings vacías a llenar después.

Tests: `tests/test_agent_log_analyze.py` con 8 cases (malformed-line
skip, workspace-dir resolution, totals, per-tool stats, follow-up
pairs containment within sessions, silent-tools diff, Markdown
render smoke, empty input).

### P3a — drop v0.6 aliases (breaking)

Promesa de v0.7 cumplida. ROADMAP §4 item 1 lo flageaba como "no data
needed". Removidos 4 `@mcp.tool` blocks de `tools/requirements.py`:

| Removido | Usar en su lugar |
|---|---|
| `link_requirement_to_code` | `link_rf_symbol` |
| `link_requirements` | `link_rf_dependency` |
| `unlink_requirements` | `unlink_rf_dependency` |
| `get_requirement_dependencies` | `get_rf_dependency_graph` |

Cambios en tests: 1 test alias-compat removido (`test_v0_5_aliases_still_work`),
3 sites renombrados a canonical en `test_did_you_mean.py`,
`test_indexing.py`, `test_phase456.py`.

### P3b prep — resource paridad (no-data, no-breaking-tools)

Prep mecánico para la conversión tool→resource pendiente en P3 main.
ZERO data necesaria — solo refactor estructural. Tools mantienen su
contrato; resources ahora devuelven el mismo payload que sus tools
homónimos. Cuando llegue data de P2 y se decida deprecar los tool
wrappers, el corte es de una línea.

- **`compute_index_status(st)`** extraído a module-level en
  `tools/indexing.py`. Tool `get_index_status` y resource
  `project://index/status` lo comparten. Resource ahora devuelve
  `{workspace, project_id, files, symbols, edges, requirements,
  last_run}` (antes solo `{last_run}`). Backward compatible —
  solo añade fields.
- **`compute_project_overview(st, include_infrastructure=False)`**
  extraído a module-level en `tools/analysis.py`. Tool
  `get_project_overview` y resource `project://overview` lo
  comparten. Resource ahora devuelve `{workspace, languages,
  top_symbols (con PageRank), requirements_total,
  requirements_linked}` (antes `{workspace, files, symbols,
  requirements}`). **Breaking en resource shape** — el tool no
  cambia.

Tests: `tests/test_indexing.py` actualizado (test viejo asserts el
nuevo shape) + 2 tests nuevos de paridad explícita
(`test_resource_overview_parity_with_tool`,
`test_resource_index_status_parity_with_tool`) que invocan tool y
resource y comparan output exacto.

### P2 sesiones de battle-test (ejecutadas, 40 calls / 3 workspaces)

Toda la data en `docs/AGENT_USAGE_DATA.md`. Usar `bench/agent_log_analyze.py`
para re-correr el agregado.

| # | Codebase | Profile | Calls | Bugs surfaced |
|---|---|---|---:|---|
| 01 | the workflow runner (130 files / 1173 syms) | exploration | 11 | #1 resolver fan-out, #2 entry-point flag, #3 structural noise |
| 02 | livespec-mcp itself (84 files / 495 syms) | refactor | 11 | #4 `__main__` guards, #5 list-stored fns, #6 middleware, #7 fixtures-as-tests |
| 03 | demo-app (4 files / 23 syms / 6 RFs) | RF flow | 7 | #8 `__init__.py` orphan, #9 missing test-coverage signal, #10 propose tests/ |

Tier-1 data-validated: **14/39 tools** (8 code intel + 4 RF agentic +
2 P0 wins overlap). Plugin candidates: **11 livespec-rf + 3 livespec-docs**.
Drop / resource-only: **8 tools**. Detalle completo en
`docs/AGENT_USAGE_DATA.md` § "Updated tier signal".

### P2 bugs surfaced + fixed (11/11 cerrados)

| # | Tool | Síntoma | Fix |
|---|---|---|---|
| 1 | `_resolve_refs` (indexer) | 7 callees en `embed_cache.search`, 4 false positives por short-name fan-out | Same-file fallback weight 0.7 cuando scope no desambigua |
| 2 | `quick_orient` | `@mcp.tool` con 0 callers reportado como "dead" implícitamente | Output `is_entry_point` + `framework_decorators` |
| 3 | `get_project_overview` | top_symbols dominado por `.get`/`add_parser`/`run` (patrones estructurales) | Filter names en ≥3 files, opt-out via `include_structural_patterns=True` |
| 4 | `find_dead_code` | `bench.run.main`, `server.main` flagged dead pero llamados desde `__main__` guard | AST-walk de top-level statements colecta refs |
| 5 | `find_dead_code` | `_m001_drop_dead_tables` etc. flagged pero referenciados en `MIGRATIONS = [...]` list | Idem: list/tuple literal refs caen en module-level walk |
| 6 | `find_dead_code` | `AgentLogMiddleware.on_call_tool` flagged, registrada cross-file en `server.py` | Cross-file: union de module-level refs + protected_class_qnames con `add_middleware` arg-position |
| 7 | `git_diff_impact` | `tests/fixtures/python/same_name_fanout/embed_cache.py` listado como suggested_test | `_looks_like_test_file()` excluye fixtures/, data/, __fixtures__/ |
| 8 | `audit_coverage` | `__init__.py` flagged en `modules_truly_orphan` | Filter package-marker basenames (init.py, mod.rs, package-info.java, lib.rs, index.{ts,js}) |
| 9 | `audit_coverage` | Test files orphan, no se acreditaba `relation='tests'` | Nuevo field `rf_test_coverage` + count `rfs_with_test_coverage` |
| 10 | `propose_requirements` | RF-009 "Test Shortener" propuesto agrupando 5 test fns | Skip path bajo `tests/`, `test/`, `__tests__/`, `fixtures/` |
| 11 | `find_dead_code` | `start_watcher._do_reindex` (nested fn passed como callback) flagged | `_used_nested_def_names` per-file: nested def names referenciados en parent body |

Wire-validated final state contra livespec-mcp: `find_dead_code` count
**0** (vs 18 pre-fixes).

### Métricas netas v0.8

- **Wire-count tools**: 35+4 (v0.7) → 39+0. Misma superficie, sin deprecated.
- **Tests**: 118 (v0.7) → **150**. +32 net (+33 nuevos −1 alias-compat).
  `uv run pytest -q -m "not embeddings"`.
- **Schema**: v7 sin migration nueva.
- **Edge graph precision livespec-mcp**: 969 → 752 edges (~22% drop = false positives eliminados por resolver fix #1).
- **find_dead_code precision livespec-mcp**: 18 → 0 false positives (100%).

### Lo que queda de v0.8 (P3 main pass — todo desbloqueado)

Data limpia, tier signal data-validated. Próximas fases NO requieren más
sesiones — pueden arrancar con confianza. **Items 1-2 son non-breaking,
items 3-5 son breaking changes que requieren OK explícito.**

- **P3.1 (no-breaking) Plugin auto-detect framework**:
  - At server startup: `SELECT COUNT(*) FROM rf > 0` → registrar `livespec-rf`.
  - Idem `SELECT COUNT(*) FROM doc > 0` → registrar `livespec-docs`.
  - `LIVESPEC_PLUGINS=rf,docs` env var override.
  - Crear `tools/plugins/rf.py` y `tools/plugins/docs.py` (vacíos por ahora).
  - server.py: `if state.has_rfs: from .plugins.rf import register; register(mcp)`.
  - Tests: 2 nuevos verifying conditional registration.
  - **Sin breaking** porque las tools siguen registradas por default igual.
- **P3.2 (no-breaking) Tool→resource conversion `get_index_status`**:
  - Resource `project://index/status` ya paritético (P3b prep landed).
  - Marcar tool como deprecado en docstring + log warning una vez por session.
  - Mantener tool 1 release más antes de drop. (Drop in v0.9.)
- **P3.3 (BREAKING) Drops tier-4** (8 tools):
  - `list_files`, `start_watcher`, `stop_watcher`, `watcher_status`,
    `rebuild_chunks`, `get_call_graph`, `get_symbol_info`, `search`.
  - Justificación: silent en 3 sessions, 3 profiles distintos. Stakeholder
    posture en CLAUDE.md auto-corrige tier basado en data.
  - Tests: cleanup tests que invocan estas tools. Algunas pruebas de
    happy-path solo, fácil de borrar.
  - CHANGELOG entry "Removed".
- **P3.4 (BREAKING) Move RF mutation tools → `plugins/rf.py`** (11 tools):
  - `link_rf_symbol`, `bulk_link_rf_symbols`, `link_rf_dependency`,
    `unlink_rf_dependency`, `get_rf_dependency_graph`,
    `scan_rf_annotations`, `scan_docstrings_for_rf_hints`,
    `create_requirement`, `update_requirement`, `delete_requirement`,
    `import_requirements_from_markdown`.
  - Si `livespec-rf` plugin auto-loads (DB state), agente sigue viendo
    las tools. Si no, no. **Para repos sin RFs, surface se reduce.**
- **P3.5 (BREAKING) Move docs tools → `plugins/docs.py`** (3 tools):
  - `generate_docs`, `list_docs`, `export_documentation`.
  - Idem auto-load por `doc` table count.

- **P4 pitch alignment (no-breaking, post-P3)**:
  - README headline → mover "local-first" del lead a feature bullet,
    liderar con "code intelligence for AI agents — built around RF↔code
    traceability".
  - Crear `docs/AGENT_QUICKSTART.md` con el flow brownfield canónico
    (de `docs/AGENT_USAGE_DATA.md` § "Common follow-up patterns").
  - Sección perf en README con números reales: livespec-mcp (505 syms,
    ~50ms p95), the workflow runner (1173 syms, ~80ms), demo-app (23 syms).

- **P7 cortar v0.8.0** (post-P3+P4):
  - CHANGELOG promote [Unreleased] → [0.8.0] dated.
  - `pyproject.toml` version bump.
  - README tool count actualizado (39 → 17 default + 14 plugins?).
  - Update HANDOFF §3.
  - `git tag -a v0.8.0 -m "..."` + `gh release create v0.8.0`.

### Bugs deferidos a v0.9

Ninguno bloqueante. Posibles items:
- Cross-language version del module-level ref scanner (TS/Rust/Go
  patterns: `if (require.main === module)`, `module.exports`, etc.).
- Closure-capture detection en otros lenguajes (TS arrow callbacks).
- `_resolve_refs` targeted re-walk (Django partial 7s → 1s) —
  desde v0.7, sigue diferido.
- LLM-assisted RF refinement opcional sobre `propose_requirements_from_codebase`.

---

## 3a. Estado previo: pre-v0.8 — doc alignment cerrado, listo para P0

**Commit:** `b6a3e8b docs: align CLAUDE.md + ROADMAP.md on RF tier-1 placement for v0.8`

Sesión previa cerró una discrepancia interna en los docs antes de
arrancar v0.8. ZERO código tocado, sólo CLAUDE.md + ROADMAP.md.

**Qué se resolvió:** Discrepancia entre **CLAUDE.md "Stakeholder
posture"** ("RF traceability es el diferenciador defensible") y la
tier-vision original que ponía sólo 2 RF tools en tier-1
(`audit_coverage`, `propose_requirements_from_codebase`). Trigger:
README línea 8 lidera con "¿Qué código implementa el RF-042?" —
pregunta contestada por `get_requirement_implementation`, que estaba
en tier-2 plugin. Bug de la tier list, no de la stakeholder posture.

Cambios concretos en `b6a3e8b`: tier-1 default a 14-16 tools (8 code
intel + 4 RF agentic + 4 quick wins por construir, ahora hechos en P0);
plugin auto-detect por DB state (decidido pero no implementado todavía
— es P3 main); 5 drops tier-4 decididos; 2 ex-tools → resources
(decidido); plan v0.8 reordenado A→B→C a A.0→B→A→C; ROADMAP §6
self-correction sobre under-counting + biases del autor.

---

## 3b. Estado previo: v0.7 listo — brownfield onboarding

**v0.7.0** (2026-05-01): cierra el gap entre "proyecto fresco con livespec
día 1" y "monorepo Rust de 50K símbolos al que adopto livespec un martes".
- **B5**: `find_symbol` matchea separator-agnostic (`Type::method` ↔ `Type.method`).
- **B3**: pagination + summary_only en aggregator tools (audit_coverage, find_dead_code, find_orphan_tests, find_endpoints, git_diff_impact). Causa: a large Rust monorepo generaba payloads de 286K-7.3M chars.
- **B4**: schema migration v7 con `symbol.visibility`. find_dead_code skipea Rust `pub` por default (a large Rust monorepo pasaba 23K dead → manageable).
- **B1**: `bulk_link_rf_symbols(mappings)` — batch-link N pairs en una sola transacción.
- **B6**: `scan_docstrings_for_rf_hints` — extrae primera oración + verbo de docstrings sin RF link, returns verb_histogram_top.
- **B2** (game changer): `propose_requirements_from_codebase` — heuristic RF discovery agrupando símbolos por módulo + ranking por PageRank, propone RFs con título humanizado + descripción + suggested_symbols.

35 tools (+ 4 aliases v0.6 todavía retenidos), 118 tests, schema v7.

**Flow brownfield end-to-end:**
```
proposals = propose_requirements_from_codebase()
for p in proposals.proposals[:N]:
    create_requirement(p.proposed_rf_id, p.title, p.description)
    bulk_link_rf_symbols([{rf_id: p.proposed_rf_id,
                           symbol_qname: s.qualified_name}
                          for s in p.suggested_symbols])
```

**Diferido a v0.8:**
- Drop aliases v0.6 (`link_requirement_to_code`, `link_requirements`, etc.)
- `_resolve_refs` targeted re-walk (Django partial 7s → 1s)
- LLM-assisted RF refinement opcional sobre B2

---

## 3b. Estado previo: v0.6 listo, hardening release

**v0.6.0** (2026-05-01): hardening / debt-paydown release. No new features
significativas; foco en sanear lo que se acumuló.
- **P0**: borré `use_workspace` (deprecated desde v0.2). Breaking.
- **P1**: renames clarificadores en RF tools — `link_rf_symbol` (RF→code) vs `link_rf_dependency` (RF→RF). Aliases viejos quedan hasta v0.7.
- **P2**: migration framework explícito con `schema_migrations` table. Se acabaron los try/except OperationalError dispersos.
- **P3**: Django stress test (40K símbolos, 1M edges). Documentado en bench/. Hot fix: dedup de symbols por (qname, start_line) para shims tipo `if/else def x:`. Graph cache por (db, project, run_id) — load_graph cuesta ~4s en Django, ahora se cachea.
- **P4**: helper `mcp_error` unificado. Todos los errors ahora `{error, isError, did_you_mean?, hint?}`. Hints actionables agregados.
- **P5**: README pitch honesto — "living traceability + on-demand docs", no "living documentation". Tabla explícita de qué vive y qué no.

32 tools (+ 4 aliases deprecated), 97 tests, schema migrations v6.

**Diferido a v0.7:**
- `_resolve_refs` targeted re-walk (partial reindex Django: 7s → ~1s)
- Auto-doc-on-drift watcher mode (gap conocido, requiere UX care)
- Drop aliases v0.6
- Multi-tenant memory pressure (LRU por RSS, no por count)

---

## 3a. Estado previo: v0.5 listo, mergeado a main, taggeado

**v0.5.0 release** (2026-05-01): self-improvement desde feedback real.
- **P0 (bugs reales en demo):** audit_coverage direct/transitive, git_diff_impact error limpio, body_hash AST normalize.
- **P1 (decorators first-class):** schema migration v3 con `symbol.decorators`, `find_endpoints` tool, `find_dead_code` skip framework handlers.
- **P2 (RF deps):** `rf_dependency` table, link_requirements / unlink_requirements / get_requirement_dependencies, analyze_impact cascade.
- **P3 (matcher harden):** multi-RF, confidence override `@rf:RF-001:0.85`, negación `@not_rf:RF-001`, golden dataset 35 cases.
- **P4-A3 (Rust):** `use crate::module::Item` scoped resolution. Cierra el último gap de scoping multilang.

33 tools, 83 tests, 9 langs con extractors testeados (8 con scoped resolution; PHP partial).

**Workflow desde v0.5:** commits directos a `main` (memoria saved). NO PRs.

---

## 3a. Estado previo: v0.4 mergeado, v0.3 taggeado

**Tag + Release v0.3.0** — pushed, GitHub Release publicado en
https://github.com/Rixmerz/livespec/releases/tag/v0.3.0 (apunta al
commit `40a2cfc`).

**Branch v0.4-p0-release-hygiene** (PR #1) tiene P0 + P1 + P2 + P3 cocinados:
- P0: CHANGELOG.md retroactivo + fix CI (uv venv en vez de --system, era
  PEP 668 sobre `/usr` en Ubuntu)
- P1: scoped resolution para TS/JS/Go/Ruby/PHP. `call_target_and_leftmost`
  ahora lee `receiver`/`scope`/`object`. Edge weight=1.0 cross-file en 5
  lenguajes nuevos.
- P2: `find_dead_code`, `audit_coverage`, `find_orphan_tests` (+3 tools, 26
  → 29). `did_you_mean` en errores Symbol-not-found (5 sites). Watcher
  atexit cleanup.
- P3: README + CHANGELOG actualizados.

Tests: 51 → 69 default. CI verde en HEAD del branch.

---

## 3a. Estado previo: v0.3 cerrado

**Commits en main (top-down, más reciente primero):**
```
40a2cfc v0.3 P2: markdown RF import + Ruby/PHP + property tests + memory bench + CI + code://
c848d7b v0.3 P1: git_diff_impact + embeddings smoke + delete_requirement + watch flag
2d557fb v0.3 P0: close 4 agent-loop friction gaps
9abc6ff Batch C: large fixture + bench suite + file watcher; revert P1.3 to persistent refs
9ebf8b0 Batch B: stateless multi-tenant + tool consolidation 25 -> 20
3d28759 Batch A: regression tests + indexer + matcher + signature drift
21ba9d1 P0: correctness + honesty pass for v0.2
48e9c14 feat: use_workspace tool for runtime workspace switching
df55874 fix: incremental re-index lost edges from unchanged files
6d0b91c feat(docs): dual-mode doc generation (caller_supplied | sampling)
2d3287e fix: edges wiped on idempotent re-index; FTS5 scores broken
0275563 feat: complete Phases 4-6 (RAG, doc generation, polish)
b465fa7 feat: bootstrap livespec-mcp (Phases 0-3)
```

**Métricas:**
- 26 tools MCP (lista completa en README sección "Tools (26)")
- 53 tests (51 default + 2 con marker `embeddings`)
- 8 lenguajes con extractor probado: Python, Go, Java, JS, TS, Rust, Ruby, PHP
- 8 lenguajes declarados pero sin tests: C, C++, C#, Kotlin, Swift, Scala, Ruby+PHP **YA testeados** ✅
- bench/results-baseline.json con perf real de `requests` repo (745 sym, 2092 edges, 356ms cold, 5ms warm, 25ms partial)

**Footguns G1-G4 cerrados:**
- G1 auto-scan dentro de `index_project` → traza no se cae sola
- G2 migration consume `_migration_state.needs_reextract` → stats correctos post-upgrade
- G3 `_is_infrastructure` heurística → `get_state`/`register`/dunders/1-line wrappers fuera del top de PageRank por default (opt-in con `include_infrastructure=True`)
- G4 `symbol_ref.scope_module` para Python → edges weight 1.0 cuando target en imports, 0.5 sólo fallback

**Killer feature entregada:** `git_diff_impact(base_ref, head_ref)` → changed files → callers → RFs → suggested tests. Smoke contra HEAD~1 de este repo: 7 archivos / 60 símbolos / 46 callers / 3 test files sugeridos. **Esta es la herramienta a destacar en cualquier demo.**

---

## 4. Pendiente: v0.8 phases (orden A.0 → B → A → C, decidido)

**Trigger:** ROADMAP §4 + commit `b6a3e8b`. Battle-test ANTES de curation
porque tier list es opinion-based (ROADMAP §6 self-admit).

### v0.8 P0 — Quick wins (½-1 día)

Construir antes del battle-test para que entren en el log. Todos viven
en `tools/analysis.py` (alias semánticos sobre `analyze_impact`) y
`tools/requirements.py` (composite tools).

- **`get_symbol_source(qname, workspace=None)`** — body extraction sin
  pasar por `get_symbol_info(detail='full')`. Devuelve `{qname, source,
  start_line, end_line, file}`. Cache hit si symbol no cambió por
  body_hash.
- **`who_calls(qname, max_depth=1, workspace=None)`** — alias agentic
  de `analyze_impact(target_type='symbol', target=qname, direction='backward', max_depth)`.
  Devuelve sólo callers, no el payload completo de impact.
- **`who_does_this_call(qname, max_depth=1, workspace=None)`** — idem
  forward direction.
- **`quick_orient(qname, workspace=None)`** — composite: símbolo +
  5 top callers + 5 top callees + linked RFs + first-line docstring.
  Reemplaza 3-4 calls separadas con 1.

Tests: agregar `tests/test_quick_wins.py` con 4 happy paths + 4 edge
cases (qname no existe → `did_you_mean`, symbol sin body, etc.).

### v0.8 P1 — Battle-test instrumentation (½-1 día)

Middleware en `server.py` que envuelve cada tool dispatch:

```python
{
    "tool_name": str,
    "args_redacted": dict,  # paths/secrets stripped
    "result_chars": int,
    "latency_ms": int,
    "agent_followed_up_with": str | None,  # next tool call name
    "result_cited_in_final_answer": bool,  # filled post-session
    "session_id": str,
    "workspace": str,
    "ts": ISO8601,
}
```

Output a `.mcp-docs/agent_log.jsonl`. `result_cited_in_final_answer`
se llena vía side-channel (manual annotation post-session, o heurística
por overlap de qnames entre tool result y agent's text output).

Tests: invariante "todo dispatch produce ≥1 log line", redacción de
paths absolutos, no logs de calls fallidos antes del middleware.

### v0.8 P2 — Battle-test execution (1-2 días)

5 codebases medium-sized + 3-5 sessions cada uno = 15-25 logs.

| Codebase | Lang | Tasks |
|----------|------|-------|
| Django (subset) | Python | feature, bugfix, refactor |
| Next.js boilerplate | TS | feature, bugfix, refactor |
| a large Rust monorepo (subset) | Rust | feature, bugfix, refactor |
| ? | ? | elegir en sesión |
| ? | ? | elegir en sesión |

Output: `docs/AGENT_USAGE_DATA.md` con números reales reemplazando las
especulaciones de ROADMAP §2.

### v0.8 P3 — Curation pass data-driven (½-1 día)

Cuando el log dice qué se usó y qué no:

1. **Drop deprecated v0.6 aliases** (`link_requirement_to_code`,
   `link_requirements`, `unlink_requirements`,
   `get_requirement_dependencies`). Sin necesidad de data — promesa.
2. **Drop tier-4 decididos** (5 tools): `list_files`, `rebuild_chunks`,
   `start_watcher`, `stop_watcher`, `watcher_status`.
3. **`get_index_status` + `get_project_overview` → resources**
   (`project://index/status`, `project://overview`).
4. **Plugin auto-detect en `server.py`**:
   - At startup, query `SELECT COUNT(*) FROM rf` y `... FROM doc`.
   - Si > 0, register plugin tools dinámicamente.
   - `LIVESPEC_PLUGINS` env var override.
5. **Move RF mutación tools to `tools/plugins/rf.py`** (10 tools).
6. **Move docs management to `tools/plugins/docs.py`** (3 tools).
7. **Validar contra log**: cualquier tool nunca llamada en 15+ sessions
   se marca como candidato a drop o plugin. Opt-in para v0.9.

### v0.8 P4 — Pitch alignment (½ día)

1. **README headline** → "local-first code intelligence for AI agents
   — call graph, impact analysis, RF↔code traceability". RF como
   diferenciador, NO "(optional)".
2. **`docs/AGENT_QUICKSTART.md`** — primer flow de un agente cold:
   ```
   index_project()
   propose_requirements_from_codebase()
   find_symbol("MyThing")
   get_symbol_info("module.MyThing", detail="full")
   get_requirement_implementation("RF-042")
   analyze_impact(target_type="symbol", target="...")
   git_diff_impact()
   ```
3. **Performance section** del README con números Django and large Rust monorepos/the workflow runner +
   guidance "para repos > 30K símbolos, summary_only=True default".
4. **Sección "agent vs human user"** explícita.

### v0.8 P7 — Cut v0.8.0

- CHANGELOG promote [Unreleased] → [0.8.0]
- pyproject.toml version bump
- README tool count + roadmap row
- HANDOFF.md §3 update
- `git tag -a v0.8.0 -m "..."` + push
- `gh release create v0.8.0`

**Total estimado v0.8: 4-6 días enfocados.**

---

## 5. Convenciones de la sesión

### Caveman ultra mode
Activado al inicio de sesión vía hook. Drop articles/filler/pleasantries. Fragmentos OK. Patrón: `[thing] [action] [reason]. [next step].` Code/commits/security en lenguaje normal.

### Commits
Formato (ver commits anteriores):
```
v0.X PN: short-summary

Detailed multi-paragraph body explaining each subtask, what changed,
why, and any tradeoffs. Test counts at the bottom.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```
- Pasar el mensaje vía HEREDOC (multilinea correcto)
- Push después de cada batch (P0/P1/P2)
- Pre-commit hook valida (no skipear con `--no-verify`)

### Tests
- `uv run pytest -q` corre 51 tests (sin embeddings)
- `uv run pytest -m embeddings` corre 2 más (necesita extras instalados; ya están en este venv)
- `uv run pytest -m "not embeddings"` para skipear explícito
- Property tests: `uv run pytest tests/test_properties.py -v`
- Test debe pasar antes de commit. Si falla, fix before commit, NO commit con `--no-verify`.

### Auto mode
Si el flag está activo, ejecutar continuamente sin pedir confirmación para cosas reversibles. Pausar para acciones destructivas (force push, drop tables, rm -rf).

### Plan mode
Sólo si el usuario lo pide explícitamente o es una tarea de research. Para implementación incremental, ir al código directo.

### Memoria persistente
Sin entradas relevantes para este proyecto. Si hace falta guardar algo entre sesiones, usar `~/.claude/projects/-Users-juanpablodiaz-my-projects-livespec-mcp/memory/`.

---

## 6. Cómo continuar — receta paso a paso

```bash
cd <repo>

# 1. Ver estado actual
git status
git log --oneline -5
# Esperado: HEAD = b6a3e8b "docs: align CLAUDE.md + ROADMAP.md..."

# 2. Verificar que tests siguen verdes
uv run pytest -q -m "not embeddings"      # 118 tests
uv run pytest -m embeddings                # 2 tests (~30s primera vez)

# 3. Próxima fase: v0.8 P0 (quick wins). Ver §4 arriba.

# 4. Crear tasks via TaskCreate, marcarlos in_progress al empezar

# 5. Para cada tarea:
#    - leer archivo objetivo
#    - hacer cambio mínimo
#    - correr test específico
#    - si pasa: marcar completed, seguir
#    - si falla: fix antes de avanzar

# 6. Cada batch coherente -> 1 commit
git add -A
git commit -m "$(cat <<'EOF'
v0.8 Pn: short-summary

Detailed body...

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push

# 7. Si tocás MCP server code, el cliente Claude Code necesita /mcp reconnect
#    para cargar los cambios (proceso largo-running, no auto-reload).
```

---

## 7. Mapa de archivos críticos

```
livespec-mcp/
├── HANDOFF.md                                    # (este archivo)
├── README.md                                     # 26 tools, 8 langs, migration table v0.1→v0.2
├── pyproject.toml                                # deps: fastmcp, tree-sitter, networkx, xxhash
│                                                 # extras: [dev]=pytest+hypothesis+psutil, [embeddings]
├── fastmcp.json                                  # MCP entrypoint declaration
├── .github/workflows/ci.yml                      # GitHub Actions: matrix py3.10/11/12 + embeddings job
├── bench/
│   ├── run.py                                    # subprocess clone + index + RSS sampling
│   ├── README.md
│   └── results-baseline.json                     # requests repo baseline
├── src/livespec_mcp/
│   ├── server.py                                 # FastMCP() + register all
│   ├── config.py                                 # Settings dataclass
│   ├── state.py                                  # multi-tenant LRU cache + use_workspace
│   ├── prompts.py                                # 7 user-facing slash-commands
│   ├── resources.py                              # project://, doc://, code://
│   ├── domain/
│   │   ├── languages.py                          # EXT_LANGUAGE map + parser cache
│   │   ├── extractors.py                         # _py_extract (ast) + _ts_extract (tree-sitter)
│   │   ├── indexer.py                            # walk + xxh3 cache + symbol_ref + resolve
│   │   ├── graph.py                              # NetworkX load + PageRank pure-fallback
│   │   ├── matcher.py                            # 2-level @rf: parser with negation guard
│   │   ├── md_rfs.py                             # markdown spec parser
│   │   ├── rag.py                                # AST chunking + FTS5 + sqlite-vec + RRF
│   │   └── watcher.py                            # watchdog wrapper + per-workspace registry
│   ├── storage/
│   │   ├── schema.sql                            # current schema (project, file, symbol, edge,
│   │   │                                         #   symbol_ref with scope_module, rf, rf_symbol,
│   │   │                                         #   doc with sig+body hashes, chunk + FTS5 +
│   │   │                                         #   vec0 virtual tables, _migration_state)
│   │   └── db.py                                 # connect + _migrate_v1_to_v2 + reextract flag
│   └── tools/
│       ├── indexing.py                           # use_workspace, index_project (force, watch),
│       │                                         #   get_index_status, list_files
│       ├── analysis.py                           # find_symbol, get_symbol_info, get_call_graph,
│       │                                         #   analyze_impact, get_project_overview,
│       │                                         #   git_diff_impact, _is_infrastructure
│       ├── requirements.py                       # CRUD + link + scan + import_md + delete
│       ├── search.py                             # search (hybrid) + rebuild_chunks (embed flag)
│       ├── docs.py                               # generate_docs (3 modes) + list_docs (only_stale)
│       │                                         #   + export_documentation
│       └── watcher.py                            # start/stop/status
└── tests/
    ├── conftest.py                               # sample_repo fixture (4 files Python)
    ├── fixtures/                                 # python, javascript, typescript, go, java,
    │                                             #   rust, ruby, php — 8 langs
    ├── test_indexing.py                          # baseline integration tests
    ├── test_phase456.py                          # RAG + docs + search
    ├── test_extractors.py                        # 8 langs parametrized
    ├── test_regressions.py                       # 4 prior bugs locked in
    ├── test_large_repo.py                        # 100+ symbol procedural fixture
    ├── test_watcher.py                           # filesystem watcher
    ├── test_git_diff.py                          # P1.1
    ├── test_md_import.py                         # P2.1
    ├── test_properties.py                        # 4 hypothesis properties
    └── test_embeddings.py                        # marker `embeddings`, skip if extras missing
```

---

## 8. Estado completo v0.8 + lo que sigue

P0 + P1 + P2 (3 sessions + 11 fixes + wire validation) + P3a + P3b-prep
**HECHOS y mergeados a main**. P3 main pass desbloqueado.

1. ✅ **P0 quick wins** — `0db55a8`. 4 tools + 9 tests.
2. ✅ **P1 instrumentation** — `bab89ba`. Middleware + 5 tests.
3. ✅ **P2 prep** — `fd6b39c`. Analyzer + skeleton.
4. ✅ **P3a alias drop** — `08315bc`. −4 aliases v0.6 (breaking).
5. ✅ **P3b prep** — `770be36`. Resource paridad + helpers compartidos.
6. ✅ **P2 sesiones 01-03** — `f7384e0` + `44a0dc4` + `af4f3db`. 40 calls,
   3 codebases, surfacearon 11 bugs.
7. ✅ **P2 fixes #1-11** — `bc8ba1d` + `c14e8d4` + `a8daf0d` + `2956bcc`.
   Todos los bugs de battle-test cerrados.
8. ✅ **P2 wire validation** — `e40a693`. find_dead_code 18→0 false
   positives en livespec-mcp.
9. 🟢 **P3 main pass** — desbloqueado. Detalle item-by-item en §3
   ("Lo que queda de v0.8"). Items 1-2 son non-breaking, items 3-5
   son breaking changes que requieren OK explícito.
10. ⏳ **P4 pitch alignment** — README headline + AGENT_QUICKSTART.md
    + sección perf. Detalle en §3.
11. ⏳ **P7 cut v0.8.0** — CHANGELOG promote, pyproject bump, tag,
    release. Detalle en §3.

**Lo que NO va en v0.8:**

- Features nuevas (incluso `pre-flight token budget` y `search_by_signature`).
- HTTP transport (compromete local-first).
- LLM-assisted RF refinement sobre B2 (deferred desde v0.7, sigue deferred).
- `_resolve_refs` targeted re-walk (deferred desde v0.7, sigue deferred).
- mkdocs site (deferred desde v0.5).

---

## 9. Estado de la sesión actual al momento de escribir esto

- Working tree: clean (todo committed y pushed)
- Branch: main, sincronizado con origin/main en `2956bcc`
- Último commit: `v0.8 P2 fix #11: nested-fn closure callback detection`
- Tests: **150/150** default (sin embeddings).
  `uv run pytest -q -m "not embeddings"`.
- Wire-count tools: 39 (39 canonical, sin deprecated).
- Schema: v7 (sin migration nueva en v0.8 a este punto).
- MCP server local: si pasaste el último ciclo `/mcp` reconnect, está
  corriendo `2956bcc`. Para confirmar: `mcp__livespec__find_dead_code({})`
  contra livespec-mcp debería dar `count: 0`. Si da más, el proceso
  está corriendo binario viejo — `/mcp` reconnect.
- Archivos nuevos en v0.8 (todos commiteados):
  - `src/livespec_mcp/instrumentation.py` (middleware)
  - `bench/agent_log_analyze.py` (P2 analyzer)
  - `docs/AGENT_USAGE_DATA.md` (P2 data — 40 calls / 3 sessions / tier signal)
  - `tests/test_quick_wins.py`, `test_instrumentation.py`,
    `test_agent_log_analyze.py`
  - `tests/fixtures/python/same_name_fanout/` (resolver fix #1 fixture)
- Memoria persistente (sin cambios):
  - `feedback_workflow_main_direct.md` (push directo a main, no PRs)
  - `project_stakeholder_posture.md` (RFs first-class, agent UX es el producto)
- Logs JSONL acumulados (P2 data):
  - `<third-party-repo>/.mcp-docs/agent_log.jsonl` (sesión 01)
  - `<repo>/.mcp-docs/agent_log.jsonl` (sesión 02 + validaciones)
  - `<demo-repo>/.mcp-docs/agent_log.jsonl` (sesión 03)
  - Re-correr aggregate: `uv run python bench/agent_log_analyze.py <ws1> <ws2> <ws3>`

---

## 10. Cuando reanude el agente

1. **Confirmar contexto** leyendo este archivo + `git log --oneline -5`.
   Esperado: HEAD = `2956bcc v0.8 P2 fix #11: nested-fn closure callback detection`.
2. **Verificar tests verdes** con `uv run pytest -q -m "not embeddings"` (150).
3. **Si MCP no está corriendo `2956bcc`**: pedir al user `/mcp` reconnect
   antes de testear. Sanity check: `find_dead_code` debe dar `count: 0`
   sobre livespec-mcp.
4. **Default next step**: iniciar **P3 main pass**. La data de P2 ya
   validó las decisiones de tier — ver §3 "Lo que queda de v0.8".
   Orden recomendado:
   - **P3.1 Plugin auto-detect** (no-breaking, framework primero) →
     crea `tools/plugins/rf.py` y `tools/plugins/docs.py`, queda vacíos,
     server.py registra condicional por DB state. Tests verifying
     conditional registration. **Implementar este primero ANTES de mover
     tools** porque sin plugin framework, mover tools rompe agentes
     existentes.
   - **P3.2 Tool→resource deprecation** (no-breaking) — marcar
     `get_index_status` deprecado, NO drop hasta v0.9.
   - **P3.3 Drops tier-4** (BREAKING, requiere OK del user) — 8 tools:
     `list_files`, `start_watcher`, `stop_watcher`, `watcher_status`,
     `rebuild_chunks`, `get_call_graph`, `get_symbol_info`, `search`.
   - **P3.4 Move RF mutation** (BREAKING, requiere OK) — 11 tools a
     `plugins/rf.py`.
   - **P3.5 Move docs tools** (BREAKING, requiere OK) — 3 tools a
     `plugins/docs.py`.
5. **Si el user pide saltarse P3 y avanzar a P4/P7 directo**:
   factible — la data ya validó que los items P3.3-P3.5 son drops/moves
   correctos. Pero es 1 release de breaking changes en lugar de 2
   (v0.8 = curation, v0.9 = drop deprecated). Decisión del user.
6. Si user pide nuevas features, declinar — v0.8 es ciclo de curación,
   no de adición. Excepción: bug fixes correctness (P2 fixes #1-11
   fueron exactamente eso).
