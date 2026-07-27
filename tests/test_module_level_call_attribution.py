"""Regression tests for bug #7/#8: calls issued directly at TS/JS module top
level (never inside any named function) were silently dropped instead of
attributed to a caller — `who_calls` returned zero callers for a real inbound
edge, and the same defect made `find_dead_code` report live, in-use symbols
as dead (their only caller was an inline anonymous route handler).

Covers every anonymous-scope shape called out in the bug report:
Hono-style inline route handler, `.map()`/`.forEach()` callback, IIFE,
object-literal method shorthand, arrow property, and a default-exported
anonymous function.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import _ts_extract
from livespec_mcp.server import mcp

# ---------- unit tests: extractor-level, one fixture per anonymous shape ----------


def _refs_to(result, target: str) -> list:
    return [r for r in result.refs if r.target_name == target]


def test_inline_route_handler_call_attributed_to_module():
    src = (
        "import { Hono } from 'hono';\n"
        "import { ingestInboundEmail } from './inbound-email.service.ts';\n"
        "\n"
        "const app = new Hono();\n"
        "\n"
        "app.post('/', async (c) => {\n"
        "  const result = await ingestInboundEmail(c);\n"
        "  return c.json(result);\n"
        "});\n"
    )
    result = _ts_extract(src, "typescript", "src.routes.webhooks.inbound-email")
    refs = _refs_to(result, "ingestInboundEmail")
    assert refs, "call inside an inline top-level arrow handler must not be dropped"
    module_sym = next(s for s in result.symbols if s.kind == "module")
    assert refs[0].src_qname == module_sym.qualified_name


def test_array_callback_call_attributed():
    src = "const xs = [1, 2, 3].map((x) => { return transform(x); });\n"
    result = _ts_extract(src, "typescript", "mod")
    assert _refs_to(result, "transform")


def test_iife_call_attributed():
    src = (
        "(function () {\n"
        "  doSetup();\n"
        "})();\n"
        "(() => {\n"
        "  doOtherSetup();\n"
        "})();\n"
    )
    result = _ts_extract(src, "typescript", "mod")
    assert _refs_to(result, "doSetup")
    assert _refs_to(result, "doOtherSetup")


def test_object_method_shorthand_gets_named_symbol():
    src = (
        "const handlers = {\n"
        "  async handler(c) {\n"
        "    return callSvc(c);\n"
        "  },\n"
        "};\n"
    )
    result = _ts_extract(src, "typescript", "mod")
    sym = next((s for s in result.symbols if s.name == "handler"), None)
    assert sym is not None
    refs = _refs_to(result, "callSvc")
    assert refs and refs[0].src_qname == sym.qualified_name


def test_object_arrow_property_call_attributed_but_mints_no_symbol():
    """An object-literal arrow property must NOT get its own symbol, but its
    calls must still be collected (by the module pass).

    Minting a symbol per property key looked harmless on this very fixture and
    was catastrophic on real code: a test mock DB is an object literal of
    Prisma-verb properties, so it minted 543 symbols named `create` /
    `findFirst` / `findUnique`, and every unqualified `db.x.findFirst()` then
    fanned out to all of them at weight 0.5 — 39,752 junk edges on one repo,
    1.1% precision on the sampled symbol, and `get_project_overview`'s top hub
    inverted from `getDb` to a mock property. The call must survive; the
    symbol must not exist.
    """
    src = (
        "const handlers = {\n"
        "  handler: async (c) => {\n"
        "    return callSvc(c);\n"
        "  },\n"
        "};\n"
    )
    result = _ts_extract(src, "typescript", "mod")
    assert not [s for s in result.symbols if s.name == "handler"], (
        "object-literal property keys must not become symbols"
    )
    assert _refs_to(result, "callSvc"), "the call must still be attributed, not dropped"


def test_function_nested_in_object_property_is_not_swallowed():
    """Regression: the deleted `pair` branch returned early, so `walk()` never
    descended into the property body and a named function declared inside it
    vanished along with all of its calls — a silent drop of the exact class
    this module pass exists to fix."""
    src = (
        "const o = {\n"
        "  h: () => {\n"
        "    function inner() {\n"
        "      deep();\n"
        "    }\n"
        "  },\n"
        "};\n"
    )
    result = _ts_extract(src, "typescript", "mod")
    assert any(s.name == "inner" for s in result.symbols), "nested named function lost"
    assert _refs_to(result, "deep"), "call inside the nested function was dropped"


def test_no_symbol_name_collisions_from_repeated_object_keys():
    """Fan-out guard. Repeated property keys across object literals must not
    produce many symbols sharing one qualified_name — `resolve_symbol` is
    LIMIT 1, so colliding qnames are unaddressable, and `_resolve_refs` fans
    every unqualified call out to all of them. The suite passed green while
    this produced 114 colliding groups on a real repo, because no test
    exercised repetition."""
    src = (
        "const a = { create: () => { x(); }, findFirst: () => { x(); } };\n"
        "const b = { create: () => { x(); }, findFirst: () => { x(); } };\n"
        "const c = { create: () => { x(); }, findFirst: () => { x(); } };\n"
    )
    result = _ts_extract(src, "typescript", "mod")
    qnames = [s.qualified_name for s in result.symbols]
    assert len(qnames) == len(set(qnames)), f"colliding qualified_names: {qnames}"
    assert not [s for s in result.symbols if s.name in ("create", "findFirst")]


def test_default_exported_anonymous_function_call_attributed():
    src = "export default async function (c) {\n  return callSvc(c);\n}\n"
    result = _ts_extract(src, "typescript", "mod")
    assert _refs_to(result, "callSvc")


def test_named_function_calls_not_double_counted_via_module_pass():
    """A call inside a NAMED top-level function must be attributed ONLY to
    that function, not also to the module pseudo-symbol (regression guard
    for the new whole-tree module-level collection pass)."""
    src = "function foo() {\n  bar();\n}\n"
    result = _ts_extract(src, "typescript", "mod")
    refs = _refs_to(result, "bar")
    assert len(refs) == 1
    assert refs[0].src_qname == "mod.foo"


def test_nested_named_const_arrow_not_double_counted():
    """A named `const` arrow nested inside a named function must own its own
    calls exclusively — not also attributed to the enclosing function (this
    was a latent double-count the boundary-skip fix also closes)."""
    src = (
        "function outer() {\n"
        "  const inner = () => {\n"
        "    innerCall();\n"
        "  };\n"
        "  outerCall();\n"
        "}\n"
    )
    result = _ts_extract(src, "typescript", "mod")
    inner_refs = _refs_to(result, "innerCall")
    outer_refs = _refs_to(result, "outerCall")
    assert len(inner_refs) == 1
    assert inner_refs[0].src_qname == "mod.outer.inner"
    assert len(outer_refs) == 1
    assert outer_refs[0].src_qname == "mod.outer"


# ---------- integration test: index_project + who_calls + find_dead_code ----------


@pytest.mark.asyncio
async def test_who_calls_finds_inline_handler_caller(workspace):
    (workspace / "inbound-email.service.ts").write_text(
        "export async function ingestInboundEmail(c: any) {\n"
        "  return { ok: true };\n"
        "}\n"
    )
    (workspace / "inbound-email.ts").write_text(
        "import { Hono } from 'hono';\n"
        "import { ingestInboundEmail } from './inbound-email.service.ts';\n"
        "\n"
        "const app = new Hono();\n"
        "\n"
        "app.post('/', async (c) => {\n"
        "  const result = await ingestInboundEmail(c);\n"
        "  return c.json(result);\n"
        "});\n"
        "\n"
        "export default app;\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "who_calls", {"qname": "inbound-email.service.ingestInboundEmail"}
            )
        ).data
        assert out["count"] >= 1, out
        assert any(
            caller["file_path"] == "inbound-email.ts" for caller in out["callers"]
        ), out


@pytest.mark.asyncio
async def test_find_dead_code_no_false_positive_for_inline_handler_callee(workspace):
    (workspace / "svc.ts").write_text(
        "export async function createUser(c: any) {\n"
        "  return { ok: true };\n"
        "}\n"
    )
    (workspace / "routes.ts").write_text(
        "import { Hono } from 'hono';\n"
        "import { createUser } from './svc.ts';\n"
        "\n"
        "const app = new Hono();\n"
        "\n"
        "app.post('/users', async (c) => {\n"
        "  return app_json(await createUser(c));\n"
        "});\n"
        "\n"
        "export default app;\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "find_dead_code",
                {"include_non_python": True, "include_public": True},
            )
        ).data
        dead_names = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any(name.endswith("createUser") for name in dead_names), dead_names
