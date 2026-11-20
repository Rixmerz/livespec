"""Experimental Kerno-backed index history (LIVESPEC_KERNO=1).

Skipped unless a `kernodb` with revisions (`Database.history`) is installed.
"""

from __future__ import annotations

import pytest

kerno = pytest.importorskip("kerno")
if not hasattr(kerno.Database, "history"):
    pytest.skip("kernodb without history support", allow_module_level=True)

from livespec_mcp.domain import kerno_history as kh  # noqa: E402
from livespec_mcp.state import get_state  # noqa: E402
from livespec_mcp.tools.indexing import run_index_pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def _kerno_on(monkeypatch):
    monkeypatch.setenv("LIVESPEC_KERNO", "1")
    yield
    kh.close_all()


def _index(ws):
    st = get_state(ws, create=True)
    return st, run_index_pipeline(st)


def _run_id(st):
    return st.conn.execute("SELECT MAX(id) FROM index_run").fetchone()[0]


def test_revisions_follow_semantic_changes_only(workspace):
    src = workspace / "mod.py"
    src.write_text("def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n")
    st, out = _index(workspace)
    first = out["kerno_history"]
    assert first["symbols_added"] >= 2 and first["first_sync"] is True
    assert first["edges_added"] >= 1
    run1 = _run_id(st)

    # Reformat only: no new revision.
    src.write_text("def helper():\n\n    return 1\n\n\ndef caller():\n    return helper()\n")
    _, out = _index(workspace)
    assert out["kerno_history"]["symbols_changed"] == 0

    # Real change: one new revision of helper.
    src.write_text("def helper():\n    return 2\n\n\ndef caller():\n    return helper()\n")
    _, out = _index(workspace)
    assert out["kerno_history"]["symbols_changed"] == 1

    revs = kh.symbol_revisions(st.project_id, st.settings.db_path, "mod.helper")
    assert not isinstance(revs, kh.KernoProblem), revs
    kinds = [r["kind"] for r in revs["units"][0]["revisions"]]
    assert kinds == ["update", "create"]

    then = kh.symbol_as_of(st.project_id, st.settings.db_path, "mod.helper", run1)
    assert not isinstance(then, kh.KernoProblem), then
    assert then["states"][0]["state"] == "present"
    assert then["states"][0]["same_as_now"] is False


def test_removed_symbol_keeps_tombstone_and_edges_follow(workspace):
    src = workspace / "mod.py"
    src.write_text("def gone():\n    return 1\n\n\ndef caller():\n    return gone()\n")
    st, _ = _index(workspace)
    src.write_text("def caller():\n    return 0\n")
    _, out = _index(workspace)
    h = out["kerno_history"]
    assert h["symbols_removed"] == 1
    assert h["edges_removed"] <= 1  # counted once, whether stale or cascaded
    report = kh.verify(st.settings.db_path)
    assert report["clean"] is True and report["relations"] == 0


def test_disabled_by_default(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_KERNO")
    (workspace / "a.py").write_text("def f():\n    return 1\n")
    _, out = _index(workspace)
    assert "kerno_history" not in out


async def test_tools_shape(workspace):
    from fastmcp import Client, FastMCP

    from livespec_mcp.tools import kerno as kerno_tools

    (workspace / "a.py").write_text("def f():\n    return 1\n")
    _index(workspace)
    app = FastMCP("kerno-test")
    kerno_tools.register(app)
    async with Client(app) as c:
        names = {t.name for t in await c.list_tools()}
        assert names == set(kerno_tools.KERNO_TOOL_NAMES)
        ok = (await c.call_tool("kerno_verify_history", {"workspace": str(workspace)})).data
        assert ok["clean"] is True
        miss = (
            await c.call_tool(
                "kerno_symbol_revisions", {"qname": "nope", "workspace": str(workspace)}
            )
        ).data
        assert miss["isError"] is True


def test_removing_both_ends_of_an_edge_does_not_wedge_the_sync(workspace):
    # The edge shows up from both removed units; unrelating it twice made Kerno
    # reject the change set, and every later sync retried the same batch.
    src = workspace / "mod.py"
    src.write_text("def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n")
    st, _ = _index(workspace)
    src.unlink()
    _, out = _index(workspace)
    h = out["kerno_history"]
    assert "error" not in h, h
    assert h["symbols_removed"] == 2
    assert kh.verify(st.settings.db_path)["relations"] == 0


def test_same_qname_twice_in_a_file_keeps_both(workspace):
    (workspace / "a.py").write_text(
        "if True:\n    def f():\n        return 1\nelse:\n    def f():\n        return 2\n"
    )
    st, out = _index(workspace)
    n = st.conn.execute("SELECT COUNT(*) FROM symbol").fetchone()[0]
    assert out["kerno_history"]["symbols"] == n


def test_rebuilt_index_starts_a_new_generation(workspace):
    from livespec_mcp import state as state_module

    f = workspace / "a.py"
    for v in (1, 2):
        f.write_text(f"def g():\n    return {v}\n")
        _index(workspace)
    state_module.reset_state()
    kh.close_all()
    for p in (workspace / ".mcp-docs").glob("docs.db*"):
        p.unlink()
    f.write_text("def g():\n    return 3\n")
    st, out = _index(workspace)
    assert out["kerno_history"]["generation"] == 2
    a = kh.symbol_as_of(st.project_id, st.settings.db_path, "a.g", 1)
    assert a["states"][0]["same_as_now"] is True  # run 1 of the new generation, not the old one


def test_unchanged_run_skips_the_sync(workspace):
    (workspace / "a.py").write_text("def f():\n    return 1\n")
    _index(workspace)
    _, out = _index(workspace)
    assert out["kerno_history"] == {"skipped": "no file changes"}
