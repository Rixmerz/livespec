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


EXPRESS_TRAILING_ARROWS = (
    "const express = require('express');\n"
    "const searchController = require('../controller/searchController');\n"
    "const healthController = require('../controller/healthController');\n"
    "const router = express.Router();\n"
    "\n"
    "function requireAuth(req, res, next) { next(); }\n"
    "function listHotels(req, res) { res.json([]); }\n"
    "\n"
    # Trailing 4-arity arrow: Express error handler.
    "router.post(\n"
    "  '/search',\n"
    "  requireAuth,\n"
    "  searchController.getFlights,\n"
    "  (error, _req, _res, _next) => { console.error(error); }\n"
    ");\n"
    # Trailing 2-arity arrow: a logging tail after the real handler.
    "router.get('/health', requireAuth, healthController.check, (_req, _res) => {\n"
    "  console.log('checked');\n"
    "});\n"
    "router.get('/named', requireAuth, listHotels);\n"
)


def test_scan_looks_past_trailing_arrow_to_the_named_handler():
    """A trailing inline arrow is a tail, not the endpoint's handler.

    Real Express code closes a route with an error handler
    `(err, req, res, next)` or a small logging arrow, so the rightmost NAMED
    argument is the answer. Stopping at the trailing arrow instead pointed
    `/health` at nothing on a real service.
    """
    routes = {r["path"]: r for r in scan_hono_routes(
        EXPRESS_TRAILING_ARROWS, "javascript"
    )}
    assert routes["/search"]["handler_name"] == "getFlights", routes["/search"]
    assert routes["/search"]["handler_import"] == "searchController"
    assert routes["/health"]["handler_name"] == "check", routes["/health"]
    assert routes["/named"]["handler_name"] == "listHotels"


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
        # Default sweep now includes Express call-style routes.
        out = (await c.call_tool("find_endpoints", {})).data
        assert out["count"] >= 4
        assert "not_swept" not in out
        routes = {
            (e.get("express_method") or e.get("http_method"), e.get("express_path") or e.get("http_path"))
            for e in out["endpoints"]
        }
        assert ("GET", "/health") in routes, routes
        assert ("GET", "/live") in routes
        assert ("GET", "/list") in routes
        assert ("POST", "/search") in routes

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


EXPRESS_INLINE_ARROW = (
    "const express = require('express');\n"
    "const { searchHotels } = require('./service/hotels');\n"
    "const router = express.Router();\n"
    "\n"
    "router.get('/inline/:id', async (req, res) => {\n"
    "  res.json(await searchHotels(req.params.id));\n"
    "});\n"
    "\n"
    "module.exports = router;\n"
)


@pytest.mark.asyncio
async def test_inline_arrow_route_id_is_navigable(workspace):
    """An inline handler must still hand back an id other tools accept.

    The route used to be reported as `routes.js:5`, which every symbol-taking
    tool answers with "Symbol not found" — the endpoint was a dead end. The
    arrow has no symbol of its own, but its calls are attributed to the scope
    that encloses it, so that scope is what the route resolves to.
    """
    (workspace / "routes.js").write_text(EXPRESS_INLINE_ARROW)
    (workspace / "service").mkdir()
    (workspace / "service" / "hotels.js").write_text(
        "async function searchHotels(id) { return { id }; }\n"
        "module.exports = { searchHotels };\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        entry = next(e for e in out["endpoints"] if e["express_path"] == "/inline/:id")
        qname = entry["qualified_name"]

        assert entry["handler_resolution"] == "enclosing_scope", entry
        assert ":" not in qname, qname
        assert entry["start_line"] == 5, entry  # still points at the registration

        # The id round-trips through the tools an agent chains next.
        src = (await c.call_tool("get_symbol_source", {"qname": qname})).data
        assert "isError" not in src, src
        calls = (await c.call_tool("who_does_this_call", {"qname": qname})).data
        assert "isError" not in calls, calls
        callees = {c_["qualified_name"] for c_ in calls["callees"]}
        assert any(name.endswith("searchHotels") for name in callees), calls


@pytest.mark.asyncio
async def test_resolved_handler_is_labelled_as_such(workspace):
    """`handler_resolution` distinguishes a real handler from the fallback."""
    (workspace / "routes.js").write_text(EXPRESS_ROUTES)
    (workspace / "controller").mkdir()
    (workspace / "controller" / "healthController.js").write_text(
        "function check(req, res) { res.send('ok'); }\n"
        "module.exports = { check };\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        by_path = {e["express_path"]: e for e in out["endpoints"]}
        assert by_path["/health"]["handler_resolution"] == "handler"
        assert by_path["/list"]["handler_resolution"] == "handler"


@pytest.mark.asyncio
async def test_find_endpoints_express_ignores_axios_noise(workspace):
    (workspace / "routes.js").write_text(EXPRESS_WITH_NOISE)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "express"})).data
        paths = {e["express_path"] for e in out["endpoints"]}
        assert paths == {"/list"}
