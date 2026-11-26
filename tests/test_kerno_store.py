"""EXPERIMENTAL Kerno mirror (storage/kerno_store.py): parity with SQLite.

Skipped without the optional `kernodb` package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

kerno = pytest.importorskip("kerno")

from livespec_mcp.config import Settings  # noqa: E402
from livespec_mcp.domain import graph as graph_mod  # noqa: E402
from livespec_mcp.domain.indexer import index_project  # noqa: E402
from livespec_mcp.domain.rag import rebuild_chunks  # noqa: E402
from livespec_mcp.storage import kerno_store  # noqa: E402
from livespec_mcp.storage.db import connect  # noqa: E402
from livespec_mcp.storage.kerno_store import KernoMirror  # noqa: E402

SRC = '''\
def helper(x):
    """Double the cookie count."""
    return x * 2


def caller(y):
    return helper(y) + 1


class Jar:
    def total(self):
        return caller(3)
'''


def _index(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "app.py").write_text(SRC)
    state = tmp_path / "state"
    settings = Settings(
        workspace=ws, state_dir=state, db_path=state / "docs.db", docs_dir=state / "docs"
    )
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    index_project(settings, conn)
    pid = int(conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"])
    rebuild_chunks(conn, pid)
    conn.commit()
    return settings, conn, pid


def _sql_view(conn, pid):
    graph_mod.invalidate_graph_cache(None)
    return graph_mod.load_graph(conn, pid)


def _edges(view):
    return {(u, v, d["edge_type"], d["weight"], d["origin"]) for u, v, d in view.g.edges(data=True)}


def test_mirror_graph_equals_sql_graph(tmp_path):
    _settings, conn, pid = _index(tmp_path)
    view = _sql_view(conn, pid)
    with KernoMirror(tmp_path / "k") as m:
        m.sync(conn, pid, run_id=1)
        meta, edges = m.graph()
    assert meta == view.sym_meta
    assert set(edges) == _edges(view)
    assert edges, "fixture must produce at least one edge"


def test_lookups_and_callers_match_sql(tmp_path):
    _settings, conn, pid = _index(tmp_path)
    with KernoMirror(tmp_path / "k") as m:
        m.sync(conn, pid, run_id=1)
        helper = m.find_symbol("helper")
        assert [s["qualified_name"] for s in helper] == ["app.helper"]
        assert m.find_qname("app.helper") == helper
        assert {s["name"] for s in m.symbols_in_file("app.py")} >= {"helper", "caller", "Jar"}
        callers = {s["name"] for s in m.callers(helper[0]["id"])}
        sql = {
            r["name"]
            for r in conn.execute(
                """SELECT s.name FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id
                   WHERE e.dst_symbol_id=? AND e.edge_type='calls'""",
                (helper[0]["id"],),
            )
        }
        assert callers == sql == {"caller"}


def test_resync_is_a_noop_then_tracks_an_edit(tmp_path):
    settings, conn, pid = _index(tmp_path)
    with KernoMirror(tmp_path / "k") as m:
        first = m.sync(conn, pid, run_id=1)
        assert first["symbols"]["created"] > 0
        again = m.sync(conn, pid, run_id=1)
        assert again["symbols"]["created"] == again["symbols"]["updated"] == 0
        assert again["edges"]["added"] == again["edges"]["removed"] == 0
        assert again["chunks"]["created"] == again["chunks"]["updated"] == 0

        (settings.workspace / "app.py").write_text(SRC + "\n\ndef extra():\n    return helper(1)\n")
        index_project(settings, conn)
        rebuild_chunks(conn, pid)
        conn.commit()
        m.sync(conn, pid, run_id=2)
        view = _sql_view(conn, pid)
        meta, edges = m.graph()
        assert meta == view.sym_meta
        assert set(edges) == _edges(view)
        assert m.run_id() == 2


def test_keyword_and_substring_search_see_every_chunk(tmp_path):
    """Guards the workaround for kernodb 1.0.0a4: chunks are raw payloads
    because the keyword index skips non-UTF-8 (i.e. document) payloads."""
    _settings, conn, pid = _index(tmp_path)
    with KernoMirror(tmp_path / "k") as m:
        m.sync(conn, pid, run_id=1)
        n_fts = conn.execute(
            "SELECT COUNT(*) c FROM chunk_fts WHERE chunk_fts MATCH 'return'"
        ).fetchone()["c"]
        assert len(m.search("return", 100)) == n_fts > 0
        hit = m.search("cookie", 5)[0]
        assert hit["file_path"] == "app.py" and "helper" in hit["snippet"]
        like = {
            int(r["id"]) for r in conn.execute("SELECT id FROM chunk WHERE text LIKE '%helper(y)%'")
        }
        assert set(m.grep("helper(y)")) == like != set()
        assert m.search("zzzznotaword", 5) == []


def test_edge_attributes_survive_the_relation_type_encoding():
    enc = kerno_store._edge_type
    dec = kerno_store._parse_edge_type
    assert dec(enc("calls", 1.0, "livespec")) == ("calls", 1.0, "livespec")
    assert dec(enc("invokes_route", 0.8, None)) == ("invokes_route", 0.8, "livespec")
    assert dec(enc("calls", 0.5, "external:graphify")) == ("calls", 0.5, "external:graphify")


def test_shadow_mode_serves_load_graph_and_falls_back_when_stale(tmp_path, monkeypatch):
    monkeypatch.setenv(kerno_store.ENV_FLAG, "1")
    _settings, conn, pid = _index(tmp_path)  # index_project synced the mirror
    before = dict(kerno_store.COUNTERS)
    view = _sql_view(conn, pid)  # load_graph, cache dropped: goes to Kerno
    assert kerno_store.COUNTERS["graph_served"] == before["graph_served"] + 1

    monkeypatch.delenv(kerno_store.ENV_FLAG)
    sql_view = _sql_view(conn, pid)
    assert view.sym_meta == sql_view.sym_meta
    assert _edges(view) == _edges(sql_view)

    # A mirror whose fingerprint is not the current edge set must not be trusted.
    monkeypatch.setenv(kerno_store.ENV_FLAG, "1")
    db_path = graph_mod._db_path(conn)
    with KernoMirror(kerno_store.mirror_path(db_path, pid)) as m:
        m.db.upsert("meta", "key", {"key": "index", "run_id": 999, "fp": "stale"})
    fallback = kerno_store.COUNTERS["graph_fallback"]
    stale = _sql_view(conn, pid)
    assert kerno_store.COUNTERS["graph_fallback"] == fallback + 1
    assert stale.sym_meta == sql_view.sym_meta


def test_noop_resync_writes_nothing_and_chunk_edit_is_still_seen(tmp_path):
    settings, conn, pid = _index(tmp_path)
    with KernoMirror(tmp_path / "k") as m:
        m.sync(conn, pid, run_id=1)
        seq = m.db.last_commit_seq()
        again = m.sync(conn, pid, run_id=1)
        assert again.get("skipped") == "unchanged"
        assert again["chunks"]["created"] == again["chunks"]["updated"] == 0
        assert m.db.last_commit_seq() == seq

        # a chunk text change with the same run id and edge set is not skipped
        conn.execute(
            "UPDATE chunk SET text = text || ' edited' WHERE id = (SELECT MIN(id) FROM chunk WHERE project_id = ?)",
            (pid,),
        )
        conn.commit()
        changed = m.sync(conn, pid, run_id=1)
        assert "skipped" not in changed
        assert changed["chunks"]["updated"] == 1
        assert m.sync(conn, pid, run_id=1).get("skipped") == "unchanged"
