"""Precision of `inherits` / `references` / `calls` for Python imports.

A wrong edge keeps a project class alive in `find_dead_code` and adds false
dependents to `analyze_impact`, so each case here pins the edge set EXACTLY:
the right edge present and the wrong one absent. The three defects, found by
validating click 8.1.8 and flask 3.1.0:

1. `import typing as t; -> t.Tuple[int, int]` linked a project `class Tuple`
   whenever any directory or module was merely NAMED `typing`;
2. `from .blueprints import Blueprint` (and `class X(pkg.Thing)` through a
   re-export) linked every same-named class in the tree;
3. the edges of an unchanged file depended on which scopes existed when it was
   last resolved.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.py_modules import PyModules
from livespec_mcp.server import mcp
from livespec_mcp.state import get_state

REPRO = {
    "tests/__init__.py": "",
    "tests/typing/__init__.py": "",
    "tests/typing/demo.py": "def check() -> None:\n    pass\n",
    "pkg/__init__.py": "from .core import Thing as Thing\n",
    "pkg/blueprints.py": "class Blueprint:\n    pass\n",
    "pkg/core.py": "class Thing:\n    pass\n",
    "pkg/parser.py": "class Thing:\n    pass\n",
    "pkg/sansio/__init__.py": "",
    "pkg/sansio/blueprints.py": "class Blueprint:\n    pass\n",
    "pkg/sansio/app.py": (
        "from .blueprints import Blueprint\n\n\ndef register(bp: Blueprint) -> None:\n    pass\n"
    ),
    "pkg/a.py": "import typing as t\n\n\ndef f() -> t.Tuple[int, int]:\n    return (1, 2)\n",
    "pkg/types.py": "class Tuple:\n    pass\n",
    "pkg/user.py": "import pkg\n\n\nclass X(pkg.Thing):\n    pass\n",
    "pkg/selfref.py": (
        "import typing as t\n\n\n"
        "class Context:\n"
        "    def __enter__(self) -> 'Context':\n        return self\n\n"
        "    def other(self, c: 'Context') -> None:\n        pass\n\n\n"
        "@t.overload\ndef cmd(x: int) -> Context: ...\n"
        "@t.overload\ndef cmd(x: str) -> Context: ...\n"
        "def cmd(x):\n    pass\n"
    ),
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _edges(workspace: Path, kinds=("inherits", "references")) -> set[tuple[str, str, str]]:
    st = get_state(str(workspace))
    marks = ",".join("?" * len(kinds))
    return {
        (r[0], r[1], r[2])
        for r in st.conn.execute(
            f"""SELECT a.qualified_name, b.qualified_name, e.edge_type
                FROM symbol_edge e
                JOIN symbol a ON a.id = e.src_symbol_id
                JOIN symbol b ON b.id = e.dst_symbol_id
                WHERE e.edge_type IN ({marks})""",
            kinds,
        )
    }


async def _index(workspace: Path, *, force: bool = False) -> None:
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace), "force": force})


@pytest.mark.asyncio
async def test_repro_produces_exactly_the_right_edges(workspace: Path):
    _write(workspace, REPRO)
    await _index(workspace)
    assert _edges(workspace) == {
        ("pkg.sansio.app.register", "pkg.sansio.blueprints.Blueprint", "references"),
        ("pkg.user.X", "pkg.core.Thing", "inherits"),
        ("pkg.selfref.cmd", "pkg.selfref.Context", "references"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stdlib_dir", ["typing", "types", "logging", "enum", "collections"])
async def test_stdlib_qualifier_never_links_a_homonymous_project_class(
    workspace: Path, stdlib_dir: str
):
    """Defect 1: the directory/module NAME is not evidence of an import."""
    _write(
        workspace,
        {
            f"tests/{stdlib_dir}/__init__.py": "",
            f"tests/{stdlib_dir}/case.py": "def check():\n    pass\n",
            f"{stdlib_dir}_dir/__init__.py": "",
            "pkg/__init__.py": "",
            "pkg/shapes.py": "class Tuple:\n    pass\n\nclass Handler:\n    pass\n\nclass Enum:\n    pass\n",
            "pkg/use.py": (
                f"import {stdlib_dir} as std\n"
                f"from {stdlib_dir} import Tuple, Handler\n\n"
                "def f() -> std.Tuple[int, int]:\n    return (1, 2)\n\n"
                "def g(h: std.Handler, e: std.Enum) -> Tuple:\n    return h\n\n"
                "class E(std.Enum, Handler):\n    pass\n"
            ),
        },
    )
    await _index(workspace)
    assert _edges(workspace) == set()


@pytest.mark.asyncio
async def test_relative_import_resolves_against_the_importers_own_package(workspace: Path):
    """Defect 2, first form: one module, not every module of that name."""
    _write(
        workspace,
        {
            "pkg/__init__.py": "",
            "pkg/blueprints.py": "class Blueprint:\n    pass\n",
            "pkg/sansio/__init__.py": "",
            "pkg/sansio/blueprints.py": "class Blueprint:\n    pass\n",
            "pkg/sansio/app.py": (
                "from .blueprints import Blueprint\n\ndef one(bp: Blueprint) -> None:\n    pass\n"
            ),
            "pkg/app.py": (
                "from .blueprints import Blueprint\n"
                "from .sansio import blueprints as sb\n\n"
                "def two(bp: Blueprint, other: sb.Blueprint) -> None:\n    pass\n"
            ),
            "pkg/sansio/deep/__init__.py": "",
            "pkg/sansio/deep/leaf.py": (
                "from ..blueprints import Blueprint as BP\n"
                "from ... import blueprints\n\n"
                "def three(a: BP, b: blueprints.Blueprint) -> None:\n    pass\n"
            ),
        },
    )
    await _index(workspace)
    assert _edges(workspace) == {
        ("pkg.sansio.app.one", "pkg.sansio.blueprints.Blueprint", "references"),
        ("pkg.app.two", "pkg.blueprints.Blueprint", "references"),
        ("pkg.app.two", "pkg.sansio.blueprints.Blueprint", "references"),
        ("pkg.sansio.deep.leaf.three", "pkg.sansio.blueprints.Blueprint", "references"),
        ("pkg.sansio.deep.leaf.three", "pkg.blueprints.Blueprint", "references"),
    }


@pytest.mark.asyncio
async def test_reexport_resolves_to_the_single_symbol_the_module_binds(workspace: Path):
    """Defect 2, second form: `from .core import Thing` binds core.Thing only."""
    _write(
        workspace,
        {
            "pkg/__init__.py": "from .core import Thing as Thing\nfrom .parser import Other\n",
            "pkg/core.py": "class Thing:\n    pass\n",
            "pkg/parser.py": "class Thing:\n    pass\n\nclass Other:\n    pass\n",
            "pkg/user.py": (
                "import pkg\nfrom pkg import Thing as T, Other\n\n"
                "class A(pkg.Thing):\n    pass\n\n"
                "class B(T):\n    pass\n\n"
                "class C(Other):\n    pass\n"
            ),
        },
    )
    await _index(workspace)
    assert _edges(workspace) == {
        ("pkg.user.A", "pkg.core.Thing", "inherits"),
        ("pkg.user.B", "pkg.core.Thing", "inherits"),
        ("pkg.user.C", "pkg.parser.Other", "inherits"),
    }


@pytest.mark.asyncio
async def test_wildcard_and_ambiguous_reexports_emit_nothing(workspace: Path):
    _write(
        workspace,
        {
            "pkg/__init__.py": "from .core import *\n",
            "pkg/core.py": "class Thing:\n    pass\n",
            "pkg/user.py": "import pkg\n\n\nclass A(pkg.Thing):\n    pass\n",
            "a/util.py": "class Helper:\n    pass\n",
            "b/util.py": "class Helper:\n    pass\n",
            "c/use.py": "from util import Helper\n\n\nclass D(Helper):\n    pass\n",
        },
    )
    await _index(workspace)
    assert _edges(workspace) == set()


@pytest.mark.asyncio
async def test_aliases_module_aliases_and_src_layout_keep_working(workspace: Path):
    _write(
        workspace,
        {
            "src/flask/__init__.py": "from .app import Flask as Flask\n",
            "src/flask/app.py": "class Flask:\n    pass\n",
            "src/flask/types.py": "class Tuple:\n    pass\n\nclass X:\n    pass\n",
            "src/flask/ext.py": (
                "import flask\nimport flask.types as ft\n"
                "from flask import Flask as App\n"
                "from flask.types import X\n\n"
                "def a() -> flask.Flask: ...\n"
                "def b() -> ft.X: ...\n"
                "def c(app: 'App') -> X: ...\n"
            ),
            "tests/test_app.py": (
                "import typing as t\nfrom flask import Flask\n\n"
                "class T(Flask):\n    pass\n\n"
                "def f() -> t.Tuple[int]: ...\n"
            ),
        },
    )
    await _index(workspace)
    assert _edges(workspace) == {
        ("src.flask.ext.a", "src.flask.app.Flask", "references"),
        ("src.flask.ext.b", "src.flask.types.X", "references"),
        ("src.flask.ext.c", "src.flask.app.Flask", "references"),
        ("src.flask.ext.c", "src.flask.types.X", "references"),
        ("tests.test_app.T", "src.flask.app.Flask", "inherits"),
    }


@pytest.mark.asyncio
async def test_local_homonyms_and_stdlib_bases_sharing_a_name_keep_working(workspace: Path):
    _write(
        workspace,
        {
            "m.py": (
                "from typing import Generic\nimport collections\n\n"
                "class Exception_:\n    pass\n\n"
                "class Generic_:\n    pass\n\n"
                "class Node:\n    pass\n\n"
                "class Box(Generic[int], Exception):\n    pass\n\n"
                "def f() -> Node:\n    class Node:\n        pass\n    x: Node = Node()\n    return x\n"
            ),
            "n.py": "class Generic:\n    pass\n\nclass Exception:\n    pass\n",
        },
    )
    await _index(workspace)
    edges = _edges(workspace)
    # `Generic` is imported from typing, `Exception` is a builtin: neither is n.py's.
    assert not any(d in ("n.Generic", "n.Exception") for _, d, _ in edges)
    # the function-local `Node` shadows the module-level one inside `f`.
    assert {(s, d) for s, d, _ in edges} == {("m.f", "m.f.Node")}


# -- calls --------------------------------------------------------------


@pytest.mark.asyncio
async def test_calls_through_a_stdlib_qualifier_do_not_link_a_homonymous_function(
    workspace: Path,
):
    _write(
        workspace,
        {
            "tests/typing/__init__.py": "",
            "tests/typing/case.py": "def cast():\n    pass\n",
            "pkg/__init__.py": "",
            "pkg/util.py": "def cast(x):\n    return x\n\ndef getLogger():\n    pass\n",
            "pkg/use.py": (
                "import typing as t\nimport logging\n\n"
                "def f(x):\n    log = logging.getLogger('a')\n    return t.cast(int, x)\n"
            ),
        },
    )
    await _index(workspace)
    assert _edges(workspace, ("calls",)) == set()


@pytest.mark.asyncio
async def test_calls_resolve_relative_import_to_one_module(workspace: Path):
    _write(
        workspace,
        {
            "pkg/__init__.py": "",
            "pkg/helpers.py": "def build():\n    pass\n",
            "pkg/sansio/__init__.py": "",
            "pkg/sansio/helpers.py": "def build():\n    pass\n",
            "pkg/sansio/app.py": (
                "from .helpers import build\nfrom .. import helpers\n\n"
                "def run():\n    build()\n\ndef run2():\n    helpers.build()\n"
            ),
        },
    )
    await _index(workspace)
    assert _edges(workspace, ("calls",)) == {
        ("pkg.sansio.app.run", "pkg.sansio.helpers.build", "calls"),
        ("pkg.sansio.app.run2", "pkg.helpers.build", "calls"),
    }


@pytest.mark.asyncio
async def test_calls_through_stdlib_json_do_not_reach_a_json_package_of_the_project(
    workspace: Path,
):
    """The shape that surfaced on flask: `src/flask/json/` exists, so
    `_json.dumps()` (the stdlib) linked every `dumps` under it."""
    _write(
        workspace,
        {
            "src/app/__init__.py": "",
            "src/app/json/__init__.py": "def dumps(o):\n    pass\n",
            "src/app/json/provider.py": "class P:\n    def dumps(self, o):\n        pass\n",
            "src/app/use.py": (
                "import json as _json\nfrom .json import dumps\n\n"
                "def a(o):\n    return _json.dumps(o)\n\n"
                "def b(o):\n    return dumps(o)\n"
            ),
        },
    )
    await _index(workspace)
    assert _edges(workspace, ("calls",)) == {
        ("src.app.use.b", "src.app.json.__init__.dumps", "calls")
    }


# -- order independence ---------------------------------------------------


def _snapshot(tree: Path, dest: Path) -> None:
    shutil.copytree(tree, dest, ignore=shutil.ignore_patterns(".mcp-docs"))


@pytest.mark.asyncio
async def test_incremental_edges_equal_a_fresh_full_index_across_edits(
    workspace: Path, tmp_path_factory
):
    """Defect 3: after each edit, an incremental index and an index of the same
    tree from scratch hold the same inherits/references/calls edges."""
    _write(workspace, REPRO)
    await _index(workspace)
    steps = [
        ("add a homonym in a stdlib-named dir", {"tests/typing/more.py": "class Tuple: ...\n"}),
        (
            "add a second Blueprint next to the importer",
            {"pkg/other/__init__.py": "", "pkg/other/blueprints.py": "class Blueprint: ...\n"},
        ),
        (
            "add a dir named like a stdlib module",
            {
                "pkg/logging/__init__.py": "class Logger: ...\n",
                "pkg/log_use.py": (
                    "import logging\n\ndef g() -> logging.Logger: ...\n\n"
                    "def h() -> logging.Tuple: ...\n"
                ),
            },
        ),
        (
            "a new file using Thing via the re-export",
            {"pkg/late.py": "import pkg\nclass L(pkg.Thing): ...\n"},
        ),
        ("a top-level module named typing", {"typing.py": "class Tuple: ...\n"}),
    ]
    for label, files in steps:
        _write(workspace, files)
        await _index(workspace)
        fresh = tmp_path_factory.mktemp("fresh") / "ws"
        _snapshot(workspace, fresh)
        await _index(fresh)
        kinds = ("inherits", "references", "calls")
        assert _edges(workspace, kinds) == _edges(fresh, kinds), label

    # rename a module: its importers are unchanged files
    (workspace / "pkg" / "types.py").rename(workspace / "pkg" / "kinds.py")
    await _index(workspace)
    fresh = tmp_path_factory.mktemp("fresh") / "ws"
    _snapshot(workspace, fresh)
    await _index(fresh)
    assert _edges(workspace, kinds) == _edges(fresh, kinds), "rename"
    await _index(workspace, force=True)
    assert _edges(workspace, kinds) == _edges(fresh, kinds), "forced"


@pytest.mark.asyncio
async def test_retargeting_a_reexport_adds_the_new_edge_and_keeps_the_old_one_until_forced(
    workspace: Path,
):
    """The one order dependence left by design: `_resolve_refs` never DELETEs
    from `symbol_edge` (refs of unchanged files must survive), so an edge made
    wrong by an edit to a THIRD file lingers in an incremental index and goes
    away on `force=True`. The new edge is always there."""
    _write(workspace, REPRO)
    await _index(workspace)
    _write(workspace, {"pkg/__init__.py": "from .parser import Thing as Thing\n"})
    await _index(workspace)
    old = ("pkg.user.X", "pkg.core.Thing", "inherits")
    new = ("pkg.user.X", "pkg.parser.Thing", "inherits")
    assert new in _edges(workspace)
    assert old in _edges(workspace)  # stale, by design
    await _index(workspace, force=True)
    assert {e for e in _edges(workspace) if e[0] == "pkg.user.X"} == {new}


# -- resolver level ---------------------------------------------------------


def _mods(files: dict[str, str]) -> PyModules:
    ids = {i: p for i, p in enumerate(files, 1)}
    by_path = {p: s for p, s in files.items()}
    return PyModules(ids, lambda fid: by_path[ids[fid]])


def test_resolve_module_counts_dots_from_the_importing_package():
    m = _mods({"pkg/a/x.py": "", "pkg/a/__init__.py": "", "pkg/b.py": "", "pkg/__init__.py": ""})
    x = 1
    assert m.resolve_module(".", x).file_id == 2  # pkg/a/__init__.py
    assert m.resolve_module("..b", x).file_id == 3
    assert m.resolve_module("..", x).file_id == 4
    assert m.resolve_module("...", x) is None  # above the project
    assert m.resolve_module("..b.Cls", x).remainder == ("Cls",)


def test_resolve_module_matches_whole_segments_only():
    m = _mods(
        {"src/click/types.py": "", "src/click/__init__.py": "", "tests/typing/__init__.py": ""}
    )
    assert m.resolve_module("click.types", 1).file_id == 1
    assert m.resolve_module("lick.types", 1) is None
    assert m.resolve_module("ick", 1) is None
    assert m.resolve_module("typing", 1) is None
    assert m.resolve_module("os.path", 1) is None


def test_lookup_type_follows_reexport_chain_and_stops_on_cycles():
    files = {
        "p/__init__.py": "from .q import Thing\n",
        "p/q/__init__.py": "from .r import Thing\n",
        "p/q/r.py": "class Thing: ...\n",
        "p/u.py": "",
        "c/__init__.py": "from .d import Loop\n",
        "c/d.py": "from . import Loop\n",
    }
    m = _mods(files)
    cands = [(10, "p.q.r.Thing", 3)]
    assert m.lookup_type("p", "Thing", 4, cands) == (cands[0], True)
    assert m.lookup_type("p.q.r", "Thing", 4, cands) == (cands[0], False)
    assert m.lookup_type("c", "Loop", 4, cands) is None  # a cycle binds nothing
