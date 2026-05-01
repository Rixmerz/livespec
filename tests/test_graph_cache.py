"""v0.6 P3: graph cache. load_graph returns the same object across calls
when no new index_run has happened, and invalidates when a new run lands."""

from __future__ import annotations

from pathlib import Path

from livespec_mcp.config import Settings
from livespec_mcp.domain.graph import invalidate_graph_cache, load_graph
from livespec_mcp.domain.indexer import index_project
from livespec_mcp.storage.db import connect


def _bootstrap(tmp_path: Path):
    state = tmp_path / ".mcp-docs"
    settings = Settings(
        workspace=tmp_path,
        state_dir=state,
        db_path=state / "docs.db",
        docs_dir=state / "docs",
        models_dir=state / "models",
    )
    settings.ensure_dirs()
    return settings, connect(settings.db_path)


def test_graph_cache_hits_on_repeated_call(tmp_path: Path):
    invalidate_graph_cache()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text("def a():\n    return b()\n\ndef b():\n    return 1\n")

    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)
    pid = conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"]

    v1 = load_graph(conn, pid)
    v2 = load_graph(conn, pid)
    # Cache hit: same object, no rebuild
    assert v1 is v2, "expected the same GraphView instance from cache"
    conn.close()


def test_graph_cache_invalidates_after_new_index_run(tmp_path: Path):
    invalidate_graph_cache()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    target = pkg / "m.py"
    target.write_text("def a():\n    return b()\n\ndef b():\n    return 1\n")

    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)
    pid = conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"]

    v1 = load_graph(conn, pid)

    # Touch the file -> new index run -> different cache key
    target.write_text(target.read_text() + "\n# touch\n")
    index_project(settings, conn)

    v2 = load_graph(conn, pid)
    assert v1 is not v2, "expected fresh GraphView after new index run"
    conn.close()


def test_invalidate_graph_cache_clears_all(tmp_path: Path):
    invalidate_graph_cache()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text("def a():\n    return 1\n")

    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)
    pid = conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"]

    v1 = load_graph(conn, pid)
    dropped = invalidate_graph_cache()
    assert dropped >= 1
    v2 = load_graph(conn, pid)
    assert v1 is not v2
    conn.close()
