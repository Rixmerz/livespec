"""Los alias de tipo a nivel de módulo son símbolos.

Una firma está escrita en un vocabulario, y hasta ahora ese vocabulario no
existía en el índice. `get_symbol(qname: QName, limit: Limit)` no le dice nada a
un consumidor que no puede buscar `QName` — y no podía: el extractor emitía solo
function/class/method, así que resolver los tipos nombrados en una firma no
encontraba símbolo alguno.

Medido sobre este repo: de los tipos nombrados en las firmas de 20 clausuras
reales, **45 de 79 no resolvían**, y los más frecuentes eran `Workspace` x7,
`Limit`, `QName`, `MaxDepth`, `SummaryOnly`, `SymbolQuery` — todos
`Annotated[...]` a nivel de módulo en `tool_params.py` / `workspace_param.py`.

La regla es DELIBERADAMENTE ANGOSTA: solo nombres que parecen tipo por
convención (CapWords) ligados a una expresión de tipo. Las constantes normales
(`MAX_ENTRIES = 500`, `_CACHE = {}`) quedan fuera del índice y, sobre todo,
fuera de los reportes de dead code — que es el radio de impacto que importa.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from livespec_mcp.config import Settings
from livespec_mcp.domain.indexer import index_project
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


def _symbols(tmp_path: Path, source: str, kind: str | None = None) -> dict[str, str]:
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn, force=True)
    sql = "SELECT name, kind, signature FROM symbol"
    rows = conn.execute(sql).fetchall()
    return {r["name"]: r["kind"] for r in rows if kind is None or r["kind"] == kind}


def test_an_annotated_alias_becomes_a_symbol(tmp_path: Path):
    found = _symbols(tmp_path, '''
from typing import Annotated

QName = Annotated[str, "a qualified name"]
Limit = Annotated[int, "how many"]


def get_symbol(qname: QName, limit: Limit) -> str:
    return qname
''')
    assert found.get("QName") == "type_alias"
    assert found.get("Limit") == "type_alias"


def test_a_plain_union_alias_becomes_a_symbol(tmp_path: Path):
    found = _symbols(tmp_path, "Weight = float | None\nMaybeName = str | None\n")
    assert found.get("Weight") == "type_alias"
    assert found.get("MaybeName") == "type_alias"


def test_the_signature_carries_what_the_alias_expands_to(tmp_path: Path):
    """Un alias sin su definición no resuelve nada — es el punto entero."""
    (tmp_path / "mod.py").write_text(
        "from typing import Annotated\n\nQName = Annotated[str, 'x']\n", encoding="utf-8"
    )
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn, force=True)
    row = conn.execute(
        "SELECT signature FROM symbol WHERE name='QName'"
    ).fetchone()
    assert row is not None
    assert "Annotated" in row["signature"]


# ---------------------------------------------------------------------------
# lo que NO debe entrar — el radio de impacto es dead-code
# ---------------------------------------------------------------------------

def test_ordinary_constants_stay_out(tmp_path: Path):
    """Un `MAX_ENTRIES = 500` en el índice es un falso positivo de dead code."""
    found = _symbols(tmp_path, '''
MAX_ENTRIES = 500
_CACHE = {}
DEFAULT_NAME = "x"
timeout = 30
''')
    for name in ("MAX_ENTRIES", "_CACHE", "DEFAULT_NAME", "timeout"):
        assert name not in found, f"{name} no es un alias de tipo"


def test_a_capworded_value_is_not_a_type(tmp_path: Path):
    """CapWords sola no alcanza: tiene que estar ligada a una expresión de tipo."""
    found = _symbols(tmp_path, '''
Registry = dict()
Instance = SomeClass()
Total = 1 + 2
''')
    assert "Registry" not in found, "una llamada es un valor, no un tipo"
    assert "Instance" not in found


def test_an_alias_inside_a_function_or_class_stays_out(tmp_path: Path):
    """Solo nivel de módulo: un local con nombre CapWords no es vocabulario público."""
    found = _symbols(tmp_path, '''
from typing import Annotated


def f():
    Local = Annotated[int, "x"]
    return Local


class C:
    Member = Annotated[str, "y"]
''')
    assert "Local" not in found
    assert "Member" not in found


def test_functions_and_classes_are_untouched(tmp_path: Path):
    """La regla nueva no debe alterar lo que ya se extraía."""
    found = _symbols(tmp_path, '''
from typing import Annotated

Name = Annotated[str, "n"]


class Thing:
    def method(self):
        return 1


def func(x: Name) -> int:
    return 1
''')
    assert found.get("Thing") == "class"
    assert found.get("method") == "method"
    assert found.get("func") == "function"
    assert found.get("Name") == "type_alias"
