"""An attribute call `X.f()` links to a project `f` only on evidence about `X`.

Indexing a mixed Python + TypeScript repo made its one module-level `join`
the top symbol of the project: every `"\\n".join(...)`, `os.path.join(...)`
and TypeScript `parts.join(",")` resolved to it by bare name, 457 callers of
which two were real. The receiver is now classified by the extractor
(`symbol_ref.receiver`) and the resolver requires an imported module for a
module-level function; `self.f()` and a call on any other object can only
reach a method; a call never crosses languages.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import (
    RECEIVER_MODULE,
    RECEIVER_OBJECT,
    RECEIVER_SELF,
    extract,
)
from livespec_mcp.server import mcp

from .conftest import requires_grammar


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


_PY_FIXTURE = {
    "pkg/__init__.py": "",
    "pkg/mod.py": "def join(*parts):\n    return '/'.join(parts)\n",
    # No edge expected from any of these.
    "pkg/strings.py": (
        "import os\n"
        "\n"
        "def glue(xs):\n"
        "    return ''.join(xs)\n"
        "\n"
        "def paths(a, b):\n"
        "    return os.path.join(a, b)\n"
        "\n"
        "def on_object(sep, xs):\n"
        "    return sep.join(xs)\n"
        "\n"
        "def on_call(xs):\n"
        "    return str(1).join(xs)\n"
        "\n"
        "class Plain:\n"
        "    def run(self, xs):\n"
        "        return self.join(xs)\n"
    ),
    # Importing the name does not make an ATTRIBUTE call of it that function.
    "pkg/imports_name.py": (
        "from pkg.mod import join\n\ndef dashes(xs):\n    return '-'.join(xs)\n"
    ),
    # Edges expected from each of these.
    "pkg/user_from.py": "from pkg.mod import join\n\ndef use_from():\n    return join('a')\n",
    "pkg/user_alias.py": "import pkg.mod as m\n\ndef use_alias():\n    return m.join('a')\n",
    "pkg/user_module.py": "from pkg import mod\n\ndef use_module():\n    return mod.join('a')\n",
    "pkg/user_lazy.py": ("def use_lazy():\n    from pkg import mod\n\n    return mod.join('a')\n"),
    "pkg/joiner.py": (
        "class Joiner:\n"
        "    def join(self, xs):\n"
        "        return xs\n"
        "\n"
        "    def run(self, xs):\n"
        "        return self.join(xs)\n"
    ),
}


async def _callers(c: Client, qname: str, min_weight: float = 0.0) -> set[str]:
    args = {"qname": qname, "max_depth": 1, "min_weight": min_weight}
    out = (await c.call_tool("who_calls", args)).data
    return {n["qualified_name"] for n in out["callers"]}


@pytest.mark.asyncio
async def test_attribute_calls_need_evidence_of_the_defining_module(workspace: Path):
    _write(workspace, _PY_FIXTURE)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})
        callers = await _callers(c, "pkg.mod.join")
        method_callers = await _callers(c, "pkg.joiner.Joiner.join")
        confirmed = await _callers(c, "pkg.joiner.Joiner.join", min_weight=0.7)

    assert callers == {
        "pkg.user_from.use_from",
        "pkg.user_alias.use_alias",
        "pkg.user_module.use_module",
        "pkg.user_lazy.use_lazy",
    }, callers
    # `self.join()` reaches its own class's method, never the module function,
    # and `self.join()` in a class with no base (`Plain`) reaches no other
    # class's method.
    assert confirmed == {"pkg.joiner.Joiner.run"}, confirmed
    # A call on an object of unknown type (`sep.join`, `str(1).join`) may
    # reach the one METHOD named `join`, but only as a plausible edge: the
    # resolver infers no types, so it is capped below confirmed edges.
    assert method_callers == {
        "pkg.joiner.Joiner.run",
        "pkg.strings.on_object",
        "pkg.strings.on_call",
    }, method_callers


@pytest.mark.asyncio
async def test_a_call_never_crosses_languages(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "def join(*parts):\n    return parts\n",
            "web/list.ts": (
                "export function render(parts: string[]) {\n"
                "  return parts.join(', ') + ['a', 'b'].join('');\n"
                "}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})
        callers = await _callers(c, "pkg.mod.join")
    assert callers == set(), callers


def _extract(tmp_path: Path, rel: str, source: str):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    _, result = extract(path, source, tmp_path)
    return result


def _refs(tmp_path: Path, source: str):
    result = _extract(tmp_path, "pkg/x.py", source)
    return {(r.src_qname.rsplit(".", 1)[-1], r.target_name): r for r in result.refs}


def test_python_receivers_are_classified(tmp_path: Path):
    refs = _refs(
        tmp_path,
        "import os\n"
        "from pkg import mod\n"
        "\n"
        "def a(xs):\n"
        "    return ''.join(xs)\n"
        "\n"
        "def b(p):\n"
        "    return os.path.join(p, p)\n"
        "\n"
        "def c():\n"
        "    return mod.run()\n"
        "\n"
        "def d(obj):\n"
        "    return obj.go()\n"
        "\n"
        "def e():\n"
        "    return plain()\n"
        "\n"
        "class K:\n"
        "    def f(self):\n"
        "        return self.g() or super().h()\n",
    )
    # A literal receiver never becomes a ref.
    assert ("a", "join") not in refs
    assert refs[("b", "join")].receiver == RECEIVER_MODULE
    assert refs[("b", "join")].scope_module == "os.path"
    assert refs[("c", "run")].receiver == RECEIVER_MODULE
    assert refs[("c", "run")].scope_module == "pkg.mod"
    assert refs[("d", "go")].receiver == RECEIVER_OBJECT
    assert refs[("e", "plain")].receiver is None
    assert refs[("f", "g")].receiver == RECEIVER_SELF
    assert refs[("f", "h")].receiver == RECEIVER_SELF


def test_typescript_literal_receiver_emits_no_call_ref(tmp_path: Path):
    requires_grammar("typescript")
    result = _extract(
        tmp_path,
        "web/r.ts",
        "export function r(xs: string[]) {\n"
        "  const a = ['x'].join('');\n"
        "  const b = 'abc'.concat('d');\n"
        "  return xs.join(',') + a + b;\n"
        "}\n",
    )
    calls = [r.target_name for r in result.refs if r.ref_type == "call"]
    # Only the call on a variable stays; the literal ones are builtin methods.
    assert calls == ["join"], calls


@pytest.mark.asyncio
async def test_typescript_member_calls_need_evidence_of_the_module(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "web/util.ts": "export function json(x: string) {\n  return x;\n}\n",
            "web/fetcher.ts": (
                "export async function load(res: Response) {\n"
                "  return res.json();\n"
                "}\n"
                "export function keys(o: object) {\n"
                "  return Object.keys(o).length;\n"
                "}\n"
            ),
            "web/named.ts": (
                "import { json } from './util';\n"
                "export function viaName() {\n"
                "  return json('a');\n"
                "}\n"
            ),
            "web/namespace.ts": (
                "import * as util from './util';\n"
                "export function viaNamespace() {\n"
                "  return util.json('a');\n"
                "}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})
        callers = await _callers(c, "web.util.json")
    assert callers == {"web.named.viaName", "web.namespace.viaNamespace"}, callers


@pytest.mark.asyncio
async def test_the_strongest_evidence_sets_the_edge_weight(workspace: Path):
    """A caller that reaches one method both through `self` and through an
    object of unknown type keeps the confirmed weight, in either order."""
    _write(
        workspace,
        {
            "pkg/__init__.py": "",
            "pkg/box.py": (
                "class Box:\n"
                "    def open(self):\n"
                "        return 1\n"
                "\n"
                "    def run(self, other):\n"
                "        return self.open() + other.open()\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(workspace)})
        confirmed = await _callers(c, "pkg.box.Box.open", min_weight=0.7)
    assert confirmed == {"pkg.box.Box.run"}, confirmed
