# livespec-mcp — Session Handoff

> **Para reanudar el trabajo después de `/clear`:** abrí este archivo y pedile al agente "leé HANDOFF.md y continuá". Contiene todo el contexto necesario para seguir sin re-explicar.

---

## 1. Identidad del proyecto

- **Repo remoto:** https://github.com/Rixmerz/livespec-mcp (branch `main`)
- **Repo local:** `<repo>`
- **Demo project:** `<demo-app>` (4 archivos Python con `@rf:` annotations en docstrings, ya tiene RFs persistidas en su `.mcp-docs/docs.db`)
- **MCP server:** instalado user-scope como `livespec` en `~/.claude.json`. Comando: `uv --directory <repo> run livespec-mcp`. Env actual: `LIVESPEC_WORKSPACE=<demo-app>` (pero las tools aceptan `workspace=` per-call vía P1.1 multi-tenant).
- **Git user:** Juan Pablo Díaz S.
- **GitHub user:** Rixmerz (auth via gh CLI con scopes `repo, gist, read:org, workflow`).

---

## 2. Stack técnico (no cambiar sin razón)

- **FastMCP 2.14.x** — stdio transport, src-layout, `fastmcp.json`
- **SQLite** (un único `docs.db` en `.mcp-docs/`), WAL, ACID, foreign_keys=ON, single-file backup con `cp`
- **tree-sitter + tree-sitter-language-pack** (1.6.2 — pinned `<1.6.3` por falta de wheel macOS arm64 en versiones más nuevas)
- **Python `ast`** para extracción Python de alta precisión + scoped resolution por imports
- **NetworkX 3.x** para call graph + PageRank (con fallback pure-Python si scipy no está)
- **xxhash** para content/body/signature hashing
- **rank-bm25** para FTS5 BM25 lane
- **watchdog>=4.0** para file watcher
- **fastembed + sqlite-vec** OPCIONALES via `pip install -e ".[embeddings]"` — modelos `jinaai/jina-embeddings-v2-base-code` (768d code) + `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (768d text)
- **hypothesis + psutil** en `[dev]` extras

Todo el stack es local-first: 0 servicios externos, 0 API keys obligatorias, 0 Docker.

---

## 3. Estado actual: v0.8 P0 + P1 + P2-prep + P3a hechos. Bloqueado en P2 (campo).

**Último commit en main:** `08315bc v0.8 P3a: drop v0.6 deprecated RF aliases (breaking)`

Sesión 2026-05-01 cerró cuatro phases consecutivas de v0.8 en una
corrida. Cada una commiteada y pusheada por separado. Working tree
clean.

### Commits del batch v0.8

| Phase | Commit | Cambio neto |
|---|---|---|
| **P0** quick wins | `0db55a8` | +4 tools agentic en `tools/analysis.py` |
| **P1** instrumentation | `bab89ba` | middleware logging + JSONL |
| **P2** prep | `fd6b39c` | analyzer + skeleton de data doc |
| **P3a** alias drop | `08315bc` | −4 aliases v0.6 deprecated (breaking) |

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

### Métricas netas v0.8 (a este punto)

- **Wire-count tools**: 35+4 (v0.7) → 39+0 (P3a). Misma funcionalidad,
  superficie sin deprecated.
- **Tests**: 118 (v0.7) → 139 (P3a). +14 nuevos −1 alias-compat.
  `uv run pytest -q -m "not embeddings"`.
- **Schema**: sin migration. v7 sigue siendo el último.
- **Working tree**: clean. main sincronizado con origin/main en `08315bc`.

### Lo que queda de v0.8 (data-blocked desde acá)

- **P2 ejecución (campo)**: 5 codebases × 3-5 sesiones reales →
  alimentar JSONL al analyzer → llenar `docs/AGENT_USAGE_DATA.md`
  Findings. **Esto NO se autoejecuta — requiere sesiones reales
  con un agent contra repos reales.**
- **P3 main pass**: plugin auto-detect (`livespec-rf` /
  `livespec-docs` por DB state); tool→resource conversion para
  `get_index_status` + `get_project_overview` (resources ya
  existen, falta deprecar/borrar tool wrapper); drops tier-4
  validados contra log; mover RF mutación tools a plugin.
- **P4 pitch alignment**: README headline, `docs/AGENT_QUICKSTART.md`,
  sección perf con números reales.
- **P7 cortar v0.8.0**: bump version, tag, release.

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
https://github.com/Rixmerz/livespec-mcp/releases/tag/v0.3.0 (apunta al
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
3. **Performance section** del README con números Django/a large Rust monorepo/the workflow runner +
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

## 8. Sugerencia de orden para v0.8

Decidido: **A.0 → B → A → C**. Estado a `08315bc`:

1. ✅ **P0 quick wins** — `0db55a8`. 4 tools nuevas + 9 tests.
2. ✅ **P1 instrumentation** — `bab89ba`. Middleware + 5 tests.
3. 🔵 **P2 battle-test** — prep listo (`fd6b39c`: analyzer + skeleton).
   **Ejecución bloqueada en trabajo de campo**: 5 codebases × 3-5
   sesiones reales con un agent → JSONL al analyzer → llenar
   `docs/AGENT_USAGE_DATA.md` Findings. NO se autoejecuta.
4. ⏳ **P3 curation pass** — sub-item P3a (drop aliases v0.6) hecho
   en `08315bc`. Resto bloqueado por data de P2:
     - Plugin auto-detect (`livespec-rf` / `livespec-docs` por
       `SELECT COUNT(*) FROM rf|doc > 0`)
     - Tool→resource: `get_index_status`, `get_project_overview`
       (resources ya existen, falta deprecar el tool wrapper —
       requiere edits en `tools/indexing.py`,
       `tools/analysis.py`, y enriquecer el resource para
       paridad con el output del tool)
     - Drops tier-4 (`list_files`, `rebuild_chunks`,
       `start_watcher`, `stop_watcher`, `watcher_status`) tras
       confirmar contra log que efectivamente no se llaman
     - Mover RF mut/docs tools a `tools/plugins/rf.py` +
       `tools/plugins/docs.py`
5. ⏳ **P4 pitch alignment** — README headline + `AGENT_QUICKSTART.md`
   + sección perf con Django/a large Rust monorepo/the workflow runner.
6. ⏳ **P7 cut v0.8.0** — CHANGELOG promote [Unreleased] → [0.8.0],
   pyproject version bump, README tool count, HANDOFF §3 update,
   tag, release.

**Lo que NO va en v0.8:**

- Features nuevas (incluso `pre-flight token budget` y `search_by_signature`).
- HTTP transport (compromete local-first).
- LLM-assisted RF refinement sobre B2 (deferred desde v0.7, sigue deferred).
- `_resolve_refs` targeted re-walk (deferred desde v0.7, sigue deferred).
- mkdocs site (deferred desde v0.5).

---

## 9. Estado de la sesión actual al momento de escribir esto

- Working tree: clean (todo committed y pushed)
- Branch: main, sincronizado con origin/main en `08315bc`
- Último commit: `v0.8 P3a: drop v0.6 deprecated RF aliases (breaking)`
- Tests: 139/139 default (sin embeddings).
  `uv run pytest -q -m "not embeddings"`.
- Wire-count tools: 39 (39 canonical, sin deprecated).
- Schema: v7 (sin migration nueva en v0.8 a este punto).
- MCP server local: el proceso connected al cliente sigue corriendo
  el binario v0.7. Para usar las 4 quick-wins + middleware: `/mcp`
  reconnect en el cliente Claude Code. Si la sesión actual no las
  necesita, no es bloqueante.
- Archivos nuevos creados en v0.8 (todos commiteados):
  - `src/livespec_mcp/instrumentation.py` (middleware)
  - `bench/agent_log_analyze.py` (P2 analyzer)
  - `docs/AGENT_USAGE_DATA.md` (P2 data skeleton)
  - `tests/test_quick_wins.py`
  - `tests/test_instrumentation.py`
  - `tests/test_agent_log_analyze.py`
- Memoria persistente (sin cambios):
  - `feedback_workflow_main_direct.md` (push directo a main, no PRs)
  - `project_stakeholder_posture.md` (RFs first-class, agent UX es el producto)

---

## 10. Cuando reanude el agente

1. **Confirmar contexto** leyendo este archivo + `git log --oneline -5`.
   Esperado: HEAD = `08315bc v0.8 P3a: drop v0.6 deprecated RF aliases`.
2. **Verificar tests verdes** con `uv run pytest -q -m "not embeddings"` (139).
3. **Default next step depende del estado del battle-test** (P2 ejecución):
   - **Si NO hay logs todavía** en `<algun-workspace>/.mcp-docs/agent_log.jsonl`
     de los target codebases (Django/Next.js/a large Rust monorepo/etc.): la próxima fase
     útil es **driveá sesiones reales** (no se autoejecuta — requiere
     un agent corriendo tasks reales contra repos reales). Después
     correr `uv run python bench/agent_log_analyze.py <ws1> <ws2> ...`
     y llenar `docs/AGENT_USAGE_DATA.md` Findings.
   - **Si HAY logs**: ejecutar P3 main pass — analyzer output → tier
     decisions → drops + plugin auto-detect. Specs en §8 item 4.
4. **Si el usuario quiere saltarse P2 y avanzar a P4 o P7 sin data**:
   pausar y avisar que ROADMAP §6 explícitamente prohíbe curation
   antes de battle-test. Stakeholder posture en CLAUDE.md gana
   siempre — RF tools NO se demotean por intuición.
5. Si el usuario pide nuevas features, declinar — v0.8 es ciclo de
   curación, no de adición. Excepción: bug fixes correctness.
