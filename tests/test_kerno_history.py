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
    assert h["edges_removed"] == 0  # the edge went with the removed unit
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
