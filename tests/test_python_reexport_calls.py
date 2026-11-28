"""A call through a Python re-export chain reaches the one defining symbol.

`from pkg.forms import f`, `forms.f()`, `formsets.f()` and an aliased
`from pkg.forms.formsets import f as g` all name a function that lives in
`pkg/forms/rules.py`; `pkg/forms/__init__.py` star-imports `formsets`, which
re-exports it explicitly. A second project function of the same name must not
be credited with those calls.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.py_modules import PyModules
from livespec_mcp.server import mcp

FILES = {
    "pkg/__init__.py": "",
    "pkg/forms/__init__.py": "from pkg.forms.formsets import *\nfrom pkg.forms.widgets import *\n",
    "pkg/forms/rules.py": (
        '__all__ = ("all_valid",)\n\n'
        "def all_valid(fs):\n    return all([f.ok for f in fs])\n\n"
        "def hidden(fs):\n    return 1\n"
    ),
    "pkg/forms/formsets.py": (
        "from .rules import all_valid\n"
        "from .rules import hidden as renamed\n\n"
        '__all__ = ["all_valid", "formset_factory"]\n\n'
        "def formset_factory():\n    return 1\n"
    ),
    "pkg/forms/widgets.py": "class Media:\n    pass\n",
    "pkg/other/__init__.py": "def all_valid(x):\n    return True\n",
    "pkg/use.py": (
        "from pkg import forms\n"
        "from pkg.forms import all_valid\n"
        "from pkg.forms import formsets\n"
        "from pkg.forms.formsets import all_valid as av2\n"
        "from pkg.forms.formsets import renamed as rn\n\n"
        "def a(fs):\n    return all_valid(fs)\n\n"
        "def b(fs):\n    return forms.all_valid(fs)\n\n"
        "def c(fs):\n    return formsets.all_valid(fs)\n\n"
        "def d(fs):\n    return av2(fs)\n\n"
        "def e(fs):\n    return rn(fs)\n"
    ),
}


def _write(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


async def _callers(c, qname):
    r = (await c.call_tool("who_calls", {"qname": qname})).data
    return sorted(x["qualified_name"] for x in r["callers"])


@pytest.mark.asyncio
async def test_calls_through_reexports_reach_the_defining_symbol(workspace):
    _write(workspace, FILES)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        assert await _callers(c, "pkg.forms.rules.all_valid") == [
            "pkg.use.a",
            "pkg.use.b",
            "pkg.use.c",
            "pkg.use.d",
        ]
        # `from .rules import hidden as renamed` then `... import renamed as rn`
        assert await _callers(c, "pkg.forms.rules.hidden") == ["pkg.use.e"]
        # The same-named function elsewhere is not credited with any of them.
        assert await _callers(c, "pkg.other.__init__.all_valid") == []


def _py_modules(files):
    paths = {i: p for i, p in enumerate(files)}
    by_id = {i: files[p] for i, p in paths.items()}
    return PyModules(paths, by_id.get), paths


def test_star_import_without_all_exports_public_names_only():
    files = {
        "p/__init__.py": "from p.m import *\n",
        "p/m.py": "def pub():\n    pass\n\ndef _priv():\n    pass\n",
    }
    mods, paths = _py_modules(files)
    mid = next(i for i, p in paths.items() if p == "p/m.py")
    imp = next(i for i, p in paths.items() if p == "p/__init__.py")
    cands = lambda n: [(100 + mid, f"p.m.{n}", mid)]  # noqa: E731
    assert mods.lookup_type("p", "pub", imp, cands) is not None
    assert mods.lookup_type("p", "_priv", imp, cands) is None


def test_star_import_cycle_terminates():
    files = {
        "p/__init__.py": "from p.a import *\n",
        "p/a.py": "from p.b import *\n",
        "p/b.py": "from p.a import *\n",
    }
    mods, paths = _py_modules(files)
    imp = next(i for i, p in paths.items() if p == "p/__init__.py")
    assert mods.lookup_type("p", "ghost", imp, lambda n: []) is None
