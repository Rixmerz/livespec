"""Integration tests for scoped resolution (P0.4 Python + P1.A1 TS/JS).

Verifies that when a ref carries `scope_module` (because the target name was
imported in the source file), the resolver picks the in-scope candidate and
emits a `symbol_edge` with `weight=1.0`. Without scoping, ambiguous targets
would resolve to a fallback `weight=0.5`.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from livespec_mcp.config import Settings
from livespec_mcp.domain.indexer import index_project
from livespec_mcp.storage.db import connect

FIXTURES = Path(__file__).parent / "fixtures"


def _bootstrap(tmp_path: Path) -> tuple[Settings, sqlite3.Connection]:
    state = tmp_path / ".mcp-docs"
    settings = Settings(
        workspace=tmp_path,
        state_dir=state,
        db_path=state / "docs.db",
        docs_dir=state / "docs",
    )
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    return settings, conn


def _edge_weight(conn: sqlite3.Connection, src_qname: str, dst_qname: str) -> float | None:
    row = conn.execute(
        """SELECT e.weight FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN symbol d ON d.id = e.dst_symbol_id
           WHERE s.qualified_name = ? AND d.qualified_name = ?""",
        (src_qname, dst_qname),
    ).fetchone()
    return float(row["weight"]) if row else None


@pytest.mark.parametrize(
    "lang_dir,suffix",
    [
        ("typescript", "ts"),
        ("javascript", "js"),
    ],
)
def test_ts_js_cross_module_edges_weight_1(tmp_path: Path, lang_dir: str, suffix: str):
    """P1.A1: cross-module calls resolve to weight=1.0 thanks to import scoping.

    Both ES6 named imports (`import { helper } from './helpers'`) and
    namespace imports (`import * as utils from './utils'`) populate
    `symbol_ref.scope_module`. The resolver then picks the in-scope target
    over any same-named candidate elsewhere in the project.
    """
    src = FIXTURES / lang_dir / "cross_module"
    dst = tmp_path / "src"
    shutil.copytree(src, dst)

    settings, conn = _bootstrap(tmp_path)
    stats = index_project(settings, conn)

    assert stats.symbols_total >= 4, f"expected ≥4 symbols, got {stats.symbols_total}"
    assert stats.edges_total >= 2, f"expected ≥2 edges, got {stats.edges_total}"

    # Named import: main.main -> helpers.helper, weight 1.0
    w_named = _edge_weight(conn, "src.main.main", "src.helpers.helper")
    assert w_named == 1.0, (
        f"named import edge should be weight=1.0, got {w_named} "
        f"(suffix={suffix})"
    )

    # Namespace import via utils.format(), resolved by leftmost-name lookup
    w_ns = _edge_weight(conn, "src.main.main", "src.utils.format")
    assert w_ns == 1.0, (
        f"namespace-import edge should be weight=1.0, got {w_ns} "
        f"(suffix={suffix})"
    )

    conn.close()


def test_go_cross_package_edges_weight_1(tmp_path: Path):
    """P1.A2: Go cross-package calls (`pkg.Func()` after `import …/pkg`) resolve
    to weight=1.0. Aliased imports (`alias "..."`) also resolve via the path's
    last segment, not the alias."""
    src = FIXTURES / "go" / "cross_package"
    dst = tmp_path / "proj"
    shutil.copytree(src, dst)

    settings, conn = _bootstrap(tmp_path)
    stats = index_project(settings, conn)
    assert stats.symbols_total >= 3

    # Plain import: util.Helper()
    w_plain = _edge_weight(conn, "proj.cmd.main.Run", "proj.util.format.Helper")
    assert w_plain == 1.0, f"Go plain-import edge should be weight=1.0, got {w_plain}"

    # Aliased import resolves through alias -> last-segment
    w_alias = _edge_weight(conn, "proj.cmd.main.Run", "proj.util.format.Format")
    assert w_alias == 1.0, f"Go aliased-import edge should be weight=1.0, got {w_alias}"
    conn.close()


def test_ruby_require_relative_edge_weight_1(tmp_path: Path):
    """P1.A4: `require_relative 'helpers'` lets `Helpers.method()` resolve to
    weight=1.0. Best-effort heuristic — only matches when the basename
    matches the constant name (the common Ruby convention)."""
    src = FIXTURES / "ruby" / "cross_module"
    dst = tmp_path / "lib"
    shutil.copytree(src, dst)

    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)

    w = _edge_weight(conn, "lib.main.run", "lib.helpers.Helpers.double")
    assert w == 1.0, (
        f"Ruby require_relative + Const.method should be weight=1.0, got {w}"
    )
    conn.close()


def test_php_use_namespace_edge_weight_1(tmp_path: Path):
    """P1.A4: PHP `use Service\\Greeter;` followed by `Greeter::method()` resolves
    via `scope` field on scoped_call_expression. Method calls on instance vars
    (`$g->method()`) are not resolved — would need flow analysis."""
    src = FIXTURES / "php" / "cross_module"
    dst = tmp_path / "app"
    shutil.copytree(src, dst)

    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)

    w = _edge_weight(
        conn,
        "app.main.run",
        "app.Service.Greeter.Greeter.makeDefault",
    )
    assert w == 1.0, (
        f"PHP use + scoped_call should be weight=1.0, got {w}"
    )
    conn.close()


def test_rust_use_declaration_edges_weight_1(tmp_path: Path):
    """P4.A3 v0.5: Rust `use crate::module::Item` enables cross-module
    weight=1.0 edges. Both `Item::method()` (scoped_call) and a bare
    imported function call resolve through the use payload."""
    src = FIXTURES / "rust" / "cross_module"
    dst = tmp_path / "proj"
    shutil.copytree(src, dst)

    settings, conn = _bootstrap(tmp_path)
    stats = index_project(settings, conn)
    assert stats.symbols_total >= 4

    # `Greeter::make_default()` -> proj.src.util.Greeter::make_default
    w_method = _edge_weight(
        conn,
        "proj.src.main.run",
        "proj.src.util.Greeter::make_default",
    )
    assert w_method == 1.0, (
        f"Rust scoped-call edge should be weight=1.0, got {w_method}"
    )

    # `helper(...)` -> proj.src.util.helper
    w_helper = _edge_weight(
        conn,
        "proj.src.main.run",
        "proj.src.util.helper",
    )
    assert w_helper == 1.0, (
        f"Rust use+call edge should be weight=1.0, got {w_helper}"
    )
    conn.close()


def test_python_cross_module_edges_weight_1(tmp_path: Path):
    """P0.4 regression lock-in: Python scoped resolution still emits
    weight=1.0 for `from pkg.x import foo; foo()` patterns."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text("def helper():\n    return 1\n")
    (pkg / "main.py").write_text(
        "from pkg.helpers import helper\n"
        "\n"
        "def main():\n"
        "    return helper()\n"
    )

    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)

    w = _edge_weight(conn, "pkg.main.main", "pkg.helpers.helper")
    assert w == 1.0, f"Python from-import edge should be weight=1.0, got {w}"
    conn.close()


