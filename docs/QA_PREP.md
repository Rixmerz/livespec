# livespec — preparación Q&A

Set de preguntas probables (técnicas, de producto y de riesgo) con respuestas
listas para usar. Estado de referencia: **v0.31.4 + trabajo Graphify sin
taggear**, 716 tests, 45 tools (28 core + 12 plugin Spec + 5 plugin docs).

Regla de honestidad para todas las respuestas: **hallazgos del grafo ≠ tráfico
de producción**. Si una respuesta suena a garantía, está mal contada.

---

## 1. Producto y posicionamiento

### ¿Qué es livespec en una frase?
Un plugin local-first de Claude Code —MCP server + subagente + Skill— que
mantiene vivos un call graph, la trazabilidad Spec↔código y documentación
on-demand de cualquier repo, sin servicios externos ni API keys.

### ¿Qué problema resuelve que no resuelva grep o el LSP del editor?
Grep responde "dónde aparece este texto"; el LSP responde "dónde está definido
esto" dentro de un proyecto abierto. livespec responde preguntas de *segundo
orden* que un agente hace de verdad: "¿qué se rompe si cambio esto?", "¿qué
Specs toca este archivo?", "¿qué endpoints no tiene ningún cliente indexado?",
y las responde cross-repo y sin editor abierto (headless, CI, cron).

### ¿Por qué la trazabilidad de Specs y no solo code intelligence?
El code intel es la capa universal —y commoditizada—. La trazabilidad Spec↔código
es el diferenciador defendible: es como operan de verdad las orgs serias
(SAFe, Scrum-at-Scale) y es *obligatoria* en industrias reguladas (finanzas,
salud, automotriz, aeroespacial). Un agente puede contestar "cambiar esta
función afecta `auth-user-login` y 3 Specs dependientes" en un round-trip.

### ¿Compite con OpenSpec?
No: lo complementa. OpenSpec es el framework de *autoría* de intención
(requirements + scenarios en `openspec/`). livespec es la capa de grafo y
trazabilidad debajo: enlaza esas specs al código que las implementa y mantiene
el vínculo vivo. Desde v0.22 el round-trip es completo —lee y escribe el
formato— con `sync_openspec` / `export_openspec` / `validate_openspec` y el
ciclo de cambios (`apply_spec_change` / `archive_spec_change`).

### ¿Compite con Graphify?
Tampoco. Desde el trabajo reciente lo **consumimos**: `corroborate_with=<graph.json>`
usa su grafo como evidencia corroborante para descartar falsos positivos.
Importar es cómo se obtienen aristas de herencia y cobertura de 36 lenguajes
sin construir ninguna de las dos cosas. Nunca aporta símbolos ni aristas al
grafo propio: solo *quita* candidatos.

### ¿Qué significa "living" exactamente?
Vive: el índice de símbolos (hash xxh3 incremental), el call graph y sus
aristas, los links Spec↔código (auto-scan de `@spec:` tras cada `index_project`),
el grafo Spec↔Spec y la detección de drift (`body_hash` + `signature_hash`).
No vive: el **contenido** de los docs generados —`generate_docs` es on-demand y
necesita un caller con LLM—. El drift se *detecta*, no se *arregla*.

### ¿Para quién NO es?
Para "borrá todo el código muerto" sin APM/logs. Para búsqueda semántica densa
(los vectores se removieron en 0.29; search es FTS5-only). Para indexar una
carpeta padre con muchos repos sin relación. Y no reemplaza tests, revisión
humana ni debugging de runtime.

---

## 2. Arquitectura

### ¿Cuál es el stack?
FastMCP 3.x sobre stdio; SQLite único (`.mcp-docs/docs.db`, WAL, ACID) con
framework de migraciones explícito; tree-sitter + `tree-sitter-language-pack`
para parsing multi-lenguaje; `ast` de Python para extracción de alta precisión;
NetworkX para call graph, PageRank e impacto topológico (cacheado por corrida
de índice). Cero servicios externos, cero API keys.

