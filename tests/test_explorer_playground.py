"""Explorer MCP playground bridge (Try it API)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from livespec_mcp.config import RepoConfig
from livespec_mcp.explorer.asgi import create_explorer_host_app, mount_explorer
from livespec_mcp.explorer.playground import playground_enabled, playground_mode
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route


def _write_minimal_bundle(workspace: Path) -> None:
    out = workspace / ".mcp-docs" / "explorer"
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(
        "<html><title>Spec Explorer</title></html>", encoding="utf-8"
    )
    (out / "data.json").write_text("{}", encoding="utf-8")


def test_playground_mode_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = RepoConfig()
    assert playground_mode(cfg) == "readonly"
    assert playground_enabled(cfg, host_serve=True) is True
    assert playground_enabled(cfg, host_serve=False) is False

    cfg_on = RepoConfig(explorer_playground=True, explorer_playground_mode="all")
    assert playground_enabled(cfg_on, host_serve=False) is True
    assert playground_mode(cfg_on) == "all"

    monkeypatch.setenv("LIVESPEC_EXPLORER_PLAYGROUND", "all")
    assert playground_mode(RepoConfig()) == "all"


def test_host_serve_playground_info(workspace: Path) -> None:
    _write_minimal_bundle(workspace)
    app = create_explorer_host_app(workspace)
    client = TestClient(app)
    info = client.get("/explorer/api/playground").json()
    assert info["enabled"] is True
    assert info["mode"] == "readonly"
    assert Path(info["workspace"]).resolve() == workspace.resolve()


def test_mount_playground_off_by_default(workspace: Path) -> None:
    _write_minimal_bundle(workspace)
    app = Starlette(routes=[Route("/", lambda r: PlainTextResponse("ok"))])
    mount_explorer(app, workspace=workspace)
    client = TestClient(app)
    info = client.get("/explorer/api/playground").json()
    assert info["enabled"] is False
    denied = client.post(
        "/explorer/api/call_tool",
        json={"name": "list_specs", "arguments": {}},
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_call_tool_readonly_list_specs(workspace: Path) -> None:
    _write_minimal_bundle(workspace)
    # Index so list_specs has a project
    from fastmcp import Client

    from livespec_mcp.server import mcp

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})

    app = create_explorer_host_app(workspace)
    client = TestClient(app)
    res = client.post(
        "/explorer/api/call_tool",
        json={"name": "list_specs", "arguments": {"limit": 5}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("ok") is True
    assert body.get("tool") == "list_specs"
    assert "result" in body
    # workspace was injected even if omitted from arguments
    assert isinstance(body["result"], dict)


@pytest.mark.asyncio
async def test_call_tool_rejects_mutation_in_readonly(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LIVESPEC_EXPLORER_PLAYGROUND", raising=False)
    _write_minimal_bundle(workspace)
    from fastmcp import Client

    from livespec_mcp.server import mcp

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})

    app = create_explorer_host_app(workspace)
    client = TestClient(app)
    res = client.post(
        "/explorer/api/call_tool",
        json={
            "name": "create_spec",
            "arguments": {"title": "Nope", "spec_id": "SPEC-NOPE"},
        },
    )
    assert res.status_code == 403
    assert res.json().get("isError") is True


@pytest.mark.asyncio
async def test_call_tool_all_mode_allows_create(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVESPEC_EXPLORER_PLAYGROUND", "all")
    _write_minimal_bundle(workspace)
    from fastmcp import Client

    from livespec_mcp.server import mcp

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})

    app = create_explorer_host_app(workspace)
    client = TestClient(app)
    res = client.post(
        "/explorer/api/call_tool",
        json={
            "name": "create_spec",
            "arguments": {"title": "Playground", "spec_id": "SPEC-PG1"},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("ok") is True or (
        isinstance(body.get("result"), dict) and not body.get("isError")
    )
