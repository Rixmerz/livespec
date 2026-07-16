"""Regression tests for the audit batch P5 performance fixes (FTS-path only —
embeddings smoke needs network for the model download)."""

from __future__ import annotations

import struct
from pathlib import Path

import networkx as nx
import pytest

from livespec_mcp.domain.graph import GraphView, graph_pagerank
from livespec_mcp.domain.rag import (
    _prune_orphan_vectors,
    ensure_vec_tables,
    have_sqlite_vec,
    rebuild_chunks,
)
from livespec_mcp.storage.db import connect, get_or_create_project


def _seed_symbol_db(tmp: Path):
    (tmp / "m.py").write_text("def a():\n    return 1\n\ndef b():\n    return a()\n")
    conn = connect(tmp / "x.db")
    pid = get_or_create_project(conn, "p", str(tmp))
    conn.execute(
        "INSERT INTO file(project_id,path,language,content_hash,line_count,mtime)"
        " VALUES(?,?,?,?,?,?)",
        (pid, "m.py", "python", "h", 5, 0.0),
    )
    fid = conn.execute("SELECT id FROM file WHERE path=?", ("m.py",)).fetchone()["id"]
    for nm, sl, el in (("m.a", 1, 2), ("m.b", 4, 5)):
        conn.execute(
            "INSERT INTO symbol(file_id,name,qualified_name,kind,start_line,end_line)"
            " VALUES(?,?,?,?,?,?)",
            (fid, nm.split(".")[-1], nm, "function", sl, el),
        )
    return conn, pid


def test_graph_pagerank_is_cached_on_view():
    g = nx.DiGraph()
    g.add_edges_from([(1, 2), (2, 3)])
    view = GraphView(g=g, sym_meta={})
    r1 = graph_pagerank(view)
    assert view._pagerank is not None
    r2 = graph_pagerank(view)
    assert r1 is r2  # same object, not recomputed


def test_rebuild_chunks_preserves_embedded_at_when_unchanged(tmp_path: Path):
    conn, pid = _seed_symbol_db(tmp_path)
    rebuild_chunks(conn, pid)
    n1 = conn.execute("SELECT COUNT(*) c FROM chunk WHERE project_id=?", (pid,)).fetchone()["c"]
    conn.execute("UPDATE chunk SET embedded_at='2020-01-01' WHERE project_id=?", (pid,))
    rebuild_chunks(conn, pid)  # unchanged -> reuse rows, keep embedded_at
    preserved = conn.execute(
        "SELECT COUNT(*) c FROM chunk WHERE project_id=? AND embedded_at IS NOT NULL", (pid,)
    ).fetchone()["c"]
    n2 = conn.execute("SELECT COUNT(*) c FROM chunk WHERE project_id=?", (pid,)).fetchone()["c"]
    assert n1 == n2 and preserved == n2


def test_rebuild_chunks_deletes_stale_source(tmp_path: Path):
    conn, pid = _seed_symbol_db(tmp_path)
    rebuild_chunks(conn, pid)
    # remove symbol b; its chunk must be pruned on rebuild
    conn.execute("DELETE FROM symbol WHERE qualified_name='m.b'")
    rebuild_chunks(conn, pid)
    remaining = {
        r["source_id"]
        for r in conn.execute(
            "SELECT source_id FROM chunk WHERE project_id=? AND source_type='symbol'", (pid,)
        )
    }
    a_id = conn.execute("SELECT id FROM symbol WHERE qualified_name='m.a'").fetchone()["id"]
    assert remaining == {a_id}


@pytest.mark.embeddings
def test_prune_orphan_vectors(tmp_path: Path):
    conn, pid = _seed_symbol_db(tmp_path)
    rebuild_chunks(conn, pid)
    if not have_sqlite_vec(conn):
        pytest.skip("sqlite-vec not installed")
    ensure_vec_tables(conn)
    cid = conn.execute("SELECT id FROM chunk WHERE project_id=?", (pid,)).fetchone()["id"]
    blob = struct.pack(f"{768}f", *([0.1] * 768))
    conn.execute("INSERT INTO chunk_vec_code(chunk_id, embedding) VALUES(?,?)", (cid, blob))
    conn.execute("INSERT INTO chunk_vec_code(chunk_id, embedding) VALUES(?,?)", (999999, blob))
    _prune_orphan_vectors(conn)
    after = conn.execute("SELECT COUNT(*) c FROM chunk_vec_code").fetchone()["c"]
    assert after == 1
