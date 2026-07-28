"""Express call-style routes via find_endpoints(framework='express')."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import extract, scan_hono_routes
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
    "function liveness(req, res) { res.send('ok'); }\n"
    "function wrap(fn) { return fn; }\n"
    "\n"
    "router.get('/health', healthController.check);\n"
    "router.get('/live', wrap(liveness));\n"
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

EXPRESS_DEFAULT_EXPORT_PROJECT = {
    "src/routes.ts": (
        "import { Router } from 'express';\n"
        "import { wrap } from './util/wrap';\n"
        "import liveness from './controllers/liveness';\n"
        "import details from './controllers/details';\n"
        "\n"
        "const router = Router();\n"
        "router.get('/liveness', wrap(liveness));\n"
        "router.get('/details/:hotel', wrap(details));\n"
        "export default router;\n"
    ),
    "src/util/wrap.ts": (
        "export function wrap(fn: any) { return fn; }\n"
    ),
    "src/controllers/liveness.ts": (
        "export default async (ctx: any): Promise<void> => {\n"
        "  ctx.status = 200;\n"
        "};\n"
    ),
    "src/controllers/details.ts": (
        "export default async (ctx: any): Promise<void> => {\n"
        "  ctx.body = {};\n"
        "};\n"
    ),
    # Name collision: a different `details` elsewhere must NOT win.
    "src/services/suppliers.ts": (
        "export function details() { return 'wrong'; }\n"
    ),
}


def test_scan_skips_non_router_receivers():
    routes = scan_hono_routes(EXPRESS_WITH_NOISE, "javascript")
    paths = {r["path"] for r in routes}
    assert "/list" in paths
    assert "https://example.com/api" not in paths
    assert "exchange:paquetes" not in paths
    assert "FlowId" not in paths


def test_scan_resolves_member_and_wrap_handlers():
    routes = {
        r["path"]: r
        for r in scan_hono_routes(EXPRESS_ROUTES, "javascript")
    }
    assert routes["/health"]["handler_name"] == "check"
    assert routes["/health"]["handler_import"] == "healthController"
    assert routes["/live"]["handler_name"] == "liveness"
    assert routes["/list"]["handler_name"] == "listHotels"


def test_default_export_anonymous_gets_basename_symbol(tmp_path):
    p = tmp_path / "src" / "controllers" / "liveness.ts"
    p.parent.mkdir(parents=True)
    p.write_text(
        "export default async (ctx: any): Promise<void> => { ctx.status = 200; };\n"
    )
    _, result = extract(p, p.read_text(), tmp_path)
    names = {s.name for s in result.symbols}
    assert "liveness" in names
    assert any(s.qualified_name.endswith(".liveness") for s in result.symbols)


@pytest.mark.asyncio
async def test_find_endpoints_express(workspace):
    (workspace / "routes.js").write_text(EXPRESS_ROUTES)
    (workspace / "controller").mkdir()
    (workspace / "controller" / "healthController.js").write_text(
        "function check(req, res) { res.send('ok'); }\n"
        "module.exports = { check };\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        empty = (await c.call_tool("find_endpoints", {})).data
        assert empty["count"] == 0
        assert "express" in (empty.get("not_swept") or [])

        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        routes = {(e["express_method"], e["express_path"]) for e in out["endpoints"]}
        assert ("GET", "/health") in routes, routes
        assert ("GET", "/live") in routes
        assert ("GET", "/list") in routes
        assert ("POST", "/search") in routes
        by_route = {
            (e["express_method"], e["express_path"]): e for e in out["endpoints"]
        }
        assert by_route[("GET", "/list")]["qualified_name"].endswith("listHotels")
        assert by_route[("GET", "/list")]["http_framework"] == "express"
        assert by_route[("GET", "/health")]["qualified_name"].endswith("check")
        assert by_route[("GET", "/live")]["qualified_name"].endswith("liveness")


@pytest.mark.asyncio
async def test_find_endpoints_prefer_import_over_name_collision(workspace):
    """wrap(details) must link to controllers.details, not services.details."""
    for rel, body in EXPRESS_DEFAULT_EXPORT_PROJECT.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        by_path = {e["express_path"]: e for e in out["endpoints"]}
        assert "/liveness" in by_path, out
        assert "/details/:hotel" in by_path, out
        live_qn = by_path["/liveness"]["qualified_name"]
        details_qn = by_path["/details/:hotel"]["qualified_name"]
        assert "controllers.liveness" in live_qn, live_qn
        assert "controllers.details" in details_qn, details_qn
        assert "suppliers" not in details_qn, details_qn


@pytest.mark.asyncio
async def test_find_endpoints_express_ignores_axios_noise(workspace):
    (workspace / "routes.js").write_text(EXPRESS_WITH_NOISE)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        paths = {e["express_path"] for e in out["endpoints"]}
        assert paths == {"/list"}
