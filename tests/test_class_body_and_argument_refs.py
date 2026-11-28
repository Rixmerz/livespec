"""A class used from a class body, or handed to a call as an argument, is used.

Django: `input_formats = DateTimeFormatsIterator()` in a class body and
`warnings.warn(msg, MediaOrderConflictWarning)` inside a method left both
classes with no incoming edge, so `find_dead_code` listed them.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

SRC = (
    "import warnings\n\n\n"
    "class Formats:\n    pass\n\n\n"
    "class Warn(UserWarning):\n    pass\n\n\n"
    "class Truly:\n    pass\n\n\n"
    "class Field:\n    input_formats = Formats()\n\n"
    "    def media(self):\n"
    '        warnings.warn("x", Warn)\n'
    "        return 1\n"
)


@pytest.mark.asyncio
async def test_class_body_instantiation_and_class_argument_are_uses(workspace):
    (workspace / "mod.py").write_text(SRC)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        dead = (await c.call_tool("find_dead_code", {"include_public": True})).data
        names = {d["qualified_name"] for d in dead["dead_symbols"]}
        assert "mod.Truly" in names, names
        assert "mod.Formats" not in names, names
        assert "mod.Warn" not in names, names
        callers = (await c.call_tool("who_calls", {"qname": "mod.Formats"})).data["callers"]
        assert [x["qualified_name"] for x in callers] == ["mod.Field"]
