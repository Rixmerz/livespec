"""Bug #11 regression: read/write tools must never materialize
``.mcp-docs/docs.db`` in a workspace that was never ``index_project``-ed.

Before this fix, ANY tool call against a typo'd/unindexed ``workspace``
silently created an empty SQLite DB via ``get_state()``'s implicit
``connect()``. A read like ``list_specs`` on a fresh directory returned
``{"specs": []}`` — indistinguishable from "indexed, but empty" — while
quietly leaving an orphan ``.mcp-docs/`` behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import get_state
from livespec_mcp.storage.db import connect
from livespec_mcp.workspace_param import WorkspaceNotIndexedError


def test_get_state_default_does_not_create_db(tmp_path: Path):
    """get_state(workspace) with the default create=False raises and creates
    nothing — no .mcp-docs anywhere under tmp_path."""
    with pytest.raises(WorkspaceNotIndexedError, match="not indexed"):
        get_state(str(tmp_path))
    assert not any(tmp_path.rglob(".mcp-docs"))


def test_get_state_create_true_builds_it_and_reads_work_after(tmp_path: Path):
    """The one sanctioned bootstrap path still works, and a subsequent
    default (create=False) call against the now-indexed workspace succeeds."""
    st = get_state(str(tmp_path), create=True)
    assert (tmp_path / ".mcp-docs" / "docs.db").is_file()
    assert st.project_id is not None

    # A fresh get_state() call (simulating a new AppState, not the LRU cache
    # hit) with the default create=False must now succeed read-write.
    from livespec_mcp.state import reset_state

    reset_state()
    st2 = get_state(str(tmp_path))
    assert st2.project_id == st.project_id


@pytest.mark.asyncio
async def test_read_tool_on_unindexed_workspace_errors_clean_no_db_created(tmp_path: Path):
    """End-to-end via the real MCP server: a read tool (list_specs) pointed
    at a directory that was never indexed gets the structured
    {error, hint} payload — not a raw exception, and not silent success —
    and leaves no .mcp-docs behind."""
    async with Client(mcp) as c:
        result = await c.call_tool("list_specs", {"workspace": str(tmp_path)})
    data = result.data if hasattr(result, "data") else result.structured_content
    assert data.get("isError") is True
    assert "not indexed" in data.get("error", "").lower()
    assert "index_project" in data.get("hint", "")
    assert not any(tmp_path.rglob(".mcp-docs"))


@pytest.mark.asyncio
async def test_index_project_then_read_tool_works(tmp_path: Path):
    """The exempted bootstrap tool (index_project) still creates the DB, and
    reads against that now-indexed workspace succeed normally."""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    async with Client(mcp) as c:
        idx = (await c.call_tool("index_project", {"workspace": str(tmp_path)})).data
        assert idx["symbols_total"] >= 1
        listed = (await c.call_tool("list_specs", {"workspace": str(tmp_path)})).data
    assert listed == {"specs": [], "total": 0, "next_cursor": None, "truncated": False}
    assert (tmp_path / ".mcp-docs" / "docs.db").is_file()


def test_db_connect_create_false_never_creates_file(tmp_path: Path):
    """The low-level primitive: create=False raises FileNotFoundError and
    touches nothing on disk."""
    missing = tmp_path / "nested" / "docs.db"
    with pytest.raises(FileNotFoundError):
        connect(missing, create=False)
    assert not missing.parent.exists()


def test_db_connect_create_false_opens_existing_readonly(tmp_path: Path):
    db_path = tmp_path / "docs.db"
    conn = connect(db_path, create=True)
    conn.execute(
        "INSERT INTO project(name, root) VALUES (?, ?)", ("x", str(tmp_path))
    )
    conn.close()

    ro = connect(db_path, create=False)
    row = ro.execute("SELECT name FROM project WHERE root=?", (str(tmp_path),)).fetchone()
    assert row["name"] == "x"
    with pytest.raises(Exception):
        ro.execute("INSERT INTO project(name, root) VALUES (?, ?)", ("y", "other"))
    ro.close()
