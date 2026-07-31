"""P2 cross-repo route edges: a frontend call site (fetch/axios/requests) is
joined to a backend handler by normalized HTTP path → `invokes_route` edge,
surfaced by who_calls (route_callers) / who_does_this_call (invokes_endpoints).
DB-wide matching makes it cross-repo under a shared group_db, intra-repo in a
monorepo."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import (
    _path_from_template_raw,
    normalize_route_path,
)
from livespec_mcp.server import mcp


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/api/users/<int:id>", "/api/users/{}"),   # Flask
        ("/api/users/{id}", "/api/users/{}"),        # FastAPI / Hono
        ("/api/users/:id", "/api/users/{}"),         # Express / React-router
        ("/api/users/123", "/api/users/{}"),         # concrete client call
        ("https://api.example.com/v1/things", "/v1/things"),
        ("/a//b/", "/a/b"),                           # dedup + trailing slash
        ("/search?q=1#frag", "/search"),             # query + fragment stripped
        ("users", "/users"),                          # leading slash added
    ],
)
def test_normalize_route_path(raw, expected):
    assert normalize_route_path(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("${base}/flights/v2", "/flights/v2"),
        ("${base}/flights/v2?x=1", "/flights/v2?x=1"),  # query kept; normalize strips
        (
            "${url}/list/${a}/${b}/${c}",
            "/list/{}/{}/{}",
        ),
        # Real repo: ternaries contain `?` — must NOT truncate as query string
        (
            "${url}/list/${params.checkIn ? `${params.checkIn}/` : \"\"}"
            "${params.checkOut ? `${params.checkOut}/` : \"\"}"
            "${params.hotelIds ? `${params.hotelIds}` : \"\"}",
            "/list/{}/{}/{}",
        ),
        ("/list/a/b?x=1", "/list/a/b"),
        ("https://hotelsvc.example/list/a/b/c", "https://hotelsvc.example/list/a/b/c"),
        ("", None),
        ("not-a-url", None),
    ],
)
def test_path_from_template_raw(raw, expected):
    assert _path_from_template_raw(raw) == expected


@pytest.mark.asyncio
async def test_thin_http_wrapper_makeRequest_emits_client_route(sample_repo):
    """Third-party client pattern: ``makeRequest(`${base}/search`, body)`` where
    ``makeRequest`` forwards its first param to ``fetch`` — the CALLER must
    get the client route_ref (not the wrapper itself)."""
    (sample_repo / "back.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.post('/search')\n"
        "def search():\n"
        "    return {}\n"
        "@app.post('/coverage')\n"
        "def coverage():\n"
        "    return {}\n"
    )
    (sample_repo / "facade.js").write_text(
        "const baseUrl = 'https://third-party-spa.example';\n"
        "async function callSupplierApi(body, params) {\n"
        "  return await makeRequest(`${baseUrl}/search`, body, params);\n"
        "}\n"
        "async function callCoverage(body, params) {\n"
        "  return await makeRequest(`${baseUrl}/coverage`, body, params);\n"
        "}\n"
        "async function makeRequest(url, bodyRequest, paramsRequest) {\n"
        "  return await fetch(url, { method: 'POST', body: JSON.stringify(bodyRequest) });\n"
        "}\n"
        "async function notAWrapper(url) {\n"
        "  return url.toUpperCase();\n"
        "}\n"
        "async function decoy(body) {\n"
        "  return await notAWrapper(`${baseUrl}/search`);\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        search_callers = (
            await c.call_tool("who_calls", {"qname": "back.search"})
        ).data.get("route_callers", [])
        coverage_callers = (
            await c.call_tool("who_calls", {"qname": "back.coverage"})
        ).data.get("route_callers", [])
    search_files = {x["qualified_name"] for x in search_callers}
    assert any("callSupplierApi" in q for q in search_files), search_callers
    assert not any("decoy" in q for q in search_files), search_callers
    assert not any("makeRequest" == q.split(".")[-1] for q in search_files), search_callers
    assert any("callCoverage" in x["qualified_name"] for x in coverage_callers), coverage_callers


@pytest.mark.asyncio
async def test_hotelsvc_multi_segment_template_links_java_handler(sample_repo):
    """composer-service→HotelSvc: nested-ternary `/list/${a}/${b}/${c}` joins
    Spring ``@GetMapping("/list/{arrival}/{departure}/{hotels}")``."""
    (sample_repo / "ListController.java").write_text(
        "package com.example;\n"
        "import org.springframework.web.bind.annotation.GetMapping;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        "@RestController\n"
        "public class ListController {\n"
        "  @GetMapping(\"/list/{arrival}/{departure}/{hotels}\")\n"
        "  public String list() { return \"ok\"; }\n"
        "}\n"
    )
    (sample_repo / "suppliers.ts").write_text(
        "export async function listByChunkSupplier(url, params) {\n"
        "  const requestUrl = `${url}/list/${\n"
        "    params.checkIn ? `${params.checkIn}/` : \"\"\n"
        "  }${params.checkOut ? `${params.checkOut}/` : \"\"}${\n"
        "    params.hotelIds ? `${params.hotelIds}` : \"\"\n"
        "  }`;\n"
        # Real-world shape: await + TS generics — tree-sitter puts await in
        # the call's function field; must still emit a client route_ref.
        "  return await axios.get<Hotel[]>(requestUrl);\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callees = (
            await c.call_tool(
                "who_does_this_call",
                {"qname": "suppliers.listByChunkSupplier"},
            )
        ).data
    ep = callees.get("invokes_endpoints", [])
    assert any("ListController" in x["qualified_name"] for x in ep), ep


BACKEND = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n"
    "@app.get('/api/users/{id}')\n"
    "def get_user(id):\n"
    "    return {'id': id}\n"
)
FRONT_PY = (
    "import requests\n"
    "def load_user():\n"
    "    return requests.get('/api/users/123')\n"
)
FRONT_TS = (
    "export async function loadUser() {\n"
    "  return fetch('/api/users/42');\n"
    "}\n"
)


@pytest.mark.asyncio
async def test_monorepo_route_edge(sample_repo):
    """Front + back in one repo (one project) — the DB-wide resolver links
    them without any group config."""
    (sample_repo / "back.py").write_text(BACKEND)
    (sample_repo / "front.py").write_text(FRONT_PY)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})

        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc = callers.get("route_callers", [])
        assert any(x["qualified_name"] == "front.load_user" for x in rc)

        callees = (
            await c.call_tool("who_does_this_call", {"qname": "front.load_user"})
        ).data
        ep = callees.get("invokes_endpoints", [])
        assert any(x["qualified_name"] == "back.get_user" for x in ep)


@pytest.mark.asyncio
async def test_cross_repo_route_edge_via_group_db(tmp_path):
    """Frontend repo (TS fetch) and backend repo (FastAPI) in a shared group
    DB — who_calls on the backend handler surfaces the frontend caller."""
    shared = tmp_path / "grp" / "shared.db"
    back = tmp_path / "back"
    front = tmp_path / "front"
    for root in (back, front):
        root.mkdir(parents=True)
        (root / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{shared}"\n')
    (back / "api.py").write_text(BACKEND)
    (front / "ui.ts").write_text(FRONT_TS)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(front)})
        await c.call_tool("index_project", {"workspace": str(back)})

        callers = (
            await c.call_tool(
                "who_calls", {"workspace": str(back), "qname": "api.get_user"}
            )
        ).data
        rc = callers.get("route_callers", [])
        assert rc, "expected a cross-repo route caller from the frontend repo"
        assert any("ui" in x["file"] for x in rc)
        # fetch() has no explicit method → method-agnostic match, weight 0.8.
        assert all(0.0 < x["confidence"] <= 1.0 for x in rc)


@pytest.mark.asyncio
async def test_cross_repo_who_calls_from_frontend_workspace(tmp_path):
    """group_db: resolve a backend qname from the frontend workspace and still
    surface route_callers (P0 audit fix — symbol lookup is group-wide)."""
    shared = tmp_path / "grp2" / "shared.db"
    back = tmp_path / "back2"
    front = tmp_path / "front2"
    for root in (back, front):
        root.mkdir(parents=True)
        (root / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{shared}"\n')
    (back / "api.py").write_text(BACKEND)
    (front / "ui.ts").write_text(FRONT_TS)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(front)})
        await c.call_tool("index_project", {"workspace": str(back)})

        found = (
            await c.call_tool(
                "find_symbol",
                {"workspace": str(front), "query": "get_user", "limit": 10},
            )
        ).data
        assert any(
            m.get("qualified_name") == "api.get_user" for m in found.get("matches") or []
        ), found

        callers = (
            await c.call_tool(
                "who_calls",
                {"workspace": str(front), "qname": "api.get_user"},
            )
        ).data
        assert not callers.get("isError"), callers
        rc = callers.get("route_callers") or []
        assert any("ui" in (x.get("file") or "") for x in rc), callers


@pytest.mark.asyncio
async def test_method_mismatch_no_edge(sample_repo):
    """A POST client call must not link to a GET-only handler."""
    (sample_repo / "back.py").write_text(BACKEND)  # GET /api/users/{id}
    (sample_repo / "front.py").write_text(
        "import requests\n"
        "def create_user():\n"
        "    return requests.post('/api/users/9')\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        assert not callers.get("route_callers")


@pytest.mark.asyncio
async def test_reindex_is_idempotent_no_duplicate_route_callers(sample_repo):
    """Indexing the same monorepo twice must not duplicate the
    invokes_route edge — exactly one route_callers entry survives."""
    (sample_repo / "back.py").write_text(BACKEND)
    (sample_repo / "front.py").write_text(FRONT_PY)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        await c.call_tool("index_project", {"workspace": str(sample_repo)})

        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc = [
            x for x in callers.get("route_callers", [])
            if x["qualified_name"] == "front.load_user"
        ]
        assert len(rc) == 1


@pytest.mark.asyncio
async def test_route_edge_weight_survives_unrelated_reindex(sample_repo):
    """A second index run (triggered by an unrelated new file) must not
    downgrade or drop the existing route edge's confidence."""
    (sample_repo / "back.py").write_text(BACKEND)
    (sample_repo / "front.py").write_text(FRONT_PY)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callers1 = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc1 = [
            x for x in callers1.get("route_callers", [])
            if x["qualified_name"] == "front.load_user"
        ]
        assert rc1
        first_conf = rc1[0]["confidence"]

        (sample_repo / "unrelated.py").write_text("def noop():\n    pass\n")
        await c.call_tool("index_project", {"workspace": str(sample_repo)})

        callers2 = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc2 = [
            x for x in callers2.get("route_callers", [])
            if x["qualified_name"] == "front.load_user"
        ]
        assert len(rc2) == 1
        assert rc2[0]["confidence"] >= first_conf


