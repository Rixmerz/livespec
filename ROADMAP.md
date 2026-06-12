# ROADMAP — agent-first reflection

> **Audiencia primaria:** este documento está escrito desde la perspectiva de
> un agente IA que usó livespec-mcp por horas (v0.5 → v0.7) y reflexiona sobre
> qué tools le aportaron valor real, cuáles fueron ruido, y qué falta.
>
> **Por qué existe:** después de cinco releases sustanciales, el proyecto está
> en un punto donde "agregar más features" rinde menos que "podar y validar".
> Esta es la dirección recomendada para v0.8+.

---

## 1. Diagnóstico honesto: estado actual (post-v0.7)

### Lo que está sólido

- **Local-first, zero servicios externos.** Diferenciación real vs. la
  mayoría de MCP servers que requieren API keys.
- **Multi-language indexer con scoping probado** en 8 lenguajes (Python, Go,
  Java, JS, TS, Rust, Ruby, PHP). Edges weight=1.0 para imports detectados.
- **Schema migrations explícitas** (v7 al cierre de v0.7). No hay try/except
  OperationalError disperso.
- **Error shape unificado** `{error, isError, did_you_mean?, hint?}` —
  introduced v0.6, refactored 15+ sites.
- **Aggregator tools paginados** (v0.7 B3) — `find_dead_code`, `audit_coverage`,
  etc. respetan limit/cursor/summary_only. Necesario a partir de ~50K símbolos.
- **`propose_requirements_from_codebase` (v0.7 B2)** — el mecanismo brownfield
  que faltaba. Demostrado funcional en `the workflow runner` (130 archivos): 7 RF candidatos
  que mapean la arquitectura real en <5 segundos.
- **Tests 118/118**, CI verde, golden dataset para el matcher.

### Lo que está saturado

**35 tools es ~2× lo que un agente típico necesita.** Concentración real de
valor en 6-8 tools; los otros son herramientas de gestión humana (RFs, docs)
que pueblan el menú de tools del agente sin agregar valor a tasks típicos.

### Lo que está faltando

- **Curation/pruning pass**: separar core agent toolkit de plugins opcionales.
- **Battle-testing con agente real**: 95% de los tests fueron self-tests o
  sobre demos. No tenemos data de qué pasa cuando un agente cualquiera con
  task arbitrario lo usa 1 hora sobre un codebase ajeno.
- **Pitch alineado**: el pitch real es "LSP-like code intelligence" + "RF
  traceability como overlay opcional". El segundo es nicho.

---

## 2. Tool tier-list desde perspectiva agentic

### Tier 1 — el toolkit core (10 tools, lo que un agente usa todos los días)

Estos resuelven preguntas que un agente se hace en cada task no trivial.

**Regla de split:** Tool va a tier-1 SI un agente HACE esa pregunta.
Tool va a plugin SI un humano la ejecuta para mutar metadata, o un agente
sólo la corre 1× durante onboarding.

