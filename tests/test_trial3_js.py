"""Third end-to-end trial (a JavaScript CLI): the defects it found.

`new X(...)` left no edge, so a class built only with `new` read as dead;
module symbols were dead-code candidates; vendored bundles were 92% of the
index; and two middleware errors reached the caller as a list envelope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.config import load_repo_config
from livespec_mcp.domain.indexer import DEFAULT_IGNORES, IndexStats, _iter_files
from livespec_mcp.server import mcp
from tests.conftest import requires_grammar
from tests.test_trace_ingest import _Trace


async def _index(c: Client, **kw) -> dict:
    return (await c.call_tool("index_project", kw)).data


async def _dead(c: Client, **kw) -> dict:
    return (await c.call_tool("find_dead_code", kw)).data


# ---------- 1. `new X(...)` is a call of X ----------


def _new_repo(root: Path) -> None:
    (root / "lib").mkdir()
    (root / "lib" / "errors.js").write_text(
        "class UsageError extends Error {}\n"
        "class Unused {}\n"
        "class Local {}\n"
        "function fail() { throw new UsageError('x'); }\n"
        "const make = () => new Local(1);\n"
        "module.exports = { UsageError, fail, make };\n"
    )
    (root / "lib" / "use.js").write_text(
        "const { UsageError } = require('./errors');\n"
        "const errors = require('./errors');\n"
        "function viaNamed() { return new UsageError('a'); }\n"
        "function viaNamespace() { return new errors.UsageError('b'); }\n"
        "function viaThis() { return new this.Unknown(); }\n"
        "module.exports = { viaNamed, viaNamespace, viaThis };\n"
    )


def _edges(root: Path) -> set[tuple[str, str, str]]:
    from livespec_mcp.state import get_state

    st = get_state(root)
    return {
        (r["s"], r["t"], r["d"])
        for r in st.conn.execute(
            """SELECT a.qualified_name s, e.edge_type t, b.qualified_name d
               FROM symbol_edge e JOIN symbol a ON a.id=e.src_symbol_id
               JOIN symbol b ON b.id=e.dst_symbol_id"""
        )
    }


@pytest.mark.asyncio
async def test_new_expression_makes_a_calls_edge(workspace):
    requires_grammar("javascript")
    _new_repo(workspace)
    async with Client(mcp) as c:
        await _index(c)
        callers = (await c.call_tool("who_calls", {"qname": "lib.errors.UsageError"})).data
    edges = _edges(workspace)
    assert ("lib.errors.fail", "calls", "lib.errors.UsageError") in edges
    assert ("lib.errors.make", "calls", "lib.errors.Local") in edges
    # imported by name, and through a module namespace: same import scope as a call
    assert ("lib.use.viaNamed", "calls", "lib.errors.UsageError") in edges
    assert ("lib.use.viaNamespace", "calls", "lib.errors.UsageError") in edges
    # `new this.Unknown()` names nothing the file's imports can place
    assert not any(s == "lib.use.viaThis" for s, _, _ in edges)
    assert {x["qualified_name"] for x in callers["callers"]} >= {
        "lib.errors.fail",
        "lib.use.viaNamed",
        "lib.use.viaNamespace",
    }


@pytest.mark.asyncio
async def test_class_built_only_with_new_is_not_dead_code(workspace):
    requires_grammar("javascript")
    _new_repo(workspace)
    async with Client(mcp) as c:
        await _index(c)
        dead = await _dead(c, include_public=True)
    names = {d["qualified_name"] for d in dead["dead_symbols"]}
    assert "lib.errors.UsageError" not in names
    assert "lib.errors.Local" not in names
    assert "lib.errors.Unused" in names  # never constructed: still reported


@pytest.mark.asyncio
async def test_new_expression_in_typescript(workspace):
    requires_grammar("typescript")
    (workspace / "a.ts").write_text(
        "export class Box<T> { constructor(public v: T) {} }\n"
        "export function mk() { return new Box<number>(1); }\n"
    )
    async with Client(mcp) as c:
        await _index(c)
    assert ("a.mk", "calls", "a.Box") in _edges(workspace)


# ---------- 2. exported JS functions with no caller ----------


@pytest.mark.asyncio
async def test_uncalled_exports_are_listed_esm_with_include_public_cjs_always(workspace):
    requires_grammar("javascript")
    (workspace / "esm.js").write_text(
        "export function esmDead() { return 1; }\nfunction plainDead() { return 2; }\n"
    )
    (workspace / "cjs.js").write_text(
        "function cjsDead() { return 1; }\nmodule.exports = { cjsDead };\n"
    )
    async with Client(mcp) as c:
        await _index(c)
        default = {d["qualified_name"] for d in (await _dead(c))["dead_symbols"]}
        public = {
            d["qualified_name"] for d in (await _dead(c, include_public=True))["dead_symbols"]
        }
    assert "esm.esmDead" not in default  # `export` protects by default
    assert "esm.esmDead" in public
    assert {"esm.plainDead", "cjs.cjsDead"} <= default <= public


# ---------- 3. module symbols are not removable code ----------


@pytest.mark.asyncio
async def test_module_symbols_are_never_candidates_or_rescued(workspace):
    requires_grammar("javascript")
    (workspace / "lib").mkdir()
    (workspace / "lib" / "m.js").write_text(
        "function ran() { return 1; }\nfunction idle() { return 2; }\nmodule.exports = { ran };\n"
    )
    t = _Trace()
    t.enter("ran", lang="node", module="m", file="lib/m.js", line=1)
    trace = t.write(workspace / "t" / "trace.jsonl")
    async with Client(mcp) as c:
        await _index(c)
        flags = {"include_public": True, "include_infrastructure": True, "include_tests": True}
        before = await _dead(c, **flags)
        await c.call_tool("ingest_traces", {"trace_path": trace, "dry_run": False})
        after = await _dead(c, **flags)
    assert all(d["kind"] != "module" for d in before["dead_symbols"])
    assert all(d["kind"] != "module" for d in after["dead_symbols"])
    assert "module" not in after["by_kind"]
    rescued = {r["qualified_name"] for r in after["runtime"]["rescued"]}
    assert rescued == {"lib.m.ran"}
    assert [d["qualified_name"] for d in after["dead_symbols"]] == ["lib.m.idle"]


# ---------- 4. generated and vendored code ----------


def _generated_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.js").write_text("function app() { return 1; }\n")
    for d in ("vendor", "third_party", "dist", "build", "out", "coverage", "node_modules"):
        (root / d / "sub").mkdir(parents=True)
        (root / d / "sub" / "x.js").write_text("function inDir() { return 1; }\n")
    (root / "src" / "lib.min.js").write_text("function a(){}\n")
    (root / "src" / "pack.bundle.js").write_text("function b(){}\n")
    (root / "src" / "pack.bundle.mjs").write_text("function c(){}\n")
    (root / "src" / "wide.js").write_text("var x=1;" + "x=x+1;" * 400 + "\n")  # one 2.4k line
    (root / "src" / "tall.js").write_text("\n".join(f"var v{i} = {i};" for i in range(400)) + "\n")


def _walk(root: Path, **kw) -> tuple[set[str], IndexStats]:
    stats = IndexStats()
    files = _iter_files(root, DEFAULT_IGNORES, load_repo_config(root), generated=stats, **kw)
    return {p.relative_to(root).as_posix() for p in files}, stats


def test_generated_and_vendored_code_is_skipped_and_counted(tmp_path):
    _generated_tree(tmp_path)
    names, stats = _walk(tmp_path)
    assert names == {"src/app.js", "src/tall.js"}
    # 6 skipped directories (node_modules is a silent ignore) + 4 bundle files
    assert stats.generated_files == 6 + 4
    assert len(stats.generated_paths) == 10
    assert "vendor/" in stats.generated_paths and "src/wide.js" in stats.generated_paths


def test_generated_paths_are_capped_at_ten_but_files_are_counted(tmp_path):
    (tmp_path / "vendor").mkdir()
    for i in range(25):
        (tmp_path / "vendor" / f"f{i}.js").write_text("function f(){}\n")
    for i in range(14):
        (tmp_path / f"b{i}.min.js").write_text("function f(){}\n")
    _, stats = _walk(tmp_path)
    assert stats.generated_files == 25 + 14
    assert len(stats.generated_paths) == 10


def test_include_generated_indexes_everything_but_node_modules(tmp_path):
    _generated_tree(tmp_path)
    names, stats = _walk(tmp_path, include_generated=True)
    assert "vendor/sub/x.js" in names and "src/lib.min.js" in names and "src/wide.js" in names
    assert not any(n.startswith("node_modules/") for n in names)
    assert stats.generated_files == 0 and stats.generated_paths == []


def test_repo_config_reincludes_a_generated_path(tmp_path):
    _generated_tree(tmp_path)
    (tmp_path / ".livespec.toml").write_text('[index]\nignore = ["!vendor/", "!src/wide.js"]\n')
    names, _ = _walk(tmp_path)
    assert "vendor/sub/x.js" in names and "src/wide.js" in names
    assert "third_party/sub/x.js" not in names


@pytest.mark.asyncio
async def test_index_project_reports_what_it_skipped(workspace):
    requires_grammar("javascript")
    _generated_tree(workspace)
    async with Client(mcp) as c:
        out = await _index(c)
        assert out["files_total"] == 2
        assert out["generated_skipped"]["files"] == 10
        assert len(out["generated_skipped"]["paths"]) == 10
        assert "include_generated" in out["generated_skipped"]["hint"]
        again = await _index(c, include_generated=True)
    assert "generated_skipped" not in again
    assert again["files_total"] > 2


@pytest.mark.asyncio
async def test_reindex_drops_files_that_became_generated(workspace):
    requires_grammar("javascript")
    _generated_tree(workspace)
    async with Client(mcp) as c:
        await _index(c, include_generated=True)
        out = await _index(c)
    assert out["files_deleted"] is True
    assert out["files_total"] == 2


@pytest.mark.asyncio
async def test_clean_project_has_no_generated_skipped_key(workspace):
    requires_grammar("javascript")
    (workspace / "a.js").write_text("function a() { return 1; }\n")
    async with Client(mcp) as c:
        out = await _index(c)
    assert "generated_skipped" not in out


# ---------- 5. middleware errors are the canonical object, not a list envelope ----------


@pytest.mark.asyncio
async def test_middleware_error_text_is_the_error_object_itself(workspace):
    (workspace / "a.py").write_text("def f():\n    return 1\n")
    async with Client(mcp) as c:
        await _index(c)
        res = await c.call_tool("who_calls", {"symbol": "x"}, raise_on_error=False)
    body = json.loads(res.content[0].text)
    assert isinstance(body, dict), "text must be the error object, not a content list"
    assert body["isError"] is True and "unknown parameter" in body["error"]
    assert "qname" in body["did_you_mean"]


@pytest.mark.asyncio
async def test_plugin_blocked_error_text_is_the_error_object_itself(workspace, monkeypatch):
    monkeypatch.delenv("LIVESPEC_PLUGINS", raising=False)
    (workspace / "a.py").write_text("def f():\n    return 1\n")
    async with Client(mcp) as c:
        await _index(c)
        res = await c.call_tool("create_spec", {"spec_id": "x", "title": "t"}, raise_on_error=False)
    body = json.loads(res.content[0].text)
    assert isinstance(body, dict) and body["isError"] is True
