"""Spec Explorer ASGI mount + FastAPI autowire."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from livespec_mcp.explorer.asgi import mount_explorer
from livespec_mcp.explorer.autowire import autowire_fastapi_explorer, find_fastapi_entrypoints
from livespec_mcp.state import get_state
from livespec_mcp.tools.explorer import write_explorer_bundle


def _write_minimal_bundle(workspace: Path) -> None:
    out = workspace / ".mcp-docs" / "explorer"
    out.mkdir(parents=True)
    (out / "index.html").write_text("<html><title>Spec Explorer</title></html>", encoding="utf-8")
    (out / "data.json").write_text("{}", encoding="utf-8")


def test_mount_explorer_serves_spa_routes(workspace: Path):
    _write_minimal_bundle(workspace)
    app = Starlette(routes=[Route("/", lambda r: PlainTextResponse("api"))])
    mount_explorer(app, workspace=workspace)

    client = TestClient(app)
    assert client.get("/explorer", follow_redirects=False).status_code == 307
    assert "Spec Explorer" in client.get("/explorer/").text
    assert client.get("/explorer/endpoints").status_code == 200
    assert client.get("/explorer/data.json").json() == {}


def test_find_fastapi_entrypoint(workspace: Path):
    (workspace / "main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")
    found = find_fastapi_entrypoints(workspace)
    assert len(found) == 1
    assert found[0][1] == "app"


def test_autowire_appends_mount_block(workspace: Path):
    main = workspace / "main.py"
    main.write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")
    result = autowire_fastapi_explorer(workspace, auto_mount=True)
    assert result.wired is True
    text = main.read_text(encoding="utf-8")
    assert 'mount_explorer(app, prefix="/explorer")' in text
    again = autowire_fastapi_explorer(workspace, auto_mount=True)
    assert again.wired is False
    assert again.reason == "already_wired"


def test_autowire_uses_mount_path_from_config(workspace: Path):
    main = workspace / "main.py"
    main.write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")
    (workspace / ".livespec.toml").write_text(
        '[explorer]\nauto_mount = true\nmount_path = "/api/docs"\n',
        encoding="utf-8",
    )
    result = autowire_fastapi_explorer(
        workspace, auto_mount=True, mount_path="/api/docs"
    )
    assert result.wired is True
    text = main.read_text(encoding="utf-8")
    assert 'mount_explorer(app, prefix="/api/docs")' in text


def test_write_explorer_bundle_autowires_fastapi(workspace: Path):
    (workspace / "main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")
    st = get_state(str(workspace), create=True)
    result = write_explorer_bundle(st)
    assert result["autowire"]["wired"] is True
    assert 'mount_explorer(app, prefix="/explorer")' in (workspace / "main.py").read_text(encoding="utf-8")


def test_enable_explorer_mounts_with_config_prefix(workspace: Path):
    _write_minimal_bundle(workspace)
    (workspace / ".livespec.toml").write_text(
        '[explorer]\nmount_path = "/spec-explorer"\n',
        encoding="utf-8",
    )
    from livespec_mcp.explorer.fastapi import enable_explorer

    app = Starlette(routes=[Route("/", lambda r: PlainTextResponse("api"))])
    prefix = enable_explorer(app, workspace=workspace)
    assert prefix == "/spec-explorer"

    client = TestClient(app)
    assert client.get("/spec-explorer/", follow_redirects=True).status_code == 200


def test_explorer_middleware_mounts_without_main_patch(workspace: Path):
    _write_minimal_bundle(workspace)
    from livespec_mcp.explorer.fastapi import LivespecExplorerMiddleware

    inner = Starlette(routes=[Route("/health", lambda r: PlainTextResponse("ok"))])
    app = LivespecExplorerMiddleware(inner, workspace=workspace)
    client = TestClient(app)
    assert client.get("/health").text == "ok"
    assert "Spec Explorer" in client.get("/explorer/", follow_redirects=True).text


def test_explorer_host_app_redirects_index_html_to_mount(workspace: Path):
    _write_minimal_bundle(workspace)
    from livespec_mcp.explorer.asgi import create_explorer_host_app

    app = create_explorer_host_app(workspace)
    client = TestClient(app)
    assert client.get("/", follow_redirects=False).headers["location"] == "/explorer/"
    assert client.get("/index.html", follow_redirects=False).headers["location"] == "/explorer/"
    assert "Spec Explorer" in client.get("/explorer/", follow_redirects=True).text
    assert client.get("/explorer/endpoints").status_code == 200
