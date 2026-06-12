"""v0.13 P2: Spring Boot + Angular framework support.

Spring: Java annotations (extracted since v0.13 P1) drive
`find_endpoints(framework='spring')` and protect controllers/services
from dead-code flagging. Angular: TS decorators drive
`find_endpoints(framework='angular')`; template-bound classes
(Component/Directive/Pipe) protect ALL their methods (HTML templates are
invisible to the indexer); lifecycle hooks are protected on any
Angular-decorated class.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

SPRING_SRC = (
    "package com.example.api;\n"
    "\n"
    "import org.springframework.web.bind.annotation.*;\n"
    "\n"
    "@RestController\n"
    "@RequestMapping(\"/api/users\")\n"
    "public class UserController {\n"
    "\n"
    "    @GetMapping\n"
    "    public String list() {\n"
    "        return helper();\n"
    "    }\n"
    "\n"
    "    @PostMapping(\"/create\")\n"
    "    public String create() {\n"
    "        return \"ok\";\n"
    "    }\n"
    "\n"
    "    private String helper() {\n"
    "        return \"[]\";\n"
    "    }\n"
    "}\n"
)

ANGULAR_SRC = (
    "import { Component } from '@angular/core';\n"
    "\n"
    "@Component({ selector: 'app-dash', templateUrl: './dash.html' })\n"
    "export class DashComponent {\n"
    "  ngOnInit(): void {}\n"
    "\n"
    "  saveFromTemplate(): void {}\n"
    "}\n"
)


@pytest.mark.asyncio
async def test_find_endpoints_spring(workspace):
    (workspace / "UserController.java").write_text(SPRING_SRC)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "spring"})).data
        by_qname = {e["qualified_name"]: e for e in out["endpoints"]}
        controller = next(
            (e for q, e in by_qname.items() if q.endswith("UserController")), None
        )
        assert controller is not None, f"controller missing: {by_qname.keys()}"
        assert "RestController" in controller["decorators"]
        assert any(q.endswith("list") for q in by_qname)
        assert any(q.endswith("create") for q in by_qname)
        # un-annotated helper is NOT an endpoint
        assert not any(q.endswith("helper") for q in by_qname)


@pytest.mark.asyncio
async def test_find_endpoints_angular(workspace):
    (workspace / "dash.component.ts").write_text(ANGULAR_SRC)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"framework": "angular"})).data
        qnames = {e["qualified_name"] for e in out["endpoints"]}
        assert any(q.endswith("DashComponent") for q in qnames), qnames


@pytest.mark.asyncio
async def test_dead_code_spring_protection(workspace):
    (workspace / "UserController.java").write_text(SPRING_SRC)
    (workspace / "Orphan.java").write_text(
        "public class Orphan {\n"
        "    public static String unusedHelper() {\n"
        "        return \"dead\";\n"
        "    }\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        # Annotated controller + mapped methods protected
        assert not any("UserController" in q and q.endswith("list") for q in qnames)
        assert not any(q.endswith("create") for q in qnames), qnames
        assert not any(q.endswith("UserController") for q in qnames), qnames
        # helper() is CALLED by list() — protected by the call edge
        # Orphan.unusedHelper has no annotation, no callers — stays flagged.
        # (Java `public` visibility is not in _PUBLIC_VIS skip-set only for
        # Rust pub; public Java symbols need include_public to surface.)
        out_pub = (
            await c.call_tool(
                "find_dead_code",
                {"include_non_python": True, "include_public": True},
            )
        ).data
        pub_qnames = {d["qualified_name"] for d in out_pub["dead_symbols"]}
        assert any(q.endswith("unusedHelper") for q in pub_qnames), pub_qnames
        # Even with include_public, the Spring-annotated symbols stay out
        assert not any(q.endswith("create") for q in pub_qnames), pub_qnames


@pytest.mark.asyncio
async def test_dead_code_angular_protection(workspace):
    (workspace / "dash.component.ts").write_text(ANGULAR_SRC)
    (workspace / "util.ts").write_text(
        "export function usedNowhere(): number {\n  return 1;\n}\n"
        "function localDead(): number {\n  return 2;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        # Component class + its methods (template-bound) + lifecycle: protected
        assert not any("DashComponent" in q for q in qnames), qnames
        # Non-exported dead TS fn still flagged (exported one needs include_public)
        assert any(q.endswith("localDead") for q in qnames), qnames
