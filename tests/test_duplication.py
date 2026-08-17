"""Duplicación: encontrar el helper que ya existe, antes de escribirlo de nuevo.

Un agente reescribe un helper que ya existe porque no tiene forma barata de
preguntar "¿esto ya está?". Grep responde sobre nombres, y todo el asunto es
que el duplicado tiene un nombre *distinto* — por eso se escribió.

**La normalización es todo el nivel 0.** Hashear el texto no encuentra nada:
el whitespace, los comentarios y una variable renombrada lo derrotan, y son
exactamente las diferencias que introduce una reescritura. Hashear una
*estructura* con los identificadores reemplazados por su posición de ligadura
encuentra la copia que un humano llamaría la misma función con otros nombres.

**Un falso positivo cuesta distinto que un falso negativo.** Un duplicado que
se escapa cuesta algo de redundancia. Un "esto ya existe" equivocado bloquea
trabajo que el agente hacía bien, y dos de esos enseñan a desactivar el check
— después de lo cual no atrapa nada. Los dos niveles están calibrados para ser
callados.
"""

from __future__ import annotations

import pytest

from livespec_mcp.domain.duplication import (
    MIN_TOKENS_FOR_LEVEL1,
    NEAR_DUPLICATE_THRESHOLD,
    Fingerprint,
    find_duplicates,
    fingerprint,
    similarity,
    structural_hash,
    tokenize,
)

# ---------------------------------------------------------------------------
# nivel 0 — la copia con los nombres cambiados
# ---------------------------------------------------------------------------

ORIGINAL = '''
def format_currency(amount, currency):
    if amount is None:
        return ""
    return f"{amount:.2f} {currency}"
'''

RENAMED = '''
def to_money_string(value, unit):
    if value is None:
        return ""
    return f"{value:.2f} {unit}"
'''

REFORMATTED = '''
def format_currency(amount, currency):

    # normaliza el vacio
    if amount is None:
        return ""

    return f"{amount:.2f} {currency}"
'''

DIFFERENT = '''
def parse_currency(text):
    parts = text.split(" ")
    return float(parts[0]), parts[1]
'''


def test_a_copy_with_renamed_identifiers_hashes_the_same():
    """El caso central: es la misma función con otros nombres."""
    assert structural_hash(ORIGINAL) == structural_hash(RENAMED)


def test_comments_and_blank_lines_do_not_change_the_hash():
    assert structural_hash(ORIGINAL) == structural_hash(REFORMATTED)


def test_different_logic_hashes_differently():
    assert structural_hash(ORIGINAL) != structural_hash(DIFFERENT)


