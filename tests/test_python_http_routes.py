"""Flask/FastAPI HTTP method + path extraction for find_endpoints / RF Explorer."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import parse_python_http_route
from livespec_mcp.server import mcp

FASTAPI_APP = (
    "from fastapi import APIRouter, FastAPI\n"
    "\n"
    "app = FastAPI()\n"
    "router = APIRouter()\n"
    "\n"
    "@app.get('/users')\n"
    "def list_users():\n"
    "    return []\n"
    "\n"
    "@router.post('/items/{item_id}')\n"
    "def create_item(item_id: int):\n"
    "    return {'id': item_id}\n"
    "\n"
    "@app.api_route('/legacy', methods=['GET', 'HEAD'])\n"
    "def legacy():\n"
    "    return 'ok'\n"
    "\n"
    "@app.websocket('/ws')\n"
    "async def websocket_endpoint():\n"
    "    pass\n"
)


def test_parse_python_http_route_fastapi_decorators():
    route = parse_python_http_route(FASTAPI_APP, start_line=7)
    assert route == {"http_method": "GET", "http_path": "/users"}

    route = parse_python_http_route(FASTAPI_APP, start_line=11)
    assert route == {"http_method": "POST", "http_path": "/items/{item_id}"}

    route = parse_python_http_route(FASTAPI_APP, start_line=15)
    assert route["http_method"] == "GET"
    assert route["http_path"] == "/legacy"

    route = parse_python_http_route(FASTAPI_APP, start_line=19)
    assert route == {"http_method": "WEBSOCKET", "http_path": "/ws"}


def test_parse_python_http_route_flask_route_with_methods():
    src = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/login', methods=['POST'])\n"
        "def login():\n"
        "    return 'ok'\n"
        "\n"
        "@app.route('/health')\n"
        "def health():\n"
        "    return 'ok'\n"
    )
    assert parse_python_http_route(src, 5) == {
        "http_method": "POST",
        "http_path": "/login",
    }
    assert parse_python_http_route(src, 9) == {
        "http_method": "GET",
        "http_path": "/health",
    }


@pytest.mark.asyncio
async def test_find_endpoints_includes_http_method_and_path(workspace):
    pkg = workspace / "api"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(FASTAPI_APP)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "fastapi"})).data

    by_handler = {e["qualified_name"]: e for e in out["endpoints"]}
    users = by_handler["api.main.list_users"]
    assert users["http_method"] == "GET"
    assert users["http_path"] == "/users"

    item = by_handler["api.main.create_item"]
    assert item["http_method"] == "POST"
    assert item["http_path"] == "/items/{item_id}"


@pytest.mark.asyncio
async def test_export_explorer_fastapi_paths_in_data_json(workspace):
    pkg = workspace / "api"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(FASTAPI_APP)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("export_explorer", {})

    data = json.loads(
        (workspace / ".mcp-docs" / "explorer" / "data.json").read_text(
            encoding="utf-8"
        )
    )
    by_handler = {e["handler"]: e for e in data["endpoints"]}
    users = by_handler["api.main.list_users"]
    assert users["method"] == "GET"
    assert users["path"] == "/users"
    assert users["framework"] == "fastapi"
