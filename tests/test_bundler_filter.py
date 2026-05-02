"""v0.11 P0: bundler-output dirs filtered from top_symbols + find_dead_code.

Bug #18 (TS/JS): Deno Fresh `_fresh/`, Next `.next/`, Webpack `dist/`,
`node_modules/`, etc. produce huge generated symbol blobs that pollute
project overview and dead-code surface.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools.analysis import _is_bundler_output_path


def test_bundler_path_helper():
    assert _is_bundler_output_path("_fresh/snapshot.js")
    assert _is_bundler_output_path("dist/index.js")
    assert _is_bundler_output_path("build/foo.js")
    assert _is_bundler_output_path(".next/server/pages/api.js")
    assert _is_bundler_output_path("out/bar.js")
    assert _is_bundler_output_path("node_modules/lodash/index.js")
    assert _is_bundler_output_path(".svelte-kit/output/x.js")
    assert _is_bundler_output_path("target/debug/foo.rs")
    assert _is_bundler_output_path("pkg/__pycache__/mod.cpython-311.pyc")
    assert _is_bundler_output_path("vendor/app.min.js")
    assert _is_bundler_output_path("static/app.bundle.js")
    # Nested under another dir still matches via /<dir>/ check
    assert _is_bundler_output_path("packages/web/dist/x.js")
    # Negative cases — real source paths
    assert not _is_bundler_output_path("src/index.ts")
    assert not _is_bundler_output_path("lib/foo.py")
    assert not _is_bundler_output_path("")
    assert not _is_bundler_output_path("distance.py")  # prefix-only false match guard


@pytest.mark.asyncio
async def test_dead_code_skips_bundler_dirs(workspace):
    """A function in dist/ must not appear in find_dead_code default output."""
    src = workspace / "src"
    src.mkdir()
    (src / "real.py").write_text("def real_dead():\n    return 1\n")

    dist = workspace / "dist"
    dist.mkdir()
    (dist / "bundle.py").write_text("def bundled_dead():\n    return 2\n")

    nm = workspace / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "mod.py").write_text("def vendored_dead():\n    return 3\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert any("real_dead" in q for q in qnames)
        assert not any("bundled_dead" in q for q in qnames)
        assert not any("vendored_dead" in q for q in qnames)


@pytest.mark.asyncio
async def test_overview_top_symbols_skips_bundler(workspace):
    """top_symbols should not surface symbols from dist/ etc."""
    src = workspace / "src"
    src.mkdir()
    (src / "a.py").write_text(
        "def core_fn():\n    helper()\n\n"
        "def helper():\n    return 1\n"
    )
    dist = workspace / "dist"
    dist.mkdir()
    (dist / "bundle.py").write_text("def bundled_fn():\n    return 1\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("get_project_overview", {})).data
        names = {s["name"] for s in out["top_symbols"]}
        assert not any("bundled_fn" == n for n in names)