### ¿Cómo están organizadas las capas?
Tres: `tools/` es la superficie expuesta por MCP (sin lógica de negocio),
`domain/` es lógica pura sin acoplamiento a MCP (extractores, indexer, graph,
matcher, rag, watcher) y `storage/` es persistencia (schema + migraciones).
La regla es que `domain/` se pueda testear sin levantar un servidor MCP.

### ¿Cómo se resuelven las llamadas entre archivos?
El extractor emite refs, y `_resolve_refs` en `indexer.py` las resuelve contra
los símbolos indexados usando *scoped resolution* por imports (Python, Go, JS,
TS, Rust, Ruby, PHP y Java). Eso es lo que llevó a Django de 1.05M aristas a
465K: menos ruido, no menos cobertura.

### ¿Por qué el resolver es `INSERT OR IGNORE` y nunca `DELETE`?
Porque las refs de archivos *no modificados* tienen que sobrevivir cuando
cambia el archivo al que apuntan. Si el resolver borrara aristas, un reindex
parcial destruiría el cono de llamadores de un símbolo que nadie tocó. Hay una
property test con Hypothesis que fija ese contrato.

### ¿Qué es `body_hash` y por qué importa que sea invariante?
Es la huella semántica del cuerpo de un símbolo, usada para detectar drift de
docs. En lenguajes tree-sitter se computa con `_normalize_ts_body` (recorrido
de tokens hoja que ignora whitespace y comentarios); en Python con
`ast.dump(annotate_fields=False, include_attributes=False)`. El contrato: un
cambio semántico real **debe** mover el hash, un reformateo **no debe**.
Testeado en `tests/test_body_hash_stability.py`.

### ¿Cómo se maneja la evolución del esquema?
Migraciones *append-only*: cada una es una tupla `(version, name, fn)` en
`storage/db.py:MIGRATIONS`. Nunca se reusa un número ni se reordena; agregar
una columna es una tupla nueva, jamás editar una existente. Los tests fuerzan
versiones monótonas. Cuando una migración agrega campos que requieren
re-extracción, marca `needs_reextract` y el próximo `index_project` re-extrae
solo. Vamos por la v20.

### ¿Qué shape tienen los errores?
Uno solo, vía `tools/_errors.py:mcp_error()`:
`{error, isError: True, did_you_mean?: list, hint?: str}`. Sin campos custom,
sin claves extra. Está testeado (`tests/test_error_shape.py`) porque un agente
que ve dos formas de error distintas gasta turnos adivinando en vez de
recuperarse.

### ¿Cómo evitan payloads gigantes?
Contrato de paginación: todo tool que devuelve colecciones potencialmente
ilimitadas acepta `limit` (default 200), `cursor` y `summary_only`. Los
**conteos son exactos** aunque el listado esté paginado. Lo motivó un monorepo
Rust real que devolvía payloads de 4M–7M caracteres.

### ¿Cómo funciona el multi-tenant?
`workspace` es obligatorio en cada llamada desde v0.12 (ruta absoluta, sin
fallback a cwd ni env; un middleware pre-valida). `state.py` cachea un
`AppState` por workspace con LRU=8, así un solo server MCP sirve N repos sin
reiniciar.

