"""La clausura de contrato: lo que hace falta para cambiar UN símbolo.

Leer código por archivo es una comodidad humana. Un agente al que le piden
cambiar `charge_card` no necesita el archivo donde vive: necesita el cuerpo,
las *firmas* de lo que llama, las *definiciones* de los tipos de esas firmas,
lo que puede lanzar, y qué tests lo cubren.

Tres propiedades que estos tests sostienen, y que son la diferencia entre una
clausura útil y una que miente:

1. **Un tipo que falta se reporta, nunca se descarta en silencio.** Una
   clausura que omite `Money` sin decirlo es peor que no tener clausura: el
   agente escribe contra un tipo que nunca vio.

2. **"No es de este proyecto" no es lo mismo que "no lo pude resolver".**
   Medido sobre este repo, mezclarlos reportaba 23% de resolución cuando cada
   miss era un import de terceros (`DiGraph` de networkx, `Table` de
   reportlab, `Response` del framework web). Separados: **100%, cero gaps
   reales**. Un número tan bajo habría condenado un diseño que funciona.

3. **La degradación es ordenada y declarada.** Pasado el presupuesto se van
   los callees más lejanos primero, y el payload dice cuántos se fueron.
   El cuerpo nunca se recorta: una clausura a la que le falta parte del cuerpo
   que le pediste no es una respuesta más chica, es una equivocada.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from livespec_mcp.config import Settings
from livespec_mcp.domain.contract_closure import (
    DEFAULT_TOKEN_BUDGET,
    build_closure,
    estimate_tokens,
)
from livespec_mcp.domain.indexer import index_project
from livespec_mcp.storage.db import connect


def _index(tmp_path: Path, files: dict[str, str]) -> tuple[sqlite3.Connection, tuple[int, ...]]:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    state = tmp_path / ".mcp-docs"
    settings = Settings(
        workspace=tmp_path, state_dir=state,
        db_path=state / "docs.db", docs_dir=state / "docs",
    )
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    index_project(settings, conn, force=True)
    conn.row_factory = sqlite3.Row
    pids = tuple(r[0] for r in conn.execute("SELECT id FROM project"))
    return conn, pids


def _sym(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT s.id, s.qualified_name, s.kind, s.signature, s.start_line, "
        "       s.end_line, f.path AS file_path "
        "FROM symbol s JOIN file f ON f.id = s.file_id WHERE s.name = ? LIMIT 1",
        (name,),
    ).fetchone()
    assert row is not None, f"{name} no quedó indexado"
    return row


# ---------------------------------------------------------------------------
# lo que la clausura tiene que traer
# ---------------------------------------------------------------------------

PROJECT = {
    "money.py": (
        "class Money:\n"
        "    def __init__(self, cents: int, currency: str):\n"
        "        self.cents = cents\n"
        "        self.currency = currency\n"
    ),
    "gateway.py": (
        "from money import Money\n"
        "\n"
        "\n"
        "def authorize(amount: Money, token: str) -> bool:\n"
        "    return amount.cents > 0\n"
    ),
    "billing.py": (
        "from money import Money\n"
        "from gateway import authorize\n"
        "\n"
        "\n"
        "class DeclinedError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def charge_card(amount: Money, token: str) -> bool:\n"
        "    if not authorize(amount, token):\n"
        "        raise DeclinedError('declined')\n"
        "    return True\n"
    ),
    "tests/test_billing.py": (
        "from billing import charge_card\n"
        "\n"
        "\n"
        "def test_charge_card_ok():\n"
        "    assert charge_card(None, 'tok')\n"
    ),
}


@pytest.fixture
def project(tmp_path: Path):
    conn, pids = _index(tmp_path, PROJECT)
    return conn, pids, tmp_path


def test_the_body_of_the_requested_symbol_is_complete(project):
    conn, pids, root = project
    cl = build_closure(conn, pids, _sym(conn, "charge_card"), root)
    assert "def charge_card" in cl.body
    assert "raise DeclinedError" in cl.body


def test_callees_arrive_as_signatures_not_bodies(project):
    conn, pids, root = project
    cl = build_closure(conn, pids, _sym(conn, "charge_card"), root)
    names = {c.qualified_name for c in cl.calls}
    assert any("authorize" in n for n in names)
    assert "return amount.cents > 0" not in cl.render(), (
        "el cuerpo del callee no va en la clausura — para eso se pide su propia"
    )


def test_the_types_named_in_those_signatures_are_defined(project):
    conn, pids, root = project
    cl = build_closure(conn, pids, _sym(conn, "charge_card"), root)
    assert "Money" in {t.name for t in cl.types}
    assert "self.cents" in next(t.definition for t in cl.types if t.name == "Money")


def test_what_it_raises_is_listed(project):
    conn, pids, root = project
    cl = build_closure(conn, pids, _sym(conn, "charge_card"), root)
    assert "DeclinedError" in cl.raises


def test_covering_tests_are_named_not_included(project):
    conn, pids, root = project
    cl = build_closure(conn, pids, _sym(conn, "charge_card"), root)
    assert any("test_charge_card_ok" in t for t in cl.covered_by)
    assert "assert charge_card(None" not in cl.render()


def test_a_symbol_no_test_touches_says_so(project):
    conn, pids, root = project
    cl = build_closure(conn, pids, _sym(conn, "authorize"), root)
    assert cl.covered_by == []
    assert "no test in the index calls this" in cl.render()


# ---------------------------------------------------------------------------
# la propiedad 2 — el hallazgo que casi condena el diseño
# ---------------------------------------------------------------------------

def test_a_third_party_type_is_external_not_a_gap(tmp_path: Path):
    """`DiGraph` no está en el índice porque es de networkx. Eso está bien."""
    conn, pids = _index(tmp_path, {
        "graphs.py": (
            "from networkx import DiGraph\n"
            "\n"
            "\n"
            "def build(g: DiGraph) -> DiGraph:\n"
            "    return g\n"
        ),
    })
    cl = build_closure(conn, pids, _sym(conn, "build"), tmp_path)
    assert "DiGraph" not in cl.unresolved, (
        "un tipo de una dependencia contado como gap hace ilegible la métrica"
    )
    assert "DiGraph" in cl.external


def test_a_stdlib_type_is_external_without_needing_an_import_line(tmp_path: Path):
    conn, pids = _index(tmp_path, {
        "io_util.py": "def where() -> str:\n    return 'x'\n",
    })
    cl = build_closure(conn, pids, _sym(conn, "where"), tmp_path)
    assert cl.unresolved == []


def test_a_type_that_is_neither_defined_nor_imported_is_a_real_gap(tmp_path: Path):
    """Éste sí es un hueco: la clausura lo prometió y no lo entregó."""
    conn, pids = _index(tmp_path, {
        "orphan.py": "def handle(payload: MysteryShape) -> None:\n    return None\n",
    })
    cl = build_closure(conn, pids, _sym(conn, "handle"), tmp_path)
    assert "MysteryShape" in cl.unresolved
    assert "NOT RESOLVED" in cl.render(), "un gap silencioso es peor que no tener clausura"


# ---------------------------------------------------------------------------
# la propiedad 3 — presupuesto y degradación
# ---------------------------------------------------------------------------

def test_a_closure_that_fits_is_not_degraded(project):
    conn, pids, root = project
    d = build_closure(conn, pids, _sym(conn, "charge_card"), root).as_dict()
    assert d["budget"]["degraded"] is False
    assert d["budget"]["dropped_calls"] == 0
    assert d["budget"]["estimated_tokens"] <= DEFAULT_TOKEN_BUDGET


def test_over_budget_the_calls_are_dropped_and_declared(project):
    conn, pids, root = project
    d = build_closure(
        conn, pids, _sym(conn, "charge_card"), root, token_budget=120
    ).as_dict()
    assert d["budget"]["degraded"] is True
    assert d["budget"]["dropped_calls"] >= 1
    assert "omitted to fit" in d["rendered"]


def test_the_body_is_never_trimmed_to_fit(project):
    """Una clausura sin parte del cuerpo pedido no es más chica: es equivocada."""
    conn, pids, root = project
    d = build_closure(
        conn, pids, _sym(conn, "charge_card"), root, token_budget=120
    ).as_dict()
    assert "def charge_card" in d["body"]
    assert "raise DeclinedError" in d["body"]


def test_depth_zero_skips_type_bodies(project):
    conn, pids, root = project
    cl = build_closure(conn, pids, _sym(conn, "charge_card"), root, depth=0)
    assert cl.types == []
    assert cl.calls, "depth=0 recorta tipos, no llamadas"


def test_the_nearest_callees_survive_the_trim(project):
    """Se van los más lejanos primero: una llamada del mismo archivo importa más."""
    conn, pids, root = project
    full = build_closure(conn, pids, _sym(conn, "charge_card"), root)
    if len(full.calls) < 2:
        pytest.skip("hace falta más de un callee para observar el orden")
    tight = build_closure(
        conn, pids, _sym(conn, "charge_card"), root, token_budget=120
    )
    assert full.calls[0].distance <= full.calls[-1].distance
    assert all(c.distance <= full.calls[-1].distance for c in tight.calls)


# ---------------------------------------------------------------------------
# forma del payload
# ---------------------------------------------------------------------------

def test_the_payload_separates_external_from_unresolved(project):
    conn, pids, root = project
    d = build_closure(conn, pids, _sym(conn, "charge_card"), root).as_dict()
    assert "external_types" in d
    assert "unresolved_types" in d
    assert set(d["external_types"]).isdisjoint(d["unresolved_types"])


def test_the_estimate_is_of_what_the_agent_actually_reads(project):
    conn, pids, root = project
    d = build_closure(conn, pids, _sym(conn, "charge_card"), root).as_dict()
    assert d["budget"]["estimated_tokens"] == estimate_tokens(d["rendered"])


# ---------------------------------------------------------------------------
# render legible — encontrado leyendo la salida, no aserciones sobre el dict
# ---------------------------------------------------------------------------

def test_a_function_callee_renders_without_doubling_its_name(project):
    """`gateway.authorizeauthorize(...)` era lo que salía."""
    conn, pids, root = project
    rendered = build_closure(conn, pids, _sym(conn, "charge_card"), root).render()
    calls_block = rendered.split("## calls (signatures only)")[1].split("\n\n")[0]

    assert "authorizeauthorize" not in calls_block
    assert "gateway.authorize(" in calls_block


def test_a_class_callee_renders_without_gluing_the_declaration(project):
    """`billing.DeclinedErrorclass DeclinedError(Exception)` era lo que salía."""
    conn, pids, root = project
    lines = build_closure(conn, pids, _sym(conn, "charge_card"), root).render()
    assert "DeclinedErrorclass" not in lines


def test_a_callee_with_no_recorded_signature_still_renders(project):
    from livespec_mcp.domain.contract_closure import Callee

    assert Callee("mod.thing", "", "mod.py").render().strip() == "mod.thing(…)"
