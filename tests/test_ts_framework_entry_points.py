"""v0.11 P1: TS framework entry-point detection (bug #19).

`find_dead_code` was over-reporting symbols in TS framework apps because
route/component files (Fresh islands, Next.js pages, SvelteKit routes) are
reachable via filesystem-based routing, not call edges.

Coverage:
- Helper unit tests for `_ts_framework_entry_point_kind` / `_is_ts_framework_entry_point`
- Fresh islands/ → not flagged dead
- Next.js pages router → not flagged dead
- Next.js app router page.tsx → not flagged dead
- SvelteKit routes/+page.server.ts → not flagged dead
- Negative: regular src/lib/utils.ts orphan IS still dead
- `find_endpoints(framework='nextjs')` surfaces page symbols
- `find_endpoints(framework='fresh')` surfaces island symbols
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.analysis import (
    _is_ts_framework_entry_point,
    _ts_framework_entry_point_kind,
)


# ---------------------------------------------------------------------------
# Unit tests for the helpers (no MCP round-trip needed)
# ---------------------------------------------------------------------------


class TestTsFrameworkEntryPointKind:
    """_ts_framework_entry_point_kind path-matching contract."""

    # --- Fresh ---
    def test_fresh_island_tsx(self):
        assert _ts_framework_entry_point_kind("islands/Counter.tsx") == "fresh"

    def test_fresh_island_with_src_prefix(self):
        assert _ts_framework_entry_point_kind("src/islands/Button.tsx") == "fresh"

    def test_fresh_island_js(self):
        assert _ts_framework_entry_point_kind("islands/Header.js") == "fresh"

    # --- Next.js pages router ---
    def test_nextjs_pages_index(self):
        assert _ts_framework_entry_point_kind("pages/index.tsx") == "nextjs_pages"

    def test_nextjs_pages_nested(self):
        assert _ts_framework_entry_point_kind("pages/blog/[slug].tsx") == "nextjs_pages"

    def test_nextjs_pages_src_prefix(self):
        assert _ts_framework_entry_point_kind("src/pages/index.tsx") == "nextjs_pages"

    # --- Next.js app router ---
    def test_nextjs_app_page(self):
        assert _ts_framework_entry_point_kind("app/dashboard/page.tsx") == "nextjs_app"

    def test_nextjs_app_layout(self):
        assert _ts_framework_entry_point_kind("app/layout.tsx") == "nextjs_app"

    def test_nextjs_app_loading(self):
        assert _ts_framework_entry_point_kind("app/loading.tsx") == "nextjs_app"

    def test_nextjs_app_route(self):
        assert _ts_framework_entry_point_kind("app/api/users/route.ts") == "nextjs_app"

    def test_nextjs_app_src_prefix(self):
        assert _ts_framework_entry_point_kind("src/app/page.tsx") == "nextjs_app"

    def test_nextjs_app_non_magic_file_is_not_entry(self):
        # helpers.ts inside app/ is not a Next.js entry point
        result = _ts_framework_entry_point_kind("app/helpers.ts")
        assert result != "nextjs_app"

    # --- SvelteKit ---
    def test_sveltekit_page(self):
        assert _ts_framework_entry_point_kind("src/routes/+page.svelte") == "sveltekit"

    def test_sveltekit_page_server(self):
        assert _ts_framework_entry_point_kind("src/routes/+page.server.ts") == "sveltekit"

    def test_sveltekit_layout(self):
        assert _ts_framework_entry_point_kind("routes/+layout.svelte") == "sveltekit"

    def test_sveltekit_error(self):
        assert _ts_framework_entry_point_kind("src/routes/+error.svelte") == "sveltekit"

    # --- Negative cases ---
    def test_regular_ts_file_not_entry(self):
        assert _ts_framework_entry_point_kind("src/lib/utils.ts") is None

    def test_python_file_not_entry(self):
        assert _ts_framework_entry_point_kind("pkg/views.py") is None

    def test_empty_path(self):
        assert _ts_framework_entry_point_kind("") is None

    def test_components_dir_not_entry(self):
        # A component in a generic components/ dir is NOT a routing entry point
        assert _ts_framework_entry_point_kind("src/components/Button.tsx") is None


class TestIsTsFrameworkEntryPoint:
    """_is_ts_framework_entry_point integration with symbol kind."""

    def test_function_in_island(self):
        meta = {
            "file_path": "islands/Counter.tsx",
            "kind": "function",
            "name": "Counter",
            "qualified_name": "Counter",
        }
        assert _is_ts_framework_entry_point(meta) is True

    def test_class_in_pages(self):
        meta = {
            "file_path": "pages/index.tsx",
            "kind": "class",
            "name": "Page",
            "qualified_name": "Page",
        }
        assert _is_ts_framework_entry_point(meta) is True

    def test_variable_in_island_is_not_entry(self):
        # Only functions/classes/methods count; variables are not surfaced
        meta = {
            "file_path": "islands/Counter.tsx",
            "kind": "variable",
            "name": "x",
            "qualified_name": "x",
        }
        assert _is_ts_framework_entry_point(meta) is False

    def test_regular_ts_file_not_entry(self):
        meta = {
            "file_path": "src/lib/utils.ts",
            "kind": "function",
            "name": "helper",
            "qualified_name": "helper",
        }
        assert _is_ts_framework_entry_point(meta) is False


# ---------------------------------------------------------------------------
# Integration tests: find_dead_code suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_island_not_dead(workspace):
    """A top-level fn in islands/ must not be reported as dead code."""
    islands = workspace / "islands"
    islands.mkdir()
    (islands / "Counter.tsx").write_text(
        "export default function Counter() {\n"
        "  return <div>0</div>;\n"
        "}\n"
    )
    # A genuine orphan in a regular dir — must still be flagged
    lib = workspace / "lib"
    lib.mkdir()
    (lib / "orphan.ts").write_text(
        "export function reallyDead() {\n  return 42;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True, "include_public": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("Counter" in q for q in qnames), (
            f"Fresh island Counter should not be dead: {qnames}"
        )
        # The regular orphan should still appear
        assert any("reallyDead" in q for q in qnames), (
            f"Regular orphan reallyDead must be reported: {qnames}"
        )


@pytest.mark.asyncio
async def test_nextjs_pages_not_dead(workspace):
    """Next.js pages-router default export must not be reported dead."""
    pages = workspace / "pages"
    pages.mkdir()
    (pages / "index.tsx").write_text(
        "export default function HomePage() {\n"
        "  return <main>Hello</main>;\n"
        "}\n"
    )
    lib = workspace / "lib"
    lib.mkdir()
    (lib / "orphan.ts").write_text(
        "export function reallyDead() {\n  return 1;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True, "include_public": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("HomePage" in q for q in qnames), (
            f"Next.js pages HomePage should not be dead: {qnames}"
        )
        assert any("reallyDead" in q for q in qnames), (
            f"Regular orphan must be reported: {qnames}"
        )


@pytest.mark.asyncio
async def test_nextjs_app_router_not_dead(workspace):
    """Next.js app-router page.tsx default export must not be reported dead."""
    dashboard = workspace / "app" / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "page.tsx").write_text(
        "export default function DashboardPage() {\n"
        "  return <h1>Dashboard</h1>;\n"
        "}\n"
    )
    lib = workspace / "lib"
    lib.mkdir()
    (lib / "orphan.ts").write_text(
        "export function reallyDead() {\n  return 1;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True, "include_public": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("DashboardPage" in q for q in qnames), (
            f"Next.js app-router DashboardPage should not be dead: {qnames}"
        )
        assert any("reallyDead" in q for q in qnames), (
            f"Regular orphan must be reported: {qnames}"
        )


@pytest.mark.asyncio
async def test_sveltekit_route_server_not_dead(workspace):
    """SvelteKit +page.server.ts top-level fn must not be reported dead."""
    routes = workspace / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "+page.server.ts").write_text(
        "export async function load() {\n"
        "  return { data: [] };\n"
        "}\n"
    )
    lib = workspace / "lib"
    lib.mkdir()
    (lib / "orphan.ts").write_text(
        "export function reallyDead() {\n  return 1;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True, "include_public": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert not any("load" in q and "routes" in q for q in qnames), (
            f"SvelteKit route load() should not be dead: {qnames}"
        )
        assert any("reallyDead" in q for q in qnames), (
            f"Regular orphan must be reported: {qnames}"
        )


@pytest.mark.asyncio
async def test_regular_ts_orphan_still_dead(workspace):
    """A TS function in a plain src/lib/ file with no callers IS dead."""
    lib = workspace / "src" / "lib"
    lib.mkdir(parents=True)
    (lib / "utils.ts").write_text(
        "export function reallyDead() {\n  return 42;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_dead_code", {"include_non_python": True, "include_public": True})
        ).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert any("reallyDead" in q for q in qnames), (
            f"src/lib orphan should be reported dead: {qnames}"
        )


# ---------------------------------------------------------------------------
# Integration tests: find_endpoints TS frameworks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_endpoints_nextjs_surfaces_pages(workspace):
    """find_endpoints(framework='nextjs') returns pages-router components."""
    pages = workspace / "pages"
    pages.mkdir()
    (pages / "index.tsx").write_text(
        "export default function HomePage() {\n"
        "  return <main>Hello</main>;\n"
        "}\n"
    )
    (pages / "about.tsx").write_text(
        "export default function AboutPage() {\n"
        "  return <p>About</p>;\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_endpoints", {"framework": "nextjs"})
        ).data
        qnames = {e["qualified_name"] for e in out["endpoints"]}
        assert any("HomePage" in q for q in qnames), (
            f"NextJS pages HomePage not in endpoints: {qnames}"
        )
        assert any("AboutPage" in q for q in qnames), (
            f"NextJS pages AboutPage not in endpoints: {qnames}"
        )
        # ts_framework label should be set
        ts_fws = {e.get("ts_framework") for e in out["endpoints"]}
        assert "nextjs_pages" in ts_fws


@pytest.mark.asyncio
async def test_find_endpoints_fresh_surfaces_islands(workspace):
    """find_endpoints(framework='fresh') returns Fresh island components."""
    islands = workspace / "islands"
    islands.mkdir()
    (islands / "Counter.tsx").write_text(
        "export default function Counter() {\n"
        "  return <div>0</div>;\n"
        "}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_endpoints", {"framework": "fresh"})
        ).data
        qnames = {e["qualified_name"] for e in out["endpoints"]}
        assert any("Counter" in q for q in qnames), (
            f"Fresh island Counter not in endpoints: {qnames}"
        )
        ts_fws = {e.get("ts_framework") for e in out["endpoints"]}
        assert "fresh" in ts_fws


@pytest.mark.asyncio
async def test_find_endpoints_none_includes_ts_frameworks(workspace):
    """find_endpoints() with no framework filter includes TS routing files."""
    islands = workspace / "islands"
    islands.mkdir()
    (islands / "Nav.tsx").write_text(
        "export default function Nav() {\n  return <nav/>;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {})).data
        qnames = {e["qualified_name"] for e in out["endpoints"]}
        assert any("Nav" in q for q in qnames), (
            f"Fresh island Nav should appear with framework=None: {qnames}"
        )
