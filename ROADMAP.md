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

### Tier 1 — el toolkit core (8 tools, lo que un agente usa todos los días)

Estos resuelven preguntas que un agente se hace en cada task no trivial.

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

**Estos 8 son el ~80% del valor real.**

### Tier 2 — RF management plugin (carga opt-in)

Si el usuario no piensa en términos de RFs, estos contaminan el menú:

- `create_requirement`, `update_requirement`, `delete_requirement`
- `list_requirements`, `get_requirement_implementation`
- `link_rf_symbol`, `bulk_link_rf_symbols`
- `link_rf_dependency`, `unlink_rf_dependency`, `get_rf_dependency_graph`
- `scan_rf_annotations`, `import_requirements_from_markdown`
- `scan_docstrings_for_rf_hints`

**Propuesta v0.8**: mover a un módulo cargable via flag/config. Si el
proyecto no tiene `.mcp-docs/rf/` populado, no aparecen. Reduce el menu de
35 a ~22 tools por default.

### Tier 3 — docs management plugin (carga opt-in)

Estos tools son features de **humanos** que querían bulk doc gen, no de
**agentes** que ya escriben código y docs como parte natural de su LLM:

- `generate_docs` (los 3 modes son arquitectura para el problema equivocado
  desde el punto de vista agentic)
- `list_docs`
- `export_documentation`

**Propuesta v0.8**: mover a plugin separado. Reduce el menu otros 3 tools.

### Tier 4 — utility con uso marginal

- `get_call_graph` — `analyze_impact` cubre lo mismo con mejor metadata
- `get_project_overview` — útil 1× por sesión, no por minuto
- `get_index_status` — telemetry, no workflow
- `find_orphan_tests` — útil pero raro
- `find_endpoints` — útil pero solo para projects con decorators framework
- `list_files` — `Grep` cubre el caso
- `search`, `rebuild_chunks` — el host del agente ya tiene búsqueda; valor
  marginal vs. costo de entender
- `start_watcher`, `stop_watcher`, `watcher_status` — net-negativo para un
  agente que está editando (auto-reindex es race condition trap)

**Propuesta v0.8**: revisar uno por uno, dejar solo los que pasan el test
"¿lo llamaría yo, agente, en un task típico?". Algunos quedan, otros van
a plugin tier-2.

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

**No más features. Curation + sharpening.** Tres pillars:

### Pillar A — toolkit reorganization (½-1 día)

1. **Drop deprecated v0.6 aliases** (`link_requirement_to_code`, `link_requirements`,
   `unlink_requirements`, `get_requirement_dependencies`). Promesa cumplida
   ("through v0.7"); v0.8 los elimina.

2. **Plugin-tier separation**:
   - Default load: tier-1 (8 tools) + critical aggregators
   - Plugin: RF management (12 tools) — opt-in via config flag o presence
     of `.mcp-docs/rf/` populated
   - Plugin: docs management (3 tools) — opt-in idem
   - Reduce default menu de 35 a ~12 tools

3. **Add tier-1 quick wins**:
   - `get_symbol_source(qname)`
   - `who_calls(qname)` / `who_does_this_call(qname)`
   - `quick_orient(qname)`
   - `grep_in_indexed_files(pattern, ...)`

### Pillar B — agent battle-test (1 día)

Esto es lo más importante y se postergó por demasiado tiempo:

1. Elegir 5 codebases medium-sized (10K-50K símbolos): mix de Python,
   TypeScript, Rust.

2. Para cada uno, dar a Claude (o GPT-4) **tasks reales no de testing**:
   - "Implementá feature X dado este description"
   - "Fixeá el bug donde Y se comporta mal"
   - "Refactorá el módulo Z para usar dependency injection"

3. **Logging**: capturar cada tool call, sus argumentos, su latencia, y un
   side-channel de "el agente al final usó este resultado o lo descartó?".

4. **Análisis post-mortem**: qué tools se llamaron, cuáles nunca, dónde el
   agente se atascó esperando data que no le vino. Ese signal es oro para
   v0.9.

5. Output: `docs/AGENT_USAGE_DATA.md` con números reales reemplazando las
   especulaciones de este documento.

### Pillar C — pitch + docs alignment (½ día)

1. **README headline change**: de "living traceability + on-demand docs" a
   **"local-first code intelligence for AI agents — call graph, impact
   analysis, RF traceability (optional)"**. Pone "lo que se usa todos los
   días" primero y RF como overlay.

2. **`docs/AGENT_QUICKSTART.md`**: una página explicando "primer flow de
   un agente que llega cold a un repo". 5-6 calls que cubren el 80% del
   uso real:
   ```
   index_project()
   propose_requirements_from_codebase()  # opcional, brownfield
   find_symbol("MyThing")
   get_symbol_info("module.MyThing", detail="full")
   analyze_impact(target_type="symbol", target="module.MyThing.method")
   git_diff_impact()  # post-cambio
   ```

3. **Performance section** del README con números reales (Django 40K,
   a large Rust monorepo 60K, the workflow runner 1K) y guidance: "para repos > 30K símbolos, usar
   `summary_only=True` por default en aggregator tools".

---

## 5. Métricas de éxito v0.8

Distinto de v0.5/v0.6/v0.7 que se midieron en "features shipped":

| Métrica | Baseline (v0.7) | Target (v0.8) |
|---------|----------------|----------------|
| Tools en default menu | 35 + 4 aliases | ~12-15 |
| Tools en plugins (opt-in) | 0 | ~20 |
| Real agent sessions logged | 0 | ≥5 |
| Tools que ningún agente llama en logged sessions | desconocido | ≤3 |
| Tools que TODO agente llama | desconocido | identificados (será input para v0.9) |
| README dice "AI agents" en el headline | No | Sí |

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

**Validar estas hipótesis es exactamente el work del Pillar B de v0.8.**

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
> ~70%: le falta curation (cortar a 1/3 los tools, mover el resto a
> plugins) y battle-test real con agentes resolviendo tasks no de testing.
>
> **v0.8 NO debería agregar features.** Debería:
> 1. Podar a tier-1 toolkit (~12 tools default)
> 2. Mover RF + doc management a plugins opt-in
> 3. Agregar 3-4 quick wins agent-first (`get_symbol_source`, `who_calls`,
>    `quick_orient`)
> 4. Hacer el primer battle-test logged con 5 codebases reales
> 5. Realinear pitch del README
>
> El siguiente inflection point del proyecto NO viene de más código.
> Viene de validar empíricamente qué tools un agente realmente usa.