### ¿Qué es el `group_db`?
`[workspace] group_db` en `.livespec.toml` hace que varios repos compartan una
sola DB (cada uno conserva su `project_id`). Eso habilita lo cross-repo: una
Spec puede enlazar símbolos de varios repos, y las aristas de ruta HTTP
(`route_ref`) conectan un `fetch`/`axios` del frontend con el handler del
backend. De ahí salen `who_calls().route_callers` ("cambié este endpoint, ¿qué
frontend se rompe?") y `find_legacy_flows`.

---

## 3. Modelo de tools

### ¿Por qué 45 tools y no 10, o 100?
Por curación con datos, no con intuición. La superficie por defecto (28) son
las preguntas que un agente *hace*; la ceremonia de mutación de Specs (12) y
los docs/Explorer (5) son cosas que un *operador* ejecuta, y viven en plugins
que aparecen solo cuando el workspace los justifica. En v0.8 se cortó de 39 a
17 con datos de battle-test (3 sesiones, 40 llamadas, 11 bugs) y desde ahí lo
que crece es cobertura real, no menú.

### ¿Cómo se decide si un tool es core o plugin?
Test: ¿existe principalmente para un humano (generación masiva de docs, export
a markdown) o para un agente en medio de una tarea? Lo primero es plugin. Y
nunca se degrada `list_specs` ni `get_spec_implementation`: contestan las
preguntas guía del README.

### ¿Cómo aparecen los plugins?
Registran al boot, pero `PluginVisibilityMiddleware` filtra `tools/list` y
gatea `tools/call` por workspace: se muestran cuando la DB tiene filas `spec`
o `doc`, cuando existe el bundle `.mcp-docs/explorer/`, o con
`LIVESPEC_PLUGINS=spec|docs|all`. `import_specs_from_markdown` y
`sync_openspec` quedan siempre visibles para poder arrancar brownfield.

### Si un agente aterriza frío en un símbolo, ¿qué llama?
`quick_orient(qname)`: en una sola llamada devuelve metadata, lead del
docstring, top-5 callers y callees por PageRank, Specs enlazadas y flag de
entry point. Reemplaza 3–4 llamadas. p95 <100 ms incluso en Django (40K
símbolos).

### ¿Por qué se removieron los embeddings?
Porque no ganaban su costo. La búsqueda es FTS5 sobre chunks AST-aware (con
split de `snake_case` en tokens OR), y para lookup exacto están `find_symbol` y
`quick_orient`. Menos dependencias, menos caché, comportamiento determinista.
Cuidado con confundirlo con la vieja opinión de "sacar search": `search` se
quedó.

### ¿Y el watcher?
`index_project(watch=True)` sigue existiendo, pero los tools del watcher se
eliminaron: con varios agentes escribiendo concurrentemente es una trampa de
race condition. La recomendación es un hook `PostToolUse` que reindexe
incremental tras cada edición (el skip por hash hace baratos los no-ops).

---

## 4. Precisión y evidencia

### ¿Qué tan confiable es `find_dead_code`?
Es **evidencia de grafo**, no tráfico. La serie sobre Django: 824 → 514 → 348 →
344 candidatos (−58% acumulado) cerrando fuentes de falsos positivos
—saltar no-Python, reconocer refs por string dotted-path, registración en
runtime, captura por closure, re-exports `from .x import Y` y `__all__`—. El
payload incluye `filtered_out`, que reporta qué excluyó cada filtro por defecto
y con qué flag se incluiría: dos corridas en el mismo repo pueden diferir
varias veces según flags, y eso hay que decirlo.

### ¿Qué agrega la corroboración con Graphify, con números?
Barrido sobre 13 repos: `find_dead_code` **382 → 264** candidatos (ayudó en 11
de 13, mediana 50% donde ayudó). `find_orphan_tests`: **273 → 260** (ayudó en 3
de 7). O sea: corroborar dead code sirve ampliamente; corroborar tests
huérfanos sirve poco. Los descartes que verifiqué a mano eran fallos genuinos
de livespec (una llamada cross-file perdida por el resolver, una clase base
viva solo por `extends`, una interface usada solo como anotación de tipo).

### ¿La lección general de ese trabajo?
**La corroboración solo ayuda donde los puntos ciegos difieren.** No es que el
otro extractor sea más exhaustivo —livespec encuentra más aristas en total
(2481 vs 1855, con 89% de coincidencia en `calls`)—: falla *distinto*. Donde
fallan igual (dispatch dinámico, harness por string, reflection) no se recupera
nada. El propio suite de livespec fue 26 → 26 huérfanos, cero, porque su
harness in-process despacha por nombre string y es punto ciego de ambos.

### ¿Y si el archivo externo no está o es de otro repo?
Devuelve `mcp_error`, no "0 descartados". Un cero silencioso se leería como
visto bueno. Además: un grafo en la ruta por defecto se **anuncia**
(`corroboration_available`) pero nunca se consume solo —un índice que cambia
sus respuestas porque apareció un archivo en disco sería peor que uno al que
hay que pedírselo—. Precedencia: argumento explícito → config → solo hint.

### ¿Cuesta tokens o API key la corroboración?
No. El pase de código de Graphify es tree-sitter sin LLM: extracción sobre
`src/` dio `input_tokens: 0` y todas las aristas con `_origin: "ast"`.
Cualquier arista sin ese origen levanta un `warning`, para que la promesa
zero-LLM no se erosione en silencio.

### ¿Qué lenguajes están realmente soportados?
Con suite de tests pasando: Python, Go, Java, JavaScript, TypeScript, Rust,
Ruby y PHP —8 con resolución por scope—. C, C++, C#, Kotlin, Swift y Scala
están listados en `EXT_LANGUAGE` y el extractor genérico *intentará* parsearlos,
pero sin suite que los cubra no los reclamamos. La tabla del README es
deliberadamente honesta en eso.

### ¿Qué frameworks reconoce?
Por decorador: Flask, FastAPI, Click, pytest, FastMCP, Celery, Django (incl.
class-based views por herencia), Spring Boot, Angular. Por routing de
filesystem: Next.js, Deno Fresh, SvelteKit, Remix. Por routing call-style:
Express y Hono (ambos en el barrido por defecto). Desde v0.31, varios pasaron a
opt-in para no inflar el conteo con cosas que no son endpoints HTTP.

---

## 5. Performance

### ¿Cuánto tarda indexar?
Django 5.1.4 (2898 archivos / 39789 símbolos): ~54 s en frío (era ~148 s en
v0.9). Un monorepo Rust de 5K archivos / 50K símbolos: ~30 s. Repos chicos,
menos de un segundo. Reindex parcial tocando 1 archivo en Django: **1.4 s**,
gracias al walk dirigido de `_resolve_refs`.

### ¿Y las consultas?
`quick_orient` p95 <100 ms en Django; `get_project_overview` ~250 ms. El grafo
NetworkX se cachea por `(db_path, project_id, last_run_id)`, lo que llevó una
reconstrucción de ~4 s a microsegundos en cache hit.

### ¿Cómo escala en repos enormes?
Más de 30K símbolos: pasar `summary_only=True` en los tools agregadores y de
traversal para mantener el payload bajo ~200 KB. Los conteos siguen exactos.
El perfil de estrés está en `bench/run.py --large`.

### ¿Cuánta memoria y disco?
En Django, tras la precisión de la resolución por scope: DB de 124 → 71 MB y
RSS post-PageRank de 609 → 294 MB. Esas cifras son independientes de la máquina
(a diferencia de los tiempos).

---

## 6. Calidad, riesgo y proceso

### ¿Cuál es el estándar para aceptar un cambio?
Un suite verde es necesario pero **no suficiente**: cada cambio viaja con
evidencia antes/después de un repo real. Y *borrar* un tool que la evidencia no
justifica es una contribución de primera clase. Está en `CONTRIBUTING.md`.

### ¿Cuántos tests hay y de qué tipo?
716 en la suite por defecto. Son integration-style: la mayoría usa
`Client(mcp)` de FastMCP para llamadas MCP in-process, sin subprocess ni red.
Hay además property tests con Hypothesis para los contratos que no se pueden
romper (resolver, migraciones, estabilidad de `body_hash`).

### ¿Cuál es el mayor riesgo técnico del proyecto?
Que la extracción es una heurística sobre tipos de nodo tree-sitter hardcodeados
(`_DEF_NODE_TYPES`, `_CALL_NODE_TYPES`), y cambia completitud por simplicidad.
Los puntos ciegos son sistemáticos —sin aristas de herencia, sin uso en
posición de tipo, alguna llamada cross-file perdida— y cada uno fabrica un
falso "dead". La mitigación no es prometer exhaustividad: es reportar
`filtered_out`, ofrecer corroboración externa y no vender hallazgos de grafo
como tráfico.

### ¿Y el mayor riesgo de producto?
Que la trazabilidad de Specs se lea como "ceremonia legacy" y se demote. Es
exactamente al revés: el code intel es la capa universal (y replicable) y la
trazabilidad es lo defendible. El nicho —software shops serias e industrias
reguladas— es también donde viven los usuarios de largo plazo.

### ¿Licencia?
AGPL-3.0-only. Si modificás livespec y lo ofrecés por red (host MCP, SaaS, API
interna), tenés que publicar el código correspondiente bajo la misma licencia.
Hay notas de adopción corporativa y una plantilla de primer contacto en
`docs/AGPL_COMPLIANCE_CONTACT.md`.

### ¿Estado de madurez?
Beta pública (v0.31.4). Battle-tested sobre polyrepos reales y validado contra
5 perfiles de agente distintos (exploración, refactor, flujo Spec, bugfix en
Django, feature en TypeScript) —los datos están en `docs/AGENT_USAGE_DATA.md`—.
El banner del README lo dice sin adornos.

### ¿Qué se rompió en breaking changes recientes y por qué sin aliases?
v0.20 renombró toda la nomenclatura RF → Spec (tablas, anotaciones, tools) y
v0.31 removió el dialecto nativo `SPEC-NNN` en favor de slugs OpenSpec. En
ambos casos: corte duro, sin aliases, una sola release rompiente. Mantener dos
nomenclaturas vivas le cuesta a cada agente que lee la lista de tools; el costo
de migrar una vez es menor que el de dudar siempre.

---

## 7. Demo / preguntas de "mostrame"

### Flujo de arranque en frío (cualquier repo)
`/livespec-onboard /abs/path` → el subagente corre `index_project` →
`get_project_overview` → `list_specs`. Después `quick_orient`, `who_calls`,
`find_endpoints`, `find_dead_code` para entender la forma.

### Repo nuevo, spec-first
Autorar capabilities en `openspec/specs/<cap>/spec.md` → `sync_openspec()` →
`validate_openspec(strict=True)` (cada requirement necesita ≥1 scenario) →
implementar enlazando con `link_scenario_symbol` / `bulk_link_spec_symbols` →
`audit_coverage()` para ver cobertura viva → antes del PR, `git_diff_impact`.

### Repo sin documentar (brownfield)
`index_project(explorer=True)` → `propose_specs_from_codebase()` (opcionalmente
con `community_graph=` para agrupar por comunidad detectada en vez de por
prefijo de qname: sobre un servicio TS consolidó 20 propuestas en 12) →
curar a mano como OpenSpec → `sync_openspec()` → enlazar → `audit_coverage()`.

### ¿Qué pasa antes de un PR?
`git_diff_impact(base, head)` devuelve archivos cambiados → llamadores
impactados → Specs afectadas → archivos de test sugeridos. Es el punto de
entrada de revisión, y se citan los spec ids en el PR.

---

## 8. Las tres preguntas incómodas (y la respuesta honesta)

**"¿Esto no es solo un wrapper de tree-sitter?"** — El parsing es el 20%. El
valor está en lo que se construye encima: resolución por scope, persistencia
incremental de aristas, invariantes de hash para drift, PageRank para ordenar
relevancia, el grafo Spec↔código, y un contrato de payload pensado para que un
agente no gaste turnos. Un wrapper no tiene contratos de migración ni de
paginación.

**"¿Por qué creerte los números de dead code?"** — No hay que creerlos: son
candidatos, y el payload dice qué filtró y con qué flag cambiaría. Por eso
existe la corroboración externa y por eso el README dice explícitamente que
hallazgos del grafo no son tráfico de producción. La respuesta correcta a
"¿borro esto?" es "confirmalo con APM/logs".

**"¿Qué pasa si Anthropic/GitHub shippea esto mañana?"** — El code intel se
commoditiza; lo asumo. Lo que no se commoditiza barato es la trazabilidad
Spec↔código con round-trip a un formato de autoría real (OpenSpec), scenarios
como filas de primera clase, cobertura de tests derivada del cono de llamadas y
soporte cross-repo por `group_db`. Ahí está el foso.
