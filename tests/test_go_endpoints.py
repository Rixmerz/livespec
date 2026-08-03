"""Go call-style HTTP routes via find_endpoints (gin / net/http)."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import scan_go_routes
from livespec_mcp.server import mcp

GIN_AND_NETHTTP = (
    "package main\n"
    "\n"
    "import (\n"
    '  "net/http"\n'
    '  "github.com/gin-gonic/gin"\n'
    ")\n"
    "\n"
    "func Hello(w http.ResponseWriter, r *http.Request) {}\n"
    "func Hi(c *gin.Context) {}\n"
    "\n"
    "func main() {\n"
    "  r := gin.Default()\n"
    '  r.GET("/hi", Hi)\n'
    '  r.POST("/hi", Hi)\n'
    '  http.HandleFunc("/x", Hello)\n'
    "  mux := http.NewServeMux()\n"
    '  mux.HandleFunc("/y", Hello)\n'
    "}\n"
)


def test_scan_go_routes_gin_and_nethttp():
    routes = scan_go_routes(GIN_AND_NETHTTP)
    by_path = {(r["method"], r["path"]): r for r in routes}
    assert ("GET", "/hi") in by_path
    assert ("POST", "/hi") in by_path
    assert ("*", "/x") in by_path
    assert ("*", "/y") in by_path
    assert by_path[("GET", "/hi")]["handler_name"] == "Hi"
    assert by_path[("*", "/x")]["handler_name"] == "Hello"
    assert by_path[("GET", "/hi")]["framework"] == "gin"
    assert by_path[("*", "/x")]["framework"] == "nethttp"


@pytest.mark.asyncio
async def test_find_endpoints_go_default_and_gin(workspace):
    (workspace / "main.go").write_text(GIN_AND_NETHTTP)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        default = (await c.call_tool("find_endpoints", {})).data
        gin = (await c.call_tool("find_endpoints", {"framework": "gin"})).data
        net = (await c.call_tool("find_endpoints", {"framework": "nethttp"})).data
    paths = {(e.get("http_method"), e.get("http_path")) for e in default["endpoints"]}
    assert ("GET", "/hi") in paths
    assert ("*", "/x") in paths
    gin_paths = {(e.get("http_method"), e.get("http_path")) for e in gin["endpoints"]}
    assert ("GET", "/hi") in gin_paths
    assert ("*", "/x") not in gin_paths
    net_paths = {(e.get("http_method"), e.get("http_path")) for e in net["endpoints"]}
    assert ("*", "/x") in net_paths
    assert ("GET", "/hi") not in net_paths
    # Handler resolved to indexed symbol
    hi = next(e for e in default["endpoints"] if e.get("http_path") == "/hi" and e.get("http_method") == "GET")
    assert hi["qualified_name"].endswith("Hi")
    assert hi.get("handler_resolution") == "handler"
