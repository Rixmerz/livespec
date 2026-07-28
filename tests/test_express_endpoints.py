"""Express call-style routes via find_endpoints(framework='express')."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import scan_hono_routes
from livespec_mcp.server import mcp

EXPRESS_ROUTES = (
    "const express = require('express');\n"
    "const healthController = require('../controller/healthController');\n"
    "\n"
    "const router = express.Router();\n"
    "\n"
    "function listHotels(req, res) {\n"
    "  res.json([]);\n"
    "}\n"
    "\n"
    "router.get('/health', healthController.check);\n"
    "router.get('/list', listHotels);\n"
    "router.post('/search', listHotels);\n"
    "\n"
    "module.exports = router;\n"
)

EXPRESS_WITH_NOISE = (
    "const express = require('express');\n"
    "const axios = require('axios');\n"
    "const router = express.Router();\n"
    "\n"
    "function listHotels(req, res) { res.json([]); }\n"
    "\n"
    "router.get('/list', listHotels);\n"
    "axios.get('https://example.com/api');\n"
    "cache.get('exchange:paquetes');\n"
    "headers.get('FlowId');\n"
)


def test_scan_skips_non_router_receivers():
    routes = scan_hono_routes(EXPRESS_WITH_NOISE, "javascript")
    paths = {r["path"] for r in routes}
    assert "/list" in paths
    assert "https://example.com/api" not in paths
    assert "exchange:paquetes" not in paths
    assert "FlowId" not in paths


@pytest.mark.asyncio
async def test_find_endpoints_express(workspace):
    (workspace / "routes.js").write_text(EXPRESS_ROUTES)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        empty = (await c.call_tool("find_endpoints", {})).data
        assert empty["count"] == 0
        assert "express" in (empty.get("not_swept") or [])

        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        routes = {(e["express_method"], e["express_path"]) for e in out["endpoints"]}
        assert ("GET", "/health") in routes, routes
        assert ("GET", "/list") in routes
        assert ("POST", "/search") in routes
        by_route = {
            (e["express_method"], e["express_path"]): e for e in out["endpoints"]
        }
        assert by_route[("GET", "/list")]["qualified_name"].endswith("listHotels")
        assert by_route[("GET", "/list")]["http_framework"] == "express"


@pytest.mark.asyncio
async def test_find_endpoints_express_ignores_axios_noise(workspace):
    (workspace / "routes.js").write_text(EXPRESS_WITH_NOISE)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        paths = {e["express_path"] for e in out["endpoints"]}
        assert paths == {"/list"}