def test_same_name_fanout_prefers_same_file(tmp_path: Path):
    """v0.8 P2 session-01 fix: when a short name matches multiple symbols
    across the project AND no scope_module is captured, the resolver should
    prefer the same-file candidate over fanning out to all of them.

    Fixture mirrors the jig pattern that surfaced the bug:
      - embed_cache.py defines list_tools, _cosine, AND a search() that
        calls both in-module.
      - internal_proxy.py also defines list_tools.
      - proxy_pool.py also defines _cosine.
    Before the fix, search() got edges to all 3 list_tools candidates and
    both _cosine candidates with weight 0.5. After the fix, only the
    same-file targets get edges (weight 0.7 — confident in-file resolution).
    """
    src = FIXTURES / "python" / "same_name_fanout"
    dst = tmp_path / "pkg"
    shutil.copytree(src, dst)

    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)

    # Correct same-file resolution must produce an edge.
    # Weight may be 1.0 (Python AST extractor sets scope to current module,
    # narrowing candidates to 1) OR 0.7 (same-file fallback when scope is
    # absent). Either path closes the bug; what matters is no fan-out.
    w_correct = _edge_weight(
        conn, "pkg.embed_cache.search", "pkg.embed_cache.list_tools"
    )
    assert w_correct is not None and w_correct >= 0.7, (
        f"same-file resolution must produce an edge with weight ≥ 0.7, got {w_correct}"
    )
    w_correct_cos = _edge_weight(
        conn, "pkg.embed_cache.search", "pkg.embed_cache._cosine"
    )
    assert w_correct_cos is not None and w_correct_cos >= 0.7, (
        f"same-file _cosine resolution must produce an edge with weight ≥ 0.7, got {w_correct_cos}"
    )

    # The actual bug fix: fan-out targets must NOT have an edge from search().
    w_wrong_lt = _edge_weight(
        conn, "pkg.embed_cache.search", "pkg.internal_proxy.list_tools"
    )
    assert w_wrong_lt is None, (
        f"resolver fanned out to internal_proxy.list_tools (got weight={w_wrong_lt}) — "
        "should be filtered by same-file preference"
    )
    w_wrong_cos = _edge_weight(
        conn, "pkg.embed_cache.search", "pkg.proxy_pool._cosine"
    )
    assert w_wrong_cos is None, (
        f"resolver fanned out to proxy_pool._cosine (got weight={w_wrong_cos}) — "
        "should be filtered by same-file preference"
    )

    # Method-on-class same-name candidate (proxy_pool.McpConnection.list_tools)
    # must also be filtered.
    w_wrong_method = _edge_weight(
        conn,
        "pkg.embed_cache.search",
        "pkg.proxy_pool.McpConnection.list_tools",
    )
    assert w_wrong_method is None, (
        f"resolver fanned out to McpConnection.list_tools (got weight={w_wrong_method})"
    )

    conn.close()
