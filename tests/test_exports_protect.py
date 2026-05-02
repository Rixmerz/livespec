"""v0.10 P1: __init__.py re-exports + __all__ protect symbols from
find_dead_code (#out-of-tree-public-API).

Library codebases like Django export classes via:
- `from .hashers import Argon2PasswordHasher` in `__init__.py`
- `__all__ = ['Argon2PasswordHasher', ...]`

Their callers live in USER code (not the indexed library), so the
in-tree call graph reports zero callers. Without this fix every
public class is flagged dead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_init_reexport_protects_class(workspace):
    """`from .impl import PublicClass` in __init__.py keeps PublicClass alive."""
    pkg = workspace / "mylib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from .impl import PublicClass\n"
    )
    (pkg / "impl.py").write_text(
        "class PublicClass:\n"
        "    def do_thing(self):\n"
        "        return 1\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert "mylib.impl.PublicClass" not in qnames, (
            f"re-exported class must be protected: {qnames}"
        )


@pytest.mark.asyncio
async def test_init_reexport_with_alias_protects(workspace):
    """`from .impl import Foo as Bar` protects Foo (original name)."""
    pkg = workspace / "mylib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from .impl import OriginalName as Renamed\n"
    )
    (pkg / "impl.py").write_text(
        "class OriginalName:\n"
        "    pass\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert "mylib.impl.OriginalName" not in qnames


@pytest.mark.asyncio
async def test_dunder_all_protects_listed_names(workspace):
    """`__all__ = ['Foo']` protects Foo from dead-code flagging."""
    pkg = workspace / "lib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "shapes.py").write_text(
        "__all__ = ['Square', 'Circle']\n"
        "\n"
        "class Square:\n"
        "    pass\n"
        "\n"
        "class Circle:\n"
        "    pass\n"
        "\n"
        "class _Internal:\n"  # not in __all__
        "    pass\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        # Public names protected
        assert "lib.shapes.Square" not in qnames
        assert "lib.shapes.Circle" not in qnames
        # _Internal not in __all__ — still flagged dead (no caller, no
        # protection). Must appear as dead.
        assert "lib.shapes._Internal" in qnames


@pytest.mark.asyncio
async def test_unimported_class_still_dead(workspace):
    """Counter-test: a class with no caller AND not in __init__/__all__
    is still flagged dead. The new protection must not over-fire."""
    pkg = workspace / "mylib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")  # empty, no re-exports
    (pkg / "impl.py").write_text(
        "class TrulyDead:\n"
        "    pass\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert "mylib.impl.TrulyDead" in qnames