@pytest.mark.asyncio
async def test_axios_client_links_to_backend_handler(sample_repo):
    """axios.get('/x') inside an exported TS function is detected as a
    client route site and joins the FastAPI GET handler."""
    (sample_repo / "back.py").write_text(BACKEND)
    (sample_repo / "front.ts").write_text(
        "export async function loadUser() {\n"
        "  return axios.get('/api/users/7');\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc = callers.get("route_callers", [])
        assert any("front" in x["file"] for x in rc)


@pytest.mark.asyncio
async def test_express_route_links_axios_template_client(sample_repo):
    """Express server registrations and `${base}/path` axios calls join."""
    (sample_repo / "results.ts").write_text(
        "export function registerResultsRoutes() {\n"
        "  router.post('/flights/v2', submitResults);\n"
        "}\n"
        "function submitResults() {}\n"
    )
    (sample_repo / "composer.ts").write_text(
        "export async function submitSearch(base) {\n"
        "  return axios.post(`${base}/flights/v2`);\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callers = (
            await c.call_tool(
                "who_calls", {"qname": "results.registerResultsRoutes"}
            )
        ).data
        callees = (
            await c.call_tool(
                "who_does_this_call", {"qname": "composer.submitSearch"}
            )
        ).data
    assert any(
        x["qualified_name"] == "composer.submitSearch"
        for x in callers.get("route_callers", [])
    )
    assert any(
        x["qualified_name"] == "results.registerResultsRoutes"
        for x in callees.get("invokes_endpoints", [])
    )


@pytest.mark.asyncio
async def test_express_route_links_axios_identifier_template_client(sample_repo):
    """A same-function URL binding resolves before axios route matching."""
    (sample_repo / "results.ts").write_text(
        "export function registerResultsRoutes() {\n"
        "  router.post('/flights/v2', submitResults);\n"
        "}\n"
        "function submitResults() {}\n"
    )
    (sample_repo / "composer.ts").write_text(
        "export async function submitSearch(base) {\n"
        "  const requestUrl = `${base}/flights/v2`;\n"
        "  return axios.post(requestUrl);\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callees = (
            await c.call_tool(
                "who_does_this_call", {"qname": "composer.submitSearch"}
            )
        ).data
    assert any(
        x["qualified_name"] == "results.registerResultsRoutes"
        for x in callees.get("invokes_endpoints", [])
    )


@pytest.mark.asyncio
async def test_axios_post_url_var_and_body_ident_is_client(sample_repo):
    """Composer pattern: ``const url = `${base}/flights/v2`; axios.post(url, body)``.

    Second-arg identifiers are request payloads, not Express handlers — must
    still emit a client route_ref and join to the backend.
    """
    (sample_repo / "back.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.post('/flights/v2')\n"
        "def search():\n"
        "    return {}\n"
    )
    (sample_repo / "front.js").write_text(
        "async function getFlightsV2(body) {\n"
        "  const baseUrl = 'https://composer.example';\n"
        "  const url = `${baseUrl}/flights/v2`;\n"
        "  return axios.post(url, body);\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callees = (
            await c.call_tool(
                "who_does_this_call", {"qname": "front.getFlightsV2"}
            )
        ).data
        ep = callees.get("invokes_endpoints", [])
        assert any(x["qualified_name"] == "back.search" for x in ep)


@pytest.mark.asyncio
async def test_bare_got_client_links_to_backend_handler(sample_repo):
    """got('/x') uses the same method-agnostic route matching as fetch."""
    (sample_repo / "back.py").write_text(BACKEND)
    (sample_repo / "front.ts").write_text(
        "export async function loadUser() {\n"
        "  return got('/api/users/7');\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc = callers.get("route_callers", [])
        assert any("front" in x["file"] for x in rc)


@pytest.mark.asyncio
async def test_hono_server_registration_is_not_a_client(sample_repo):
    """A Hono/Express-style ``app.get('/x', handler)`` server registration
    (object `app`, not in the HTTP-client allowlist) must not create a
    spurious route_callers entry on the FastAPI handler."""
    (sample_repo / "back.py").write_text(BACKEND)
    (sample_repo / "hono.ts").write_text(
        "function getUser(c) {\n"
        "  return c.json({ id: 1 });\n"
        "}\n"
        "app.get('/api/users/:id', getUser);\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc = callers.get("route_callers", [])
        assert not any("hono" in x["file"] for x in rc)


@pytest.mark.asyncio
async def test_dynamic_fetch_url_produces_no_route_edge(sample_repo):
    """fetch(url) with a variable (not a string literal) must not crash
    indexing and must not produce a route edge."""
    (sample_repo / "back.py").write_text(BACKEND)
    (sample_repo / "front.ts").write_text(
        "export async function loadDynamic(url) {\n"
        "  return fetch(url);\n"
        "}\n"
    )
    async with Client(mcp) as c:
        result = await c.call_tool("index_project", {"workspace": str(sample_repo)})
        assert not result.data.get("isError")
        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc = callers.get("route_callers", [])
        assert not any("front" in x["file"] for x in rc)


@pytest.mark.asyncio
async def test_trailing_slash_and_param_equivalence(sample_repo):
    """A client call with a trailing slash and a concrete id still matches
    a server route with a `{id}` param after normalization."""
    (sample_repo / "back.py").write_text(BACKEND)  # GET /api/users/{id}
    (sample_repo / "front.py").write_text(
        "import requests\n"
        "def load_user():\n"
        "    return requests.get('/api/users/999/')\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        callers = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data
        rc = callers.get("route_callers", [])
        assert any(x["qualified_name"] == "front.load_user" for x in rc)


@pytest.mark.asyncio
async def test_allowlisted_router_name_with_handler_is_not_a_client(sample_repo):
    """Regression (review finding): an Express/Hono router commonly named
    `api`/`client`/`request` is in the client-object allowlist, but a
    `.get('/x', handler)` with a trailing handler arg is a SERVER route
    registration and must NOT create a client edge — even for `api`. A bare
    `api.get('/x')` (no handler) still counts as a client call."""
    (sample_repo / "back.py").write_text(BACKEND)  # GET /api/users/{id}
    # `api` IS in the allowlist, but the handler arg marks this as a server reg.
    (sample_repo / "server.ts").write_text(
        "function registerRoutes() {\n"
        "  api.get('/api/users/5', getUser);\n"
        "}\n"
    )
    # A genuine client call on an allowlisted object, no handler arg.
    (sample_repo / "client.ts").write_text(
        "export function load() {\n"
        "  return api.get('/api/users/5');\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(sample_repo)})
        rc = (
            await c.call_tool("who_calls", {"qname": "back.get_user"})
        ).data.get("route_callers", [])
        files = {x["file"] for x in rc}
        assert not any("server.ts" in f for f in files), (
            "server route registration must not be a client caller"
        )
        assert any("client.ts" in f for f in files), (
            "a bare api.get('/x') client call should still link"
        )
