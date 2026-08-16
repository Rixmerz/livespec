"""Resolución por scope léxico y por parámetro del llamador.

Dos formas de fan-out que las reglas anteriores no podían cubrir, ambas medidas
sobre este mismo repo antes de arreglarlas: **501 de 2523 aristas `calls` (20%)
eran el mismo nombre corto resuelto contra cada homónimo del archivo.**

1. **Scope léxico.** La regla de "mismo archivo" no ayuda cuando los duplicados
   están TODOS en un archivo, que es la forma normal de un helper anidado:
   `extractors.py` define un closure `text` dentro de doce funciones distintas,
   así que una llamada a `text` desde cualquiera de ellas resolvía a las doce.
   El lenguaje resuelve al del ámbito que lo encierra más cerca; el resolver
   ahora hace lo mismo.

2. **Parámetro del llamador.** `_ts_http_call_uses_param(call_node, param, text)`
   llama `text(n)` — `text` es un ARGUMENTO. El callee es lo que le hayan pasado,
   no ninguno de los doce closures homónimos. Emitir una arista por definición
   con ese nombre no es una respuesta imprecisa: es una respuesta equivocada.

Tras ambas reglas: 501 → 124 duplicadas (20% → 6%). Lo que queda es ambigüedad
real entre archivos (`register` definido en nueve módulos de plugin), que el
resolver marca a peso 0.5 a propósito.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from livespec_mcp.config import Settings
from livespec_mcp.domain.indexer import _is_caller_parameter, index_project
from livespec_mcp.storage.db import connect


def _bootstrap(tmp_path: Path) -> tuple[Settings, sqlite3.Connection]:
    state = tmp_path / ".mcp-docs"
    settings = Settings(
        workspace=tmp_path,
        state_dir=state,
        db_path=state / "docs.db",
        docs_dir=state / "docs",
    )
    settings.ensure_dirs()
    return settings, connect(settings.db_path)


def _callees(conn: sqlite3.Connection, src_qname: str) -> list[str]:
    return [
        r["qualified_name"]
        for r in conn.execute(
            """SELECT d.qualified_name FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN symbol d ON d.id = e.dst_symbol_id
               WHERE s.qualified_name = ? AND e.edge_type = 'calls'
               ORDER BY d.qualified_name""",
            (src_qname,),
        )
    ]


def _index(tmp_path: Path, source: str) -> sqlite3.Connection:
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn, force=True)
    return conn


# ---------------------------------------------------------------------------
# 1. scope léxico
# ---------------------------------------------------------------------------

_TWO_CLOSURES = '''
def outer_a(node):
    def text(n):
        return str(n)

    def walk(n):
        return text(n)

    return walk(node)


def outer_b(node):
    def text(n):
        return repr(n)

    def walk(n):
        return text(n)

    return walk(node)
'''


def test_a_nested_helper_binds_to_its_own_enclosing_scope(tmp_path: Path):
    """El caso exacto que producía el 20% de aristas duplicadas."""
    conn = _index(tmp_path, _TWO_CLOSURES)

    assert _callees(conn, "mod.outer_a.walk") == ["mod.outer_a.text"], (
        "walk dentro de outer_a debe resolver al text de outer_a, no a los dos"
    )
    assert _callees(conn, "mod.outer_b.walk") == ["mod.outer_b.text"]


def test_the_scoped_edge_is_unambiguous(tmp_path: Path):
    """Un candidato tras el filtro léxico es certeza, no una preferencia."""
    conn = _index(tmp_path, _TWO_CLOSURES)
    row = conn.execute(
        """SELECT e.weight FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN symbol d ON d.id = e.dst_symbol_id
           WHERE s.qualified_name = 'mod.outer_a.walk'
             AND d.qualified_name = 'mod.outer_a.text'"""
    ).fetchone()
    assert row is not None and float(row["weight"]) == 1.0


def test_the_nearest_enclosing_scope_wins_over_an_outer_one(tmp_path: Path):
    """Sombreado: el `helper` interno gana al del módulo, como en el lenguaje."""
    conn = _index(tmp_path, '''
def helper(x):
    return x


def outer(node):
    def helper(x):
        return x * 2

    def use(n):
        return helper(n)

    return use(node)
''')
    assert _callees(conn, "mod.outer.use") == ["mod.outer.helper"], (
        "el helper del módulo quedó sombreado por el interno"
    )


def test_genuine_cross_file_ambiguity_is_still_kept(tmp_path: Path):
    """La regla no debe borrar ambigüedad real — esa se marca, no se adivina."""
    (tmp_path / "a.py").write_text("def register():\n    return 'a'\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def register():\n    return 'b'\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("def go():\n    return register()\n", encoding="utf-8")
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn, force=True)

    assert sorted(_callees(conn, "c.go")) == ["a.register", "b.register"], (
        "dos definiciones en archivos distintos y ningún import: sigue siendo "
        "ambiguo, y borrar una de las dos sería inventar"
    )


# ---------------------------------------------------------------------------
# 2. parámetro del llamador
# ---------------------------------------------------------------------------

def test_calling_a_parameter_emits_no_edge(tmp_path: Path):
    conn = _index(tmp_path, '''
def owner(node):
    def text(n):
        return str(n)

    return text(node)


def uses_param(node, text):
    return text(node)
''')
    assert _callees(conn, "mod.uses_param") == [], (
        "`text` es un parámetro: el callee es lo que le pasaron, no el closure "
        "homónimo de otra función"
    )
    assert _callees(conn, "mod.owner") == ["mod.owner.text"], (
        "y la llamada legítima del dueño del closure sigue resolviendo"
    )


def test_a_parameter_named_like_a_module_function_does_not_shadow_others(tmp_path: Path):
    """Solo se descarta para el llamador que tiene ese parámetro."""
    conn = _index(tmp_path, '''
def render(x):
    return str(x)


def takes_render(x, render):
    return render(x)


def calls_render(x):
    return render(x)
''')
    assert _callees(conn, "mod.takes_render") == []
    assert _callees(conn, "mod.calls_render") == ["mod.render"]


# ---------------------------------------------------------------------------
# el parser de firmas, directo
# ---------------------------------------------------------------------------

def test_is_caller_parameter_handles_every_signature_shape():
    f = _is_caller_parameter
    assert f("_ts_http_call_uses_param(call_node, param: str, text) -> bool", "text")
    assert f("walk(node, parent_qname: str | None)", "node")
    assert f("g(*args, **kw)", "args")
    assert f("g(*args, **kw)", "kw")
    assert f("h(x, /, y, *, z)", "z")
    assert f("k(a: int=..., b: str=...)", "b")

    # No es un parámetro
    assert not f("scan(root: Path)", "text")
    assert not f("noargs()", "text")
    assert not f(None, "text")
    assert not f("", "text")


def test_a_type_argument_is_not_a_parameter_name():
    """Split solo en comas de nivel superior, o `dict[str, int]` inventa un `int`."""
    assert not _is_caller_parameter("f(a: dict[str, int], b)", "int")
    assert not _is_caller_parameter("f(a: dict[str, int], b)", "str")
    assert _is_caller_parameter("f(a: dict[str, int], b)", "a")
    assert _is_caller_parameter("f(a: dict[str, int], b)", "b")


# ---------------------------------------------------------------------------
# 3. import que apunta fuera del proyecto
# ---------------------------------------------------------------------------

def test_a_stdlib_attribute_call_emits_no_edge(tmp_path: Path):
    """`os.walk(...)` no es ninguno de los `walk` del proyecto.

    El extractor guarda solo el ÚLTIMO nombre de la llamada, así que `os.walk`
    llega como `walk` y caía en el fan-out contra cada función homónima — seis
    en este repo. Pero el ref sí conserva `scope_module='os'`, y ese import es
    la evidencia de que ninguna de las seis es el destino.
    """
    (tmp_path / "mod.py").write_text('''
import os


def walk(node):
    return node


def other(node):
    def walk(n):
        return n
    return walk(node)


def scan(root):
    for dirpath, dirnames, filenames in os.walk(root):
        yield dirpath
''', encoding="utf-8")
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn, force=True)

    assert _callees(conn, "mod.scan") == [], (
        "os.walk resolvió a un walk del proyecto: el scope_module decía que el "
        "destino está fuera"
    )


def test_an_unmatched_PROJECT_import_stays_ambiguous(tmp_path: Path):
    """La regla solo aplica a scopes que el proyecto no define en absoluto.

    Un import de un módulo propio que no logró casar contra el símbolo sigue
    siendo ambigüedad — se marca, no se borra. Borrarla convertiría un fallo
    del resolver en un grafo silenciosamente incompleto.
    """
    (tmp_path / "helpers.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import helpers\n\n\ndef go():\n    return helpers.run()\n", encoding="utf-8"
    )
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn, force=True)

    assert _callees(conn, "app.go") == ["helpers.run"], (
        "helpers es un módulo DEL proyecto — la arista debe existir"
    )
