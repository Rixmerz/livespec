"""Large procedural fixture: 100+ symbols spread across multiple files
and three languages, with cross-file calls and edge cases.

This is the test the v0.1 suite was missing — the previous fixture had 4 files
and missed two real bugs. This one is generated programmatically so it stays
deterministic without bloating the repo with checked-in fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _build_python_module(root: Path, idx: int, n_funcs: int) -> None:
    """Module with n_funcs functions; each calls the previous one (chain)."""
    pkg = root / f"pkg_{idx:02d}"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    body = ['"""Auto-generated module."""', "", "def fn_0(x):", "    return x + 1", ""]
    for i in range(1, n_funcs):
        body.extend([
            f"def fn_{i}(x):",
            f"    return fn_{i - 1}(x) * 2",
            "",
        ])
    body.extend([
        f"class Helper_{idx}:",
        '    """Class with a few methods.\n\n    @rf:RF-AUTO\n    """',
        "    def step(self, x):",
        f"        return fn_{n_funcs - 1}(x)",
        "",
        "    def double_step(self, x):",
        "        return self.step(self.step(x))",
        "",
    ])
    (pkg / "core.py").write_text("\n".join(body))


def _build_js_module(root: Path, idx: int) -> None:
    js = root / f"js_{idx:02d}.js"
    js.write_text(
        "// Auto-generated JS module with arrow + class\n\n"
        "function helperJs(x) { return x * 3; }\n\n"
        f"const arrowJs_{idx} = (x) => helperJs(x + 1);\n\n"
        f"class JsClass_{idx} {{\n"
        "  greet(name) {\n"
        f"    return helperJs(name.length) + arrowJs_{idx}(name.length);\n"
        "  }\n"
        "}\n"
    )


def _build_go_module(root: Path, idx: int) -> None:
    go = root / f"go_{idx:02d}.go"
    go.write_text(
        f"package mod{idx}\n\n"
        "func Helper(x int) int { return x * 4 }\n\n"
        "type Worker struct{ Name string }\n\n"
        "func (w *Worker) Run() int { return Helper(len(w.Name)) }\n"
    )


@pytest.fixture
def large_repo(tmp_path: Path, monkeypatch) -> Path:
    """Procedural workspace: 5 Python pkgs (with chain calls), 3 JS modules,
    3 Go modules. Total ~120 symbols + ~80 edges."""
    monkeypatch.setenv("LIVESPEC_WORKSPACE", str(tmp_path))
    from livespec_mcp import state as state_module

    state_module.reset_state()
    for i in range(5):
        _build_python_module(tmp_path, i, n_funcs=10)
    for i in range(3):
        _build_js_module(tmp_path, i)
    for i in range(3):
        _build_go_module(tmp_path, i)
    yield tmp_path
    state_module.reset_state()


@pytest.mark.asyncio
async def test_large_repo_indexes_correctly(large_repo):
    async with Client(mcp) as c:
        stats = (await c.call_tool("index_project", {})).data
        # 5 pkgs * (1 init + 1 core) + 3 js + 3 go = 16 files
        assert stats["files_total"] == 16
        # 5 pkgs * (10 fns + 1 class + 2 methods) = 65 + js (3*3=9) + go (3*3=9) = 83-ish
        # Use a generous lower bound — extractor may add more (constructors, etc)
        assert stats["symbols_total"] >= 70, stats
        # Cross-call chain produces at least 5 * 9 = 45 edges from Python alone
        assert stats["edges_total"] >= 40, stats


@pytest.mark.asyncio
async def test_large_repo_partial_reindex_preserves_edges(large_repo):
    async with Client(mcp) as c:
        baseline = (await c.call_tool("index_project", {})).data
        before = baseline["edges_total"]

        # Touch one Python module
        target = large_repo / "pkg_00" / "core.py"
        target.write_text(target.read_text() + "\n# touched\n")
        delta = (await c.call_tool("index_project", {})).data
        assert delta["files_changed"] == 1
        # Tolerance: +/- a handful of edges is OK; massive loss is not
        assert abs(delta["edges_total"] - before) <= 5, (
            f"edges drifted significantly on partial re-index: "
            f"before={before} after={delta['edges_total']}"
        )


@pytest.mark.asyncio
async def test_large_repo_pagerank_consistent(large_repo):
    """PageRank must rank chain-callee functions higher than chain-caller fns,
    because the callee is reached from many sources.
    include_infrastructure=True so 1-line chain helpers aren't filtered (P0.3).
    include_structural_patterns=True because the fixture replicates fn_0..fn_9
    across 5 packages — that triggers the v0.8 structural-pattern filter,
    which is orthogonal to the PageRank-ordering concern this test exercises."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        ov = (
            await c.call_tool(
                "get_project_overview",
                {"include_infrastructure": True, "include_structural_patterns": True},
            )
        ).data
        ranks = {s["qualified_name"]: s["pagerank"] for s in ov["top_symbols"]}
        # fn_0 is at the bottom of every chain so it should outrank fn_9
        # (across all 5 pkgs, fn_0 is a sink for many callers).
        # Use loose check — in any of the 5 pkgs, fn_0 ranks above fn_9.
        wins = sum(
            1 for i in range(5)
            if ranks.get(f"pkg_{i:02d}.core.fn_0", 0) > ranks.get(f"pkg_{i:02d}.core.fn_9", 0)
        )
        assert wins >= 3, f"PageRank ordering broke: {ranks}"
