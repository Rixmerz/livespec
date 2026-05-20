"""Per-call workspace= isolates indexes (multi-tenant MCP)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import get_state, reset_state


@pytest.fixture(autouse=True)
def _clear_state_cache():
    reset_state()
    yield
    reset_state()


def _write_py(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_two_workspaces_isolated_indexes(tmp_path: Path):
    ws_a = tmp_path / "proj_a"
    ws_b = tmp_path / "proj_b"
    _write_py(ws_a, "a.py", "def alpha():\n    return 1\n")
    _write_py(ws_b, "b.py", "def beta():\n    return 2\n")

    async with Client(mcp) as client:
        ra = await client.call_tool(
            "index_project",
            {"workspace": str(ws_a), "force": True},
        )
        rb = await client.call_tool(
            "index_project",
            {"workspace": str(ws_b), "force": True},
        )

    assert ra.data["symbols_total"] >= 1
    assert rb.data["symbols_total"] >= 1
    assert str(ra.data["workspace"]) == str(ws_a.resolve())
    assert str(rb.data["workspace"]) == str(ws_b.resolve())

    st_a = get_state(ws_a)
    st_b = get_state(ws_b)
    assert st_a.settings.db_path != st_b.settings.db_path
    names_a = {
        r["qualified_name"]
        for r in st_a.conn.execute(
            "SELECT qualified_name FROM symbol s JOIN file f ON f.id=s.file_id "
            "WHERE f.project_id=?",
            (st_a.project_id,),
        )
    }
    names_b = {
        r["qualified_name"]
        for r in st_b.conn.execute(
            "SELECT qualified_name FROM symbol s JOIN file f ON f.id=s.file_id "
            "WHERE f.project_id=?",
            (st_b.project_id,),
        )
    }
    assert any("alpha" in n for n in names_a)
    assert any("beta" in n for n in names_b)
    assert not any("beta" in n for n in names_a)


def test_get_state_rejects_missing_workspace_argument(monkeypatch):
    from livespec_mcp import state as sm
    from livespec_mcp.workspace_param import WorkspaceRequiredError

    def _strict(path: str | Path | None = None) -> Path:
        if path is None or (isinstance(path, str) and not str(path).strip()):
            raise WorkspaceRequiredError("workspace is required")
        return Path(str(path)).expanduser().resolve()

    monkeypatch.setattr(sm, "_resolve_workspace", _strict)
    with pytest.raises(WorkspaceRequiredError, match="workspace is required"):
        get_state(None)


def test_get_state_rejects_missing_directory(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError, match="Workspace directory not found"):
        get_state(missing)
