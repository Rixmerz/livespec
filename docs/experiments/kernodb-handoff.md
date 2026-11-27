# Handoff: kernodb vs SQLite (experimento livespec)

Contexto para retomar en otra máquina. Escrito el 2026-11-27 al pausar la
sesión en la nube. El historial completo de mediciones está en
[`kernodb-versus.md`](./kernodb-versus.md) (rondas 1 a 19b).

## Meta

Rama experimental de livespec que usa kernodb como espejo de SQLite
(`src/livespec_mcp/storage/kerno_store.py`) y lo compara con
`bench/versus.py`. Criterio del dueño: **SQLite no puede ganar ninguna fila**;
fix simple, merge a `main` de kernodb con validación justa (no suite
completa); el release final lo hace el dueño.

Forma de trabajo acordada: el agente principal investiga, perfila y escribe la
especificación (archivo, función, causa medida, cambio exacto, umbral
numérico); subagentes Opus con esfuerzo bajo construyen, de a uno; cambios
chicos los hace el principal. Respuestas en español, cortas.

## Dónde está cada cosa

| Repo | Rama | Commit | Estado |
|---|---|---|---|
| `Rixmerz/kernodb` | `main` | `669b71a` | subido, probado |
| `Rixmerz/kernodb` | `perf/no-thp` | `068a86b` | subido, **sin merge** (ver abajo) |
| `rixmerz/livespec` | `ccr-eddd4f9b-r92g7z` (PR borrador #28) | ver `git log` | subido |

Pruebas en kernodb `main`: 1985 Rust (`cargo test --release -p kernodb-storage
-p kernodb-units -p kernodb`), clippy limpio, 233 del binding. livespec: 1332.

## Lo pendiente inmediato: `perf/no-thp`

Un cambio de una línea en `bindings/python/Cargo.toml`: mimalloc con la
feature `no_thp` (no pide páginas de 2 MiB; no toca el resto del proceso
Python).

Causa medida: durante la carga de `requests` (25 ms) Kerno pedía 49 MB de
memoria nueva, 45 MB en páginas enormes que el kernel pone en cero. Eso son
10 a más de 100 ms de tiempo de sistema según cuánta memoria libre tenga el
host, y explica las corridas lentas de 50-150 ms que SQLite no tiene. Con
`no_thp` la carga toca 20 MB.

Medido en el contenedor, build `669b71a` contra `669b71a` + `no_thp`,
corridas intercaladas en ambos órdenes (ms):

| Muestra | SQLite | Kerno sin `no_thp` | Kerno con `no_thp` |
|---|---|---|---|
| `requests`, 40 pares, orden A | 29.4-30.2 | 121.5 (gana 1/40) | 24.6 (gana 36/40, máx 37.4) |
| `requests`, 40 pares, orden B | 29.4-30.0 | 108.8 (gana 3/40) | 25.8 (gana 33/40, máx 42.4) |
| fastapi, 4 pares | 774-812 | 643 | 294 |

Falta antes del merge a `main`:

1. Django con `no_thp` (la medición se cortó al pausar).
2. Pruebas del binding (233) y `tests/test_kerno_store.py` de livespec con ese
   wheel.
3. Si pasa: merge fast-forward de `perf/no-thp` a `main`, push, y añadir la
   ronda al final de `kernodb-versus.md` (antes de "## If this continues").

## Estado de las filas del versus

- **Ingesta fastapi y Django:** Kerno gana en todas las corridas (≈2x y
  ≈1.25x en ventanas calmas).
- **Disco:** Kerno más chico en los tres repos (1.37 vs 1.52 MB, 21.5 vs
  28.4 MB, 115 vs 138 MB).
- **Apertura + primera consulta y lecturas:** a favor de Kerno desde rondas
  anteriores.
- **Ingesta `requests`:** era la única fila en duda. Con `main` gana por
  mediana (22.8-24.6 vs 25.1-26.7 ms) pero con corridas lentas; con `no_thp`
  gana claro en el contenedor. **Confirmar en una máquina con núcleos reales.**

## Hechos que conviene no volver a derivar

- El titular de ingesta compara `sqlite_normal_ms` con `kerno_write_ms`
  (tiempo dentro de `commit()` + `close()`); `kerno_ms` es el sync completo.
- Fijado a un solo núcleo Kerno pierde en `requests` (37.6 vs 26.8 ms): hace
  ~40 % más trabajo total y gana usando un segundo núcleo.
- `requests` son 3 commits (779 chunks, 754 símbolos, 1484 relaciones) + close.
- El perfil del hilo del commit es plano: `Database.commit` ≈ 85 M Ir,
  tokenización ≈ 32 M Ir en hilos auxiliares, LZ4 ≈ 30 M Ir en el flusher.
- Descartado como causa de las corridas lentas: latencia de fsync (29 syncs
  por carga, parejos), número de hilos de tokenización, purga de mimalloc.
- Sin compresión de bloques se pierde la fila de disco; no es opción.
- No se relajó REC-10a; `bulk_without_wal` es opt-in.

## Aviso de método

En A/B alternados, dos configuraciones idénticas dieron 42.9 vs 26.3 ms de
media: la posición en el ciclo puede fingir un efecto. Medir siempre en ambos
órdenes y con 30-40 corridas de proceso fresco; mirar mediana, p90 y máximo.

## Comandos

```bash
# wheel de kernodb (desde el repo de kernodb)
cd bindings/python && maturin build --release
# instalar en el venv de livespec (desde el repo de livespec)
uv pip install --force-reinstall --no-deps <kernodb>/bindings/python/target/wheels/kernodb-*.whl
# versus completo
.venv/bin/python bench/versus.py --workdir <dir-temporal> --repos requests fastapi django --json bench/versus-results.json
# suite de livespec
.venv/bin/python -m pytest -q
```

## Reglas del experimento

- livespec: suite completa antes de cada commit, nunca `--no-verify`, no tocar
  HANDOFF/README/pyproject por el experimento, trabajar solo en la rama del PR
  borrador.
- kernodb: `crates/kerno/examples/prof.rs` es local, no se commitea.

## Para el release (a cargo del dueño)

Revisar la semántica de `bulk_without_wal`, el codificador rápido de LZ4, los
saltos de capa 11-13, el valor por defecto de `verify_payload_on_read`, las
pruebas de Node (no corridas), `cargo fmt --check` y los issues #12 y #13.