def test_hashing_the_raw_text_would_have_found_none_of_this():
    """Fija por qué existe la normalización, no solo que funciona."""
    import hashlib

    def raw(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()

    assert raw(ORIGINAL) != raw(RENAMED)
    assert raw(ORIGINAL) != raw(REFORMATTED)
    assert structural_hash(ORIGINAL) == structural_hash(RENAMED) == structural_hash(REFORMATTED)


def test_the_function_name_is_not_part_of_the_structure():
    """Renombrar es el disfraz; incluir el nombre haría inútil el nivel 0."""
    a = structural_hash("def alpha(x):\n    return x + 1\n")
    b = structural_hash("def beta(y):\n    return y + 1\n")
    assert a == b


def test_attribute_names_are_kept():
    """`x.commit()` y `x.rollback()` no son el mismo código."""
    a = structural_hash("def f(s):\n    return s.commit()\n")
    b = structural_hash("def f(s):\n    return s.rollback()\n")
    assert a != b, "borrar el nombre del atributo es donde el nivel 0 empieza a mentir"


def test_a_literal_is_reduced_to_its_type_not_its_value():
    """Dos funciones que difieren en un número mágico son la misma con otra constante."""
    a = structural_hash("def f():\n    return 42\n")
    b = structural_hash("def f():\n    return 7\n")
    assert a == b
    c = structural_hash("def f():\n    return 'x'\n")
    assert a != c, "un int y un str no son la misma constante"


# ---------------------------------------------------------------------------
# nivel 1 — el casi-duplicado
# ---------------------------------------------------------------------------

LONG_A = '''
def process_batch(items, retries, logger):
    results = []
    for item in items:
        attempt = 0
        while attempt < retries:
            try:
                value = item.transform()
                results.append(value)
                break
            except ValueError:
                attempt = attempt + 1
                logger.warn("retry")
        else:
            logger.error("gave up")
    return results
'''

LONG_B_EDITED = '''
def run_items(entries, max_tries, log):
    output = []
    for entry in entries:
        tries = 0
        while tries < max_tries:
            try:
                got = entry.transform()
                output.append(got)
                break
            except ValueError:
                tries = tries + 1
                log.warn("retry")
        else:
            log.error("gave up")
    metrics = len(output)
    return output
'''


def test_a_near_duplicate_scores_above_the_threshold():
    """Renombrado + una línea agregada: ya no es idéntico, sigue siendo el mismo código."""
    a, b = fingerprint(LONG_A), fingerprint(LONG_B_EDITED)
    assert a.structural_hash != b.structural_hash, "si fueran idénticos sería nivel 0"
    assert similarity(a, b) >= NEAR_DUPLICATE_THRESHOLD


def test_unrelated_code_scores_far_below_the_threshold():
    a = fingerprint(LONG_A)
    b = fingerprint('''
def render_template(name, context, engine):
    tpl = engine.load(name)
    for key in sorted(context):
        tpl = tpl.replace("{" + key + "}", str(context[key]))
    if not tpl:
        raise LookupError("empty template")
    return tpl.strip()
''')
    assert similarity(a, b) < NEAR_DUPLICATE_THRESHOLD


def test_a_short_body_is_not_eligible_for_level1():
    """Toda guard clause de dos líneas se parece a las demás."""
    fp = fingerprint("def f(x):\n    if not x:\n        return None\n    return x\n")
    assert fp.token_count < MIN_TOKENS_FOR_LEVEL1
    assert not fp.eligible_for_level1


def test_a_long_body_is_eligible():
    assert fingerprint(LONG_A).eligible_for_level1


# ---------------------------------------------------------------------------
# find_duplicates — la pregunta que un llamador hace de verdad
# ---------------------------------------------------------------------------

def _corpus() -> list[tuple[str, str, Fingerprint]]:
    return [
        ("fmt.format_currency", "fmt.py", fingerprint(ORIGINAL)),
        ("parse.parse_currency", "parse.py", fingerprint(DIFFERENT)),
        ("batch.process_batch", "batch.py", fingerprint(LONG_A)),
    ]


def test_an_exact_structural_copy_is_reported_as_level_zero():
    hits = find_duplicates(fingerprint(RENAMED), _corpus())
    assert hits
    assert hits[0].level == 0
    assert hits[0].qualified_name == "fmt.format_currency"
    assert "only the names differ" in hits[0].reason


def test_a_near_duplicate_is_reported_as_level_one():
    hits = find_duplicates(fingerprint(LONG_B_EDITED), _corpus())
    assert hits
    assert hits[0].level == 1
    assert hits[0].qualified_name == "batch.process_batch"
    assert "%" in hits[0].reason


def test_genuinely_new_code_reports_nothing():
    """El falso positivo es el error caro: dos y se apaga el check."""
    novel = fingerprint('''
def open_socket(host, port, timeout, backlog):
    sock = create(host, port)
    sock.settimeout(timeout)
    sock.listen(backlog)
    while not sock.ready():
        sock.poll()
    return sock
''')
    assert find_duplicates(novel, _corpus()) == []


def test_exact_matches_rank_before_near_matches():
    corpus = [*_corpus(), ("other.copy", "other.py", fingerprint(RENAMED))]
    hits = find_duplicates(fingerprint(ORIGINAL), corpus)
    assert [h.level for h in hits] == sorted(h.level for h in hits)


def test_the_result_is_capped():
    corpus = [(f"m.f{i}", f"m{i}.py", fingerprint(ORIGINAL)) for i in range(20)]
    assert len(find_duplicates(fingerprint(RENAMED), corpus, limit=3)) == 3


# ---------------------------------------------------------------------------
# los otros ocho lenguajes
# ---------------------------------------------------------------------------

def test_a_non_python_body_still_produces_a_fingerprint():
    ts = "function addUser(name: string): void {\n  store.push(name);\n}"
    fp = fingerprint(ts, language="typescript")
    assert fp.token_count > 0
    assert fp.structural_hash


def test_the_generic_tokenizer_still_catches_a_rename():
    a = fingerprint("function addUser(name) { store.push(name); }", language="javascript")
    b = fingerprint("function addMember(label) { store.push(label); }", language="javascript")
    assert a.structural_hash == b.structural_hash


def test_the_generic_tokenizer_keeps_keywords_distinct():
    a = fingerprint("function f(x) { return x; }", language="javascript")
    b = fingerprint("function f(x) { while (x) { break; } }", language="javascript")
    assert a.structural_hash != b.structural_hash


def test_unparseable_python_falls_back_instead_of_raising():
    """Un cuerpo cortado de un archivo puede no parsear solo. No puede explotar."""
    fp = fingerprint("    return x + 1  # method body sin su def", language="python")
    assert fp.token_count > 0


def test_a_method_body_sliced_at_an_indent_still_parses():
    body = "    def method(self, a):\n        return a + 1\n"
    assert fingerprint(body, language="python").token_count > 0


def test_tokenize_never_raises_on_garbage():
    for junk in ("", "   ", "@@@ not code @@@", "def (:"):
        assert isinstance(tokenize(junk), list)


# ---------------------------------------------------------------------------
# presupuesto — lo que hace que el check sobreviva a la primera semana
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", [ORIGINAL, LONG_A])
def test_fingerprinting_is_fast_enough_to_run_before_a_write(source: str):
    """§6.7: p95 < 150 ms por hook, y esto es solo una parte de ese presupuesto."""
    import time

    t = time.perf_counter()
    for _ in range(50):
        fingerprint(source)
    per_call_ms = (time.perf_counter() - t) * 1000 / 50
    assert per_call_ms < 10, f"{per_call_ms:.1f} ms por fingerprint"


# ---------------------------------------------------------------------------
# el corpus cacheado — lo que hace que esto sobreviva delante de un write
# ---------------------------------------------------------------------------

def _mini_repo(tmp_path):
    from livespec_mcp.config import Settings
    from livespec_mcp.domain.indexer import index_project
    from livespec_mcp.storage.db import connect

    (tmp_path / "a.py").write_text(ORIGINAL + LONG_A, encoding="utf-8")
    (tmp_path / "b.py").write_text(DIFFERENT, encoding="utf-8")
    state = tmp_path / ".mcp-docs"
    st = Settings(workspace=tmp_path, state_dir=state,
                  db_path=state / "docs.db", docs_dir=state / "docs")
    st.ensure_dirs()
    conn = connect(st.db_path)
    index_project(st, conn, force=True)
    import sqlite3
    conn.row_factory = sqlite3.Row
    pids = tuple(r[0] for r in conn.execute("SELECT id FROM project"))
    return conn, pids, tmp_path


def test_the_corpus_covers_the_indexed_functions(tmp_path):
    from livespec_mcp.domain.duplication import load_corpus

    conn, pids, root = _mini_repo(tmp_path)
    corpus = load_corpus(conn, pids, root)
    names = {q for q, _p, _f in corpus}
    assert any("format_currency" in n for n in names)
    assert any("process_batch" in n for n in names)


def test_the_second_load_reads_no_files(tmp_path, monkeypatch):
    """El costo era leer 1443 cuerpos de disco en CADA llamada: 764 ms medidos."""
    from livespec_mcp.domain import duplication
    from livespec_mcp.domain.duplication import load_corpus

    conn, pids, root = _mini_repo(tmp_path)
    load_corpus(conn, pids, root)  # llena el cache

    def explode(*a, **k):
        raise AssertionError("una carga con el cache caliente no debe tocar el disco")

    monkeypatch.setattr(duplication, "fingerprint", explode)
    corpus = load_corpus(conn, pids, root)
    assert corpus, "el cache devolvió un corpus vacío"


def test_the_cache_is_keyed_by_body_not_by_symbol_id(tmp_path):
    """Un rename o un move no cambian el cuerpo: la huella sigue siendo válida."""
    from livespec_mcp.domain.duplication import load_corpus

    conn, pids, root = _mini_repo(tmp_path)
    load_corpus(conn, pids, root)
    rows = conn.execute("SELECT body_hash FROM symbol_fingerprint").fetchall()
    assert rows, "no se cacheó nada"
    assert all(len(r[0]) > 8 for r in rows), "la clave es el hash del cuerpo"


def test_a_changed_body_simply_misses(tmp_path):
    """La invalidación entera: cuerpo nuevo, hash nuevo, miss. Sin barrido."""
    from livespec_mcp.domain.duplication import load_corpus

    conn, pids, root = _mini_repo(tmp_path)
    load_corpus(conn, pids, root)
    before = conn.execute("SELECT COUNT(*) FROM symbol_fingerprint").fetchone()[0]

    (root / "a.py").write_text(
        ORIGINAL + LONG_A + "\n\ndef nueva(x, y, z):\n    return x + y + z\n",
        encoding="utf-8",
    )
    from livespec_mcp.config import Settings
    from livespec_mcp.domain.indexer import index_project
    st = Settings(workspace=root, state_dir=root / ".mcp-docs",
                  db_path=root / ".mcp-docs" / "docs.db",
                  docs_dir=root / ".mcp-docs" / "docs")
    index_project(st, conn, force=True)
    load_corpus(conn, pids, root)

    after = conn.execute("SELECT COUNT(*) FROM symbol_fingerprint").fetchone()[0]
    assert after > before, "el cuerpo nuevo tenía que agregar una entrada"
