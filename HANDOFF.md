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

## 3. Estado actual: v0.6 listo, hardening release

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

## 4. Pendiente: v0.4 candidates (no priorizado, elegir batch en sesión)

### Tema A — Multi-language parity (cerrar la deuda de P0.4)

**A1. Scoped resolution por imports en TS/JS**
P0.4 sólo cubre Python. TS/JS tienen imports estáticos similares (`import { foo } from './bar'`, `const x = require('./y')`). El extractor `_ts_extract` debería detectar `import_statement` y `require_call`, mapear local_name → source_module, y usarlo en `_resolve_refs` igual que Python.
- Archivos: `src/livespec_mcp/domain/extractors.py:_ts_extract`
- Tests: extender `tests/test_extractors.py` para validar que un fixture con imports cross-file produce edges weight=1.0 y no 0.5.

**A2. Scoped resolution para Go (package imports)**
Go usa `import "path/to/pkg"` y referencias por `pkg.Func()`. Similar a A1 pero con sintaxis Go.

**A3. Scoped resolution para Rust (use statements)**
`use crate::module::Item` → tabla local→fully-qualified. Más complejo por el sistema de módulos.

**A4. Hardening Ruby + PHP**
- Ruby: `require 'foo'` y constantes globales — los refs hoy son weight=0.5 globales
- PHP: `use App\Service\X` namespaces

**A5. Notebook (.ipynb) support**
Parsear notebooks Jupyter como secuencia de cells Python. Útil para data science repos. Requiere parser dedicado (no tree-sitter).

### Tema B — New domain features

**B1. RF dependency graph (RF-A depends-on RF-B)**
Hoy las RFs son independientes. Modelar dependencias permitiría: "si RF-001 cambia de status, propaga a sus dependientes". Schema: nueva tabla `rf_dependency(parent_rf_id, child_rf_id, kind)`. Tools: `link_requirements(parent, child)`, `analyze_impact(target_type='requirement')` extendido para incluir descendientes RF.

**B2. `find_dead_code()` tool**
Símbolos con 0 callers (ancestor cone vacío) Y 0 rf_symbol links. Candidatos a borrar. Filtrar entry points (funciones decoradas con `@app.route`, `@mcp.tool`, `if __name__ == "__main__"`, archivos en `bin/`).

**B3. `audit_coverage()` tool**
Reporte: módulos sin ningún RF asociado, RFs sin implementation, RFs con confidence promedio < 0.7. Ya existe el material en list_requirements + get_requirement_implementation; sería un agregador único.

**B4. `bulk_link_requirements(mappings: list[dict])`**
Hoy `link_requirement_to_code` es uno por uno. Para migrar un proyecto con 50 RFs es tedioso. Bulk acepta lista de `{rf_id, symbol_qname, ...}`.

