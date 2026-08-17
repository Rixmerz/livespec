"""Congelar lo que ya está, para poder bloquear lo que llega.

Prendé la detección de duplicación en un repo existente y se ilumina entera:
cientos de casi-duplicados legítimos, deliberados, o simplemente no prioridad
de esta semana. Todos ciertos. Ninguno accionable hoy.

Ese ruido no es un problema de calibración sino de ciclo de vida, y la
solución no es un umbral más alto: un umbral que alcance para callar un repo
legacy alcanza también para perderse el duplicado escrito hace cinco minutos.
La solución es registrar lo que existía al instalar y dejar de reportarlo.

Después de eso se bloquean exactamente dos cosas: código nuevo, y — si el
equipo lo activa — una regresión en un archivo que la sesión ya tocó.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from livespec_mcp.domain import debt_baseline as debt
from livespec_mcp.domain.duplication import Match, fingerprint


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    debt.ensure_table(c)
    return c


VIEJO = "def helper_viejo(a, b):\n    return a + b\n"
NUEVO = "def helper_nuevo(x, y, z):\n    return x * y * z\n"


def _corpus(*items: tuple[str, str, str]):
    return [(q, p, fingerprint(src)) for q, p, src in items]


# ---------------------------------------------------------------------------
# sin baseline: todo reporta, que es lo honesto
# ---------------------------------------------------------------------------

def test_with_no_baseline_everything_reports(conn):
    m = [Match("mod.otro", "mod.py", 0, 1.0)]
    v = debt.judge(conn, fingerprint(NUEVO).structural_hash, m)
    assert v.gated
    assert "no baseline" in v.reason


def test_no_matches_is_never_gated(conn):
    v = debt.judge(conn, fingerprint(NUEVO).structural_hash, [])
    assert v.allowed


def test_status_of_an_empty_baseline(conn):
    assert not debt.has_baseline(conn)


# ---------------------------------------------------------------------------
# con baseline: la deuda vieja calla
# ---------------------------------------------------------------------------

def test_capture_freezes_only_shapes_that_repeat(conn):
    copia = "def otro_helper(p, q):\n    return p + q\n"
    stats = debt.capture(conn, _corpus(
        ("mod.helper_viejo", "mod.py", VIEJO),
        ("otro.otro_helper", "otro.py", copia),
    ))
    assert stats["frozen"] == 2
    assert debt.has_baseline(conn)


def test_capture_is_idempotent(conn):
    copia = "def otro_helper(p, q):\n    return p + q\n"
    c = _corpus(("mod.a", "a.py", VIEJO), ("mod.b", "b.py", copia))
    debt.capture(conn, c)
    second = debt.capture(conn, c)
    assert second["added"] == 0
    assert second["frozen"] == 2


def test_a_shape_already_duplicated_at_capture_goes_quiet(conn):
    """El caso que el baseline existe para callar: deuda que ya estaba, por duplicado."""
    copia = "def otro_helper(p, q):\n    return p + q\n"
    debt.capture(conn, _corpus(
        ("mod.helper_viejo", "mod.py", VIEJO),
        ("otro.otro_helper", "otro.py", copia),
    ))

    v = debt.judge(
        conn, fingerprint(VIEJO).structural_hash,
        [Match("mod.helper_viejo", "mod.py", 0, 1.0)],
    )
    assert v.allowed
    assert "already duplicated at capture time" in v.reason


def test_a_lone_symbol_is_not_a_violation_so_a_new_copy_still_reports(conn):
    """El error que costó la primera versión: congelar símbolos en vez de violaciones.

    Si `format_currency` es único, no es deuda. Escribir hoy una copia SÍ es
    duplicación nueva — y callarla era dejar mudo justo el caso principal.
    """
    debt.capture(conn, _corpus(("mod.helper_viejo", "mod.py", VIEJO)))
    assert not debt.has_baseline(conn), (
        "un símbolo único no es una violación: no hay nada que congelar"
    )

    v = debt.judge(
        conn, fingerprint(VIEJO).structural_hash,
        [Match("mod.helper_viejo", "mod.py", 0, 1.0)],
    )
    assert v.gated


def test_new_code_duplicating_new_code_reports(conn):
    otra = "def a(i, j):\n    return i - j\n"
    debt.capture(conn, _corpus(
        ("mod.x", "mod.py", VIEJO), ("mod.y", "mod.py", VIEJO),
    ))
    v = debt.judge(
        conn, fingerprint(otra).structural_hash,
        [Match("mod.recien", "nuevo.py", 0, 1.0)],
    )
    assert v.gated
    assert "mod.recien" in v.matches


def test_capture_reports_how_many_shapes_it_froze(conn):
    copia = "def otro_helper(p, q):\n    return p + q\n"
    stats = debt.capture(conn, _corpus(
        ("mod.a", "a.py", VIEJO), ("mod.b", "b.py", copia),
        ("mod.solo", "c.py", NUEVO),
    ))
    assert stats["scanned"] == 3
    assert stats["shapes_frozen"] == 1, "solo una forma estaba duplicada"
    assert stats["frozen"] == 2, "las dos copias de esa forma"


# ---------------------------------------------------------------------------
# la regla del boy scout — opcional a propósito
# ---------------------------------------------------------------------------

def _con_deuda(conn):
    copia = "def otro_helper(p, q):\n    return p + q\n"
    debt.capture(conn, _corpus(
        ("mod.helper_viejo", "mod.py", VIEJO),
        ("otro.otro_helper", "otro.py", copia),
    ))


def test_boy_scout_off_leaves_frozen_debt_alone(conn):
    _con_deuda(conn)
    v = debt.judge(
        conn, fingerprint(VIEJO).structural_hash,
        [Match("mod.helper_viejo", "mod.py", 0, 1.0)],
        touched_files=frozenset({"mod.py"}), boy_scout=False,
    )
    assert v.allowed


def test_boy_scout_on_reports_frozen_debt_in_a_touched_file(conn):
    """Editar un archivo desprolijo convierte el desprolijo en tu problema."""
    _con_deuda(conn)
    v = debt.judge(
        conn, fingerprint(VIEJO).structural_hash,
        [Match("mod.helper_viejo", "mod.py", 0, 1.0)],
        touched_files=frozenset({"mod.py"}), boy_scout=True,
    )
    assert v.gated
    assert "already opened" in v.reason


def test_boy_scout_does_not_reach_untouched_files(conn):
    _con_deuda(conn)
    v = debt.judge(
        conn, fingerprint(VIEJO).structural_hash,
        [Match("mod.helper_viejo", "mod.py", 0, 1.0)],
        touched_files=frozenset({"nada_que_ver.py"}), boy_scout=True,
    )
    assert v.allowed


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def test_clear_drops_the_snapshot(conn):
    copia = "def otro_helper(p, q):\n    return p + q\n"
    debt.capture(conn, _corpus(("mod.a", "a.py", VIEJO), ("mod.b", "b.py", copia)))
    assert debt.clear(conn) == 2
    assert not debt.has_baseline(conn)


def test_recapturing_without_reset_accepts_what_landed_since(conn):
    """Documentado como trampa: por eso `reset` existe y hay que decirlo."""
    copia = "def otro_helper(p, q):\n    return p + q\n"
    debt.capture(conn, _corpus(("mod.a", "a.py", VIEJO), ("mod.b", "b.py", copia)))
    stats = debt.capture(conn, _corpus(
        ("mod.a", "a.py", VIEJO), ("mod.b", "b.py", copia),
        ("mod.c", "c.py", NUEVO), ("mod.d", "d.py", NUEVO),
    ))
    assert stats["added"] == 2, "la duplicación que llegó después queda aceptada"


# ---------------------------------------------------------------------------
# integración con search_similar
# ---------------------------------------------------------------------------

def _repo(tmp_path: Path):
    from livespec_mcp.config import Settings
    from livespec_mcp.domain.indexer import index_project
    from livespec_mcp.storage.db import connect

    # Dos copias: eso es una VIOLACIÓN preexistente, que es lo que el baseline
    # congela. Un símbolo solo no es deuda y no habría nada que congelar.
    (tmp_path / "viejo.py").write_text(VIEJO, encoding="utf-8")
    (tmp_path / "otro.py").write_text(
        "def otro_helper(p, q):\n    return p + q\n", encoding="utf-8"
    )
    state = tmp_path / ".mcp-docs"
    st = Settings(workspace=tmp_path, state_dir=state,
                  db_path=state / "docs.db", docs_dir=state / "docs")
    st.ensure_dirs()
    c = connect(st.db_path)
    index_project(st, c, force=True)
    c.row_factory = sqlite3.Row
    return st, c


def _tools():
    from livespec_mcp.tools import analysis

    class _Fake:
        def __init__(self) -> None:
            self.t: dict = {}

        def tool(self, *a, **k):
            def deco(fn):
                self.t[fn.__name__] = fn
                return fn
            return deco

    m = _Fake()
    analysis.register(m)
    return m.t


def test_search_similar_reports_before_a_baseline_exists(tmp_path: Path):
    _repo(tmp_path)
    t = _tools()
    r = t["search_similar"](code=VIEJO, workspace=str(tmp_path))
    assert r["baseline"]["captured"] is False
    assert r["matches"], "sin baseline, la deuda preexistente sí reporta"


def test_search_similar_goes_quiet_after_capture(tmp_path: Path):
    _repo(tmp_path)
    t = _tools()
    cap = t["debt_baseline_capture"](workspace=str(tmp_path))
    assert cap["frozen"] >= 1

    r = t["search_similar"](code=VIEJO, workspace=str(tmp_path))
    assert r["baseline"]["captured"] is True
    assert r["matches"] == [], "la deuda congelada no puede seguir reportando"


def test_the_status_tool_says_whether_anything_is_frozen(tmp_path: Path):
    _repo(tmp_path)
    t = _tools()
    before = t["debt_baseline_status"](workspace=str(tmp_path))
    assert before["captured"] is False
    assert "Run debt_baseline_capture" in before["hint"]

    t["debt_baseline_capture"](workspace=str(tmp_path))
    after = t["debt_baseline_status"](workspace=str(tmp_path))
    assert after["captured"] is True
    assert after["frozen"] >= 1
    assert after["last_captured_at"]
