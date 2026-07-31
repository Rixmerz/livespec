"""v0.13 P3: Hono framework support.

Two mechanisms: (1) named handlers passed to registration-style calls
(`app.get('/users', listUsers)`) get refs — extract-time `callback_arg`
refs inside symbol bodies plus a find_dead_code-time TS/JS scan for
module-level registrations; (2) `find_endpoints(framework='hono')`
extracts call-style routes with method + path.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

HONO_APP = (
    "import { Hono } from 'hono';\n"
    "\n"
    "const app = new Hono();\n"
    "\n"
    "function listUsers(c: any) {\n"
    "  return c.json([]);\n"
    "}\n"
    "\n"
    "function createUser(c: any) {\n"
    "  return c.json({ ok: true });\n"
    "}\n"
    "\n"
    "function neverRegistered(c: any) {\n"
    "  return c.text('dead');\n"
    "}\n"
    "\n"
    "app.get('/users', listUsers);\n"
    "app.post('/users', createUser);\n"
    "app.get('/health', (c) => c.text('ok'));\n"
    "app.on('PURGE', '/cache', (c) => c.text('purged'));\n"
    "\n"
    "export default app;\n"
)


@pytest.mark.asyncio
async def test_find_endpoints_hono(workspace):
    (workspace / "server.ts").write_text(HONO_APP)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "hono"})).data
        routes = {(e["hono_method"], e["hono_path"]) for e in out["endpoints"]}
        assert ("GET", "/users") in routes, routes
        assert ("POST", "/users") in routes
        assert ("GET", "/health") in routes
        assert ("PURGE", "/cache") in routes
        # Named handler resolves to its symbol
        by_route = {
            (e["hono_method"], e["hono_path"]): e for e in out["endpoints"]
        }
        assert by_route[("GET", "/users")]["qualified_name"].endswith("listUsers")
        assert by_route[("GET", "/users")]["handler_resolution"] == "handler"
        # An inline arrow has no symbol: the route resolves to the scope that
        # owns its calls (the module here), not to a `server.ts:9` pseudo-id
        # that no other tool accepts.
        inline = by_route[("GET", "/health")]
        assert inline["handler_resolution"] == "enclosing_scope"
        assert inline["qualified_name"] == "server", inline


@pytest.mark.asyncio
async def test_dead_code_hono_handlers_protected(workspace):
    (workspace / "server.ts").write_text(HONO_APP)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        # Registered at module level — protected by the TS runtime scan
        assert not any(q.endswith("listUsers") for q in qnames), qnames
        assert not any(q.endswith("createUser") for q in qnames), qnames
        # Never registered anywhere — stays flagged
        assert any(q.endswith("neverRegistered") for q in qnames), qnames


@pytest.mark.asyncio
async def test_callback_refs_inside_function_body(workspace):
    """Registration INSIDE a function body produces extract-time refs, so
    who_calls answers and the handler is protected without the TS scan."""
    (workspace / "setup.ts").write_text(
        "import { Hono } from 'hono';\n"
        "\n"
        "function ping(c: any) {\n"
        "  return c.text('pong');\n"
        "}\n"
        "\n"
        "export function buildApp() {\n"
        "  const app = new Hono();\n"
        "  app.get('/ping', ping);\n"
        "  return app;\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("who_calls", {"qname": "setup.ping"})).data
        callers = {x["qualified_name"] for x in out.get("callers", [])}
        assert any("buildApp" in q for q in callers), out


@pytest.mark.asyncio
async def test_hono_scan_skips_non_hono_files(workspace):
    """`map.get(key)` patterns in files without 'hono' must not emit routes."""
    (workspace / "cache.ts").write_text(
        "const store = new Map<string, string>();\n"
        "export function lookup(key: string) {\n"
        "  return store.get(key);\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "hono"})).data
        assert out["count"] == 0, out