**B5. `find_orphan_tests()` tool**
Tests que no llaman a ningún símbolo del código de producción (potencialmente desconectados). Usa el grafo: tests/* cuyo descendant cone no contiene src/*.

**B6. Per-project `.livespec.toml` config**
Ignore patterns extra, max file size override, lenguajes a skipear, base path para tests. Hoy todo está hardcoded en `domain/indexer.py:DEFAULT_IGNORES`.

### Tema C — Quality & release

**C1. Tag `v0.3.0` + GitHub Release**
Crear tag, generar release notes en GitHub UI o `gh release create`. Cambelog desde commits.

**C2. CI: verificar que el workflow de GitHub Actions PASA**
`.github/workflows/ci.yml` está checked-in pero nunca corrió en GitHub aún (recién pushed). Posibles fixes: cache hit, paths de tree-sitter wheels en Linux (la pin `<1.6.3` es macOS-only — Linux puede usar 1.6.3+).

**C3. Performance regression CI**
Job opcional que corre `bench/run.py --quick`, compara JSON con `bench/results-baseline.json`, falla si cold_ms regresa >20%. Postear comentario en PR.

**C4. Más property tests**
- `git_diff_impact`: cualquier diff válido devuelve estructura consistente
- `_is_infrastructure`: ningún símbolo "real" (>20 líneas, sin nombre dunder/register) clasifica como infra
- `_resolve_refs`: con scope_module no-None y target en scope, weight es siempre 1.0 o 0.9

**C5. Documentation site (mkdocs)**
- `docs/` con páginas: getting started, tools reference (auto-generado del docstring de cada tool), troubleshooting
- `mkdocs.yml` con material theme
- GitHub Pages deployment

**C6. CHANGELOG.md**
v0.1 → v0.2 → v0.3 con highlights y breaking changes (find_references, suggest_rf_links, embed_pending, generate_docs_for_*, detect_stale_docs eliminados).

**C7. CONTRIBUTING.md**
Cómo agregar un lenguaje (fixture + entry en `EXT_LANGUAGE` + tests). Cómo agregar una tool. Convención de commits (feat/fix/refactor + co-author Claude).

### Tema D — Hardening profundo

**D1. Watcher cleanup on shutdown**
Hoy si el server muere con un watcher activo, el thread daemon termina pero los `.mcp-docs/docs.db-wal` pueden quedar en estado raro. Agregar `atexit.register(_stop_all_watchers)` en `domain/watcher.py`.

**D2. Symbol body extraction más robusto**
Hoy `body_hash_seed` para Python es `ast.dump(node)` que incluye line numbers en algunos casos. Verificar que pequeños whitespace changes no disparen drift falso. Posible: normalizar el AST antes de hashear.

**D3. Error contextual en tools**
Hoy errores como "Symbol 'foo' not found" no sugieren alternativas. Devolver top-3 `find_symbol(query='foo')` matches en el campo `did_you_mean`. Patrón ya pseudocodeado en design doc original.

**D4. SQLite write lock detection**
Si el server está siendo usado mientras otro proceso (CI por ejemplo) corre `index_project`, los locks pueden producir errores opacos. Detectar `database is locked` y devolver mensaje claro.

**D5. `code://file/{path}` resource**
Análogo a `code://symbol/{qname}`: devolver el archivo completo. Útil para "dame el archivo que implementa RF-X".

**D6. LLM re-rank tercer nivel del matcher**
v0.2 plan original hablaba de esto. Cuando hay N candidatos para un RF y la confianza es media, dispatch a `ctx.sample()` con prompt validador. Postergado hasta que más hosts soporten sampling.

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

# 2. Verificar que tests siguen verdes
uv run pytest -q -m "not embeddings"      # 51 tests
uv run pytest -m embeddings                # 2 tests (necesita ~30s primera vez para load model)

# 3. (Opcional) ver el bench actual
uv run python bench/run.py --quick

# 4. Decidir qué tema atacar (A/B/C/D arriba)

# 5. Crear tasks via TaskCreate, marcarlos in_progress al empezar

# 6. Para cada tarea:
#    - leer archivo objetivo
#    - hacer cambio mínimo
#    - correr test específico
#    - si pasa: marcar completed, seguir
#    - si falla: fix antes de avanzar

# 7. Cada batch coherente -> 1 commit
git add -A
git commit -m "$(cat <<'EOF'
v0.4 Pn: short-summary

Detailed body...

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push

# 8. Si tocás MCP server code, el cliente Claude Code necesita /mcp reconnect
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

## 8. Sugerencia de orden para v0.4

Si pidiera mi opinión, atacaría en este orden:

1. **C1 + C6** (1 hora): tag v0.3.0 + CHANGELOG. Cierra la versión actual, da punto de partida limpio para v0.4.
2. **C2** (30 min): verificar que el CI workflow pasa en GitHub. Si falla, fix.
3. **A1 + A2** (medio día): scoped resolution para TS/JS y Go. Lleva el "honesto" de v0.2 al siguiente nivel — los 6 lenguajes ya testeados ahora también tienen edges precisos, no sólo símbolos.
4. **B2 + B3** (medio día): `find_dead_code` + `audit_coverage`. Dos tools que son agregadores de queries existentes — bajo costo, alto valor demostrable.
5. **B1** (1 día): RF dependencies. Cambio de schema + 2 tools nuevas. Es el feature de modelado que faltaba.
6. **D1 + D3** (medio día): watcher cleanup + did_you_mean. UX polish.

Eso te deja con v0.4 en ~3-4 días enfocados, sin tocar embeddings ni HTTP transport.

Lo que NO pondría en v0.4:
- HTTP transport (compromete local-first)
- LanceDB (sin user con >2M chunks aún)
- Dashboard UI (out of scope)
- LSP-grade resolution (meses)
- Multi-process indexer (bottleneck no es CPU)

---

## 9. Estado de la sesión actual al momento de escribir esto

- Working tree: clean (todo committed y pushed)
- Branch: main, sincronizado con origin/main en `40a2cfc`
- Tests: 51/51 default + 2/2 embeddings = 53/53
- MCP server local: el proceso connected al cliente sigue corriendo el binario VIEJO (anterior a v0.3) — un `/mcp` reconnect cargaría el nuevo
- venv: `.venv/` con todas las deps incluidas embeddings + hypothesis + psutil
- bench cache: `~/.cache/livespec-bench/requests/` con repo clonado

---

## 10. Una pregunta antes de empezar

Cuando el agente reanude, debería primero:

1. Confirmar contexto leyendo este archivo + `git log --oneline -10`
2. Preguntar al usuario: ¿qué tema (A/B/C/D) atacar? O sugerir el orden de §8.

NO empezar a programar sin confirmar dirección. v0.3 fue un sprint largo y conviene reset de prioridades.