| Tool | Pregunta que resuelve | Frecuencia esperada |
|------|----------------------|---------------------|
| `index_project` | ¿Tengo el grafo cargado? | 1× por sesión |
| `find_symbol` (con `did_you_mean`) | ¿Cómo se llama esto? | cada minuto |
| `get_symbol_info(detail="full")` | ¿Qué es X? Source + callers + callees + RFs en una call | cada 5 min |
| `analyze_impact(target=symbol, max_depth=2)` | ¿Qué rompo si cambio X? | cada 10 min |
| `git_diff_impact` | ¿Mi cambio toca lo que esperaba? Self-review post-cambio | cada commit |
| `find_dead_code` | ¿Hay código no alcanzable? | rara vez pero alto valor |
| `audit_coverage` | ¿Qué falta tracear? (cuando hay RFs) | rara vez |
| `propose_requirements_from_codebase` | Dame el mapa arquitectónico (brownfield) | 1× al adoptar livespec |
| `get_requirement_implementation` | ¿Qué código implementa RF-042? (README lead question #1) | cuando agente investiga RF |
| `list_requirements` | ¿Qué RFs existen en este repo? (RF discoverability) | 1× al orientarse |

**Estos 10 son el toolkit agentic central.** Las dos últimas (RF
agentic queries) se promovieron desde tier-2 plugin después de
notar que `get_requirement_implementation` contesta literalmente la
primera pregunta del README — bug en la versión inicial de esta tier list.

### Tier 2 — RF management plugin (auto-on si rf table tiene rows)

**Mecanismo de activación:** server queries DB al startup. Si
`SELECT COUNT(*) FROM rf > 0`, el plugin se registra automáticamente.

> **Corrección v0.12+:** el auto-detect por DB-state murió con el modo
> multi-tenant (workspace per-call) — no hay UNA DB que consultar en
> startup. `register_all_plugins` registra los plugins SIEMPRE; las 32
> tools son visibles en todo momento. Si volver a menús per-workspace
> importa, el mecanismo tiene que ser per-call, no startup-time.
Override via `LIVESPEC_PLUGINS=rf` o `LIVESPEC_PLUGINS=` para
forzar/desactivar. Cero fricción cognitiva — agente que llega a un
repo que ya adoptó RFs encuentra las tools de mutación sin configurar nada.

Tools que MUTAN o son ceremony humana:

- `create_requirement`, `update_requirement`, `delete_requirement` (CRUD)
- `link_rf_symbol`, `bulk_link_rf_symbols` (manual + bulk linking)
- `link_rf_dependency`, `unlink_rf_dependency` (modeling RF→RF)
- `get_rf_dependency_graph` (redundante con `analyze_impact(target_type='requirement')` cascade — la pregunta agentic ya está cubierta)
- `scan_rf_annotations`, `scan_docstrings_for_rf_hints` (bulk re-scan)
- `import_requirements_from_markdown` (bulk import)

11 tools. Quedan invisibles en el menú de un agente fresco si el repo
todavía no tiene RFs.

### Tier 3 — docs management plugin (auto-on si doc table tiene rows)

Estos tools son features de **humanos** que querían bulk doc gen, no de
**agentes** que ya escriben código y docs como parte natural de su LLM:

- `generate_docs` (los 3 modes son arquitectura para el problema equivocado
  desde el punto de vista agentic)
- `list_docs`
- `export_documentation`

3 tools. Mismo mecanismo que el RF plugin: registro automático si
`doc` table tiene rows. Override via `LIVESPEC_PLUGINS=docs`.

### Tier 4 — utility con uso marginal (decisiones tomadas para v0.8)

| Tool | Decisión | Razón |
|------|----------|-------|
| `find_orphan_tests` | **Tier 1** (promovido) | aggregator agentic real, útil en QA tasks |
| `find_endpoints` | **Tier 1** (promovido) | útil en framework projects, decorator-aware |
| `get_call_graph` | **Tier 1** (mantener) | edge list explícita, distinto shape que `analyze_impact` |
| `get_project_overview` | **Resource** `project://overview` | útil 1× por sesión, no merece tool slot |
| `get_index_status` | **Resource** `project://index/status` | telemetry, no workflow |
| `list_files` | **DROP** | `Grep` host con path glob cubre |
| `search` | **Plugin** `livespec-rag` (auto-on con embeddings extras) | host ya busca; valor marginal sin embeddings |
| `rebuild_chunks` | **DROP** (auto-run dentro de `index_project`) | usuario no debería gestionar chunks manualmente |
| `start_watcher` / `stop_watcher` / `watcher_status` | **DROP** | race condition trap para agente editando; net-negativo |

5 tools dropeadas. 2 promovidas a tier-1. 2 movidas a resources. 1 movida a plugin RAG.

---

## 3. Tools que faltan (que un agente quiere y no tiene)

### Quick wins (small, alto valor agentic)

1. **`get_symbol_source(qname)`** — solo el body. Hoy hay que pasar por
   `get_symbol_info(detail=full)` y extraer `.source`, o leer el archivo
   con start_line/end_line. Friction acumulada en cada tarea.

2. **`who_calls(qname)` / `what_does_this_call(qname)`** — semantically
   clearer aliases para `analyze_impact(depth=1, direction='backward'|'forward')`.
   Un agente piensa "find references", no "impact analysis with depth=1".

3. **`grep_in_indexed_files(pattern, path_glob?, kind?)`** — pattern search
   limitado a archivos indexados. Hoy `Grep` raw filesystem con riesgo de
   pegar `node_modules`/`.venv`.

4. **`quick_orient(qname)`** — one-shot tool que devuelve en payload
   compacto: símbolo + 5 callers + 5 callees + linked RFs + first-line
   docstring. Reemplaza 3-4 calls separadas con 1.

### Medium wins (un poco más de inversión)

5. **Pre-flight token budget** — antes de devolver una payload masiva,
   responder con `{estimated_size: 4_400_000_chars, suggestion: "use summary_only"}`.
   v0.7 paginó el problema; v0.8 podría preverlo.

6. **`search_by_signature(...)`** — "encontrá funciones que toman
   `Connection` y devuelven `Optional[X]`". Útil para refactoring patterns.
   Requiere parser-aware signature normalization.

### Speculative (esperar a que falte de verdad)

7. **`agent_scratch(qname, note)` / `agent_scratch_clear()`** — anotaciones
   provisorias durante work del agente sin contaminar RFs reales. Hoy no
   hay forma limpia de "marcá que estoy investigando esto" sin abusar RFs.

---

## 4. Plan recomendado para v0.8

**No más features. Data primero, después curation.** Tres pillars en
orden **B → A → C** (battle-test antes de cortar, no al revés).

> **Cambio respecto a la versión inicial:** la primera versión de este
> documento ordenó Pillar A (curation) antes de Pillar B (battle-test).
> Ese orden es backwards — §6 self-admite que la tier list es opinion-based.
> Cortar tools sin data se basa en intuición; battle-test primero da
> evidence-based cuts. Quick wins (parte de A) se mantienen tempranos
> porque son agent-UX wins que conviene validar EN el battle-test.

### Pillar A.0 — quick wins primero (½ día)

Construir antes del battle-test para que entren en el log:

- `get_symbol_source(qname)` — body extraction sin pasar por `get_symbol_info`
- `who_calls(qname)` — alias semántico de `analyze_impact(direction='backward', depth=1)`
- `who_does_this_call(qname)` — alias semántico de `analyze_impact(direction='forward', depth=1)`
- `quick_orient(qname)` — composite: símbolo + 5 callers + 5 callees + RFs + first-line docstring

`grep_in_indexed_files` se difiere a v0.9 — host del agente cubre con
`Grep` + path glob; valor incremental no justifica el slot.

### Pillar B — agent battle-test (1-2 días) **PRIMERO**

Lo más importante. Validar empíricamente la tier list antes de cortar.

1. **Instrumentación primero (½ día):** middleware en `server.py` que
   captura por cada tool call:
   ```
   {tool_name, args_redacted, result_chars, latency_ms,
    agent_followed_up_with: tool_name | None,
    result_cited_in_final_answer: bool}
   ```
   Output a `.mcp-docs/agent_log.jsonl`. Sin este contrato, post-mortem
   es feel-based — el `result_cited_in_final_answer` es la señal de
   utilidad real que separa "se llamó" de "sirvió".

2. **5 codebases medium-sized (10K-50K símbolos):** mix Python (Django o
   FastAPI repo), TypeScript (Next.js app), Rust (a large Rust monorepo o actix repo),
   más 2 elegidos en el momento.

3. **3-5 sessions por codebase = 15-25 logs.** Para cada uno, dar a
   Claude tasks reales NO de testing:
   - "Implementá feature X dado este description"
   - "Fixeá el bug donde Y se comporta mal"
   - "Refactorá el módulo Z para usar dependency injection"

4. **Análisis post-mortem:** qué tools se llamaron, cuáles nunca, qué
   tools devolvieron data que el agente citó vs descartó.

5. Output: `docs/AGENT_USAGE_DATA.md` con números reales reemplazando
   las especulaciones de §2.

### Pillar A — curation pass (½-1 día) **DESPUÉS del battle-test**

Una vez con data:

1. **Drop deprecated v0.6 aliases** (`link_requirement_to_code`, `link_requirements`,
   `unlink_requirements`, `get_requirement_dependencies`). Promesa cumplida
   ("through v0.7"); v0.8 los elimina. (Sin necesidad de data — es promesa.)

2. **Drop tier-4 decididos** (`list_files`, `rebuild_chunks`, watcher×3).
   `get_index_status` + `get_project_overview` → resources. (Sin necesidad
   de data — son net-negativo o redundantes.)

3. **Plugin-tier separation con auto-detect:**
   - `server.py` query DB al startup: `SELECT COUNT(*) FROM rf` y
     `SELECT COUNT(*) FROM doc`. Si > 0, registrar plugin correspondiente.
   - `LIVESPEC_PLUGINS=rf,docs,rag` env var override.
   - Plugin RF (10 tools): CRUD + linking + bulk + RF-RF + scan + import.
   - Plugin docs (3 tools): generate_docs, list_docs, export_documentation.
   - Plugin RAG (2 tools): search, rebuild_chunks (auto-on con embeddings extras).

4. **Validar contra log del battle-test:** cualquier tool que el log
   muestre como "nunca llamado por agente, alta latencia" se reconsidera
   como candidato a drop o a plugin.

### Pillar C — pitch + docs alignment (½ día)

1. **README headline change**: de "living traceability + on-demand docs" a
   **"local-first code intelligence for AI agents — call graph, impact
   analysis, RF↔code traceability"**. RF traceability NO va como
   "(optional)" — es el diferenciador defensible (CLAUDE.md stakeholder
   posture). El plugin-tier es implementation detail, no marketing.

2. **`docs/AGENT_QUICKSTART.md`**: una página explicando "primer flow de
   un agente que llega cold a un repo". 5-6 calls que cubren el 80% del
   uso real:
   ```
   index_project()
   propose_requirements_from_codebase()  # brownfield onboarding
   find_symbol("MyThing")
   get_symbol_info("module.MyThing", detail="full")
   get_requirement_implementation("RF-042")  # README lead question
   analyze_impact(target_type="symbol", target="module.MyThing.method")
   git_diff_impact()  # post-cambio
   ```

3. **Performance section** del README con números reales (Django 40K,
   a large Rust monorepo 60K, the workflow runner 1K) y guidance: "para repos > 30K símbolos, usar
   `summary_only=True` por default en aggregator tools".

4. **Sección "agent vs human user"**: README explícito sobre quién es
   el target (agentes IA), qué tools default ven, qué plugins se
   activan automáticamente. Honestidad estratégica.

---

## 5. Métricas de éxito v0.8

Distinto de v0.5/v0.6/v0.7 que se midieron en "features shipped":

| Métrica | Baseline (v0.7) | Target (v0.8) |
|---------|----------------|----------------|
| Tools en default menu (sin RFs en repo) | 35 + 4 aliases | ~14 (10 tier-1 + 4 quick wins) |
| Tools auto-on con plugin RF (rf table populada) | 35 + 4 aliases | ~24 (14 + 10 plugin RF) |
| Tools auto-on con plugin RF + docs | 35 + 4 aliases | ~27 (24 + 3 plugin docs) |
| Tools dropeadas | 0 | 5 (`list_files`, `rebuild_chunks`, watcher×3) |
| Resources nuevos (ex-tools) | 0 | 2 (`project://overview`, `project://index/status`) |
| Real agent sessions logged | 0 | ≥15 (5 codebases × 3 tasks min) |
| Tools llamadas en ≥3 logged sessions | desconocido | ≥10 (validación tier-1) |
| Tools llamadas en 0 logged sessions | desconocido | identificadas → revisar |
| Tools cuyo `result_cited_in_final_answer=True` ≥50% | desconocido | identificadas → input v0.9 |
| README dice "AI agents" en headline | No | Sí |
| README RF positioning | "(optional)" risk | "differentiator" explícito |

---

## 6. Self-aware lo que este documento NO contesta

Cosas que afirmé arriba sin evidencia empírica robusta:

1. **"6-8 tools concentran 80% del valor"** — basado en mi propia
   experiencia en esta sesión, no en datos multi-agente.
2. **"Generate_docs es feature humana, no agentic"** — sólo testeé
   marginalmente. Quizás un agente con task "documentá este módulo"
   sí lo usa.
3. **"Pagination en aggregator tools fue suficiente"** — verificado en
   a large Rust monorepo pero no en otros monorepos grandes.
4. **"El call graph es lo más universal"** — afirmación que pediría
   verificación en battle-test antes de tirarla en marketing.
5. **Self-correction post-CLAUDE.md (commit posterior):** la versión
   inicial de este documento puso sólo 2 RF tools en tier-1 (`audit_coverage`
   + `propose_requirements_from_codebase`). Sub-conteo. README línea 8
   lidera con "¿Qué código implementa el RF-042?" — pregunta contestada
   por `get_requirement_implementation`, que estaba en tier-2 plugin.
   Bug de la tier list, no de la stakeholder posture. Promoción de
   `get_requirement_implementation` + `list_requirements` a tier-1
   resuelve la inconsistencia interna y honra la trayectoria evolutiva
   del proyecto: v0.3 hizo el `@rf:` scan automático (RF stays fresh),
   v0.5 invirtió pesado en RF dependency graph (modeling), v0.7 construyó
   `propose_requirements_from_codebase` (zero-friction adoption). Cada
   release hizo RFs MÁS centrales, no menos. La tier list inicial
   contradecía esa trayectoria.
6. **Sesgo del autor:** este documento lo escribió la misma sesión Claude
   que construyó v0.1→v0.7. Riesgos identificados:
   - **Recency bias:** redactado tras ver el menú de 35 tools en uso
     simulado. Reflejo "cortar lo poco usado". Tools poco usadas EN ESTA
     SESIÓN (donde RFs ya estaban cargados) son ceremony de creación —
     pero en adopción brownfield real esas mismas tools serían
     intensamente usadas durante la primera hora.
   - **Survivor bias agentic:** Claude que escribió todo conoce el grafo
     de calls de memoria, no necesita `find_symbol` cada minuto. Claude
     fresco en repo ajeno SÍ lo necesita. Frecuencias estimadas en la
     tabla tier-1 podrían sub-estimar para agentes nuevos.

**Validar 1-4 es exactamente el work del Pillar B de v0.8.** Item 5 ya
quedó resuelto en este commit. Item 6 es contexto para futuros
maintainers leyendo este doc — tomarlo con escepticismo proporcional.

---

## 7. Notas para futuros maintainers (yo o cualquier otro agente)

- **Direct push a main** para este repo. PRs sólo si el sandbox lo bloquea.
- **Tests deben pasar antes de cada commit** — no bypass `--no-verify`.
- **Schema migrations son append-only** — nuevos números, nunca reuse.
- **CHANGELOG entry por cada release** + tag + GitHub release.
- **HANDOFF.md** es el punto de continuidad post-`/clear`.
- **El usuario prefiere honestidad por sobre marketing** — si algo no anda,
  decirlo. Si una feature es speculative, marcarla.

---

## 8. Versión corta para impacientes

> **Estado v0.7**: técnicamente sólido (118 tests, schema v7, 35 tools,
> migration framework, error shape unificado, brownfield onboarding via
> `propose_requirements_from_codebase`). Como producto agentic está al
> ~70%: le falta curation evidence-based + battle-test real con agentes
> resolviendo tasks no de testing.
>
> **v0.8 NO debería agregar features.** Orden **B → A → C** (data antes
> que cortar):
>
> 1. **A.0 Quick wins** (½ día): `get_symbol_source`, `who_calls`,
>    `who_does_this_call`, `quick_orient`. Construir antes para entrar
>    en el log del battle-test.
> 2. **B Battle-test instrumentado** (1-2 días): middleware logging,
>    5 codebases × 3-5 sessions, capturar `result_cited_in_final_answer`.
>    Output: `docs/AGENT_USAGE_DATA.md`.
> 3. **A Curation pass** (½-1 día): drop aliases v0.6, drop tier-4
>    decididos (5 tools), plugin auto-detect por DB state, mover
>    RF mutación + docs management a plugins. Validar contra log.
> 4. **C Pitch alignment** (½ día): README headline agentic, RF como
>    diferenciador (NO "optional"), `AGENT_QUICKSTART.md`, performance
>    section con números reales.
>
> Tier-1 default (10 tools): code intel core 8 + 2 RF agentic
> (`audit_coverage`, `propose_requirements_from_codebase`,
> `get_requirement_implementation`, `list_requirements` — los últimos
> 2 promovidos al resolver discrepancia con README lead questions).
> Plus 4 quick wins = 14 tools default sin RFs en repo. Plugin RF
> auto-on (24 total) cuando `rf` table tiene rows.
>
> El siguiente inflection point del proyecto NO viene de más código.
> Viene de validar empíricamente qué tools un agente realmente usa.
