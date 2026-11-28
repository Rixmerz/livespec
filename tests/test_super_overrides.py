"""`super()` resolution, override reporting and instance-class dispatch.

`super().m()` used to resolve to any project method NAMED `super` (Django's
`BlockNode.super`) and to nothing on the parent. Overrides were invisible
(`analyze_impact` on a base method listed callers only). Drive the whole
pipeline through FastMCP; the negative cases matter as much as the positives.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import get_state

from .conftest import requires_grammar


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _calls(workspace: Path) -> set[tuple[str, str]]:
    st = get_state(str(workspace))
    return {
        (r[0], r[1])
        for r in st.conn.execute(
            """SELECT a.qualified_name, b.qualified_name FROM symbol_edge e
               JOIN symbol a ON a.id = e.src_symbol_id
               JOIN symbol b ON b.id = e.dst_symbol_id WHERE e.edge_type = 'calls'"""
        )
    }


async def _index(c: Client) -> None:
    await c.call_tool("index_project", {})


PY_SUPER = {
    "m.py": (
        "class Base:\n"
        "    def collect(self, x):\n        return x\n\n"
        "class Child(Base):\n"
        "    def collect(self, x):\n        return super().collect(x)\n\n"
        "class Block:\n"
        "    def super(self):\n        return 1\n\n"
        "    def render(self):\n        return self.super()\n\n"
        "class Mid(Base):\n    pass\n\n"
        "class Leaf(Mid):\n"
        "    def collect(self, x):\n        return super(Leaf, self).collect(x)\n\n"
        "class Lonely:\n"
        "    def run(self):\n        return super().run()\n\n"
        "class Other:\n"
        "    def collect(self, x):\n        return x\n\n"
        "def uses(y):\n    return y.super()\n"
    )
}


@pytest.mark.asyncio
async def test_super_call_reaches_the_parent_method_not_a_method_named_super(workspace: Path):
    _write(workspace, PY_SUPER)
    async with Client(mcp) as c:
        await _index(c)
    calls = _calls(workspace)
    assert ("m.Child.collect", "m.Base.collect") in calls
    # super(Leaf, self).collect: Leaf -> Mid (no collect) -> Base
    assert ("m.Leaf.collect", "m.Base.collect") in calls
    # the builtin `super` is never a call to the project method `Block.super`
    assert not any(
        s in ("m.Child.collect", "m.Leaf.collect", "m.Lonely.run") and d == "m.Block.super"
        for s, d in calls
    )
    # an unrelated class with the same method name is not a parent
    assert ("m.Child.collect", "m.Other.collect") not in calls
    # a base with no indexed definition: no edge at all
    assert not any(s == "m.Lonely.run" for s, _ in calls)
    # a real attribute call `.super()` still resolves
    assert ("m.Block.render", "m.Block.super") in calls
    assert ("m.uses", "m.Block.super") in calls


@pytest.mark.asyncio
async def test_super_naming_another_class_makes_no_edge(workspace: Path):
    _write(
        workspace,
        {
            "m.py": (
                "class A:\n    def f(self):\n        pass\n\n"
                "class B(A):\n    def f(self):\n        pass\n\n"
                "class C(B):\n    def f(self):\n        return super(B, self).f()\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    assert not any(s == "m.C.f" for s, _ in _calls(workspace))


@pytest.mark.asyncio
async def test_overrides_in_analyze_impact_and_quick_orient(workspace: Path):
    _write(
        workspace,
        {
            "m.py": PY_SUPER["m.py"]
            + "\nclass Grand(Child):\n    def collect(self, x):\n        return x\n"
        },
    )
    async with Client(mcp) as c:
        await _index(c)
        impact = (
            await c.call_tool(
                "analyze_impact", {"target_type": "symbol", "target": "m.Base.collect"}
            )
        ).data
        orient = (await c.call_tool("quick_orient", {"qname": "m.Base.collect"})).data
        orient_child = (await c.call_tool("quick_orient", {"qname": "m.Child.collect"})).data
        plain = (await c.call_tool("quick_orient", {"qname": "m.Other.collect"})).data
        plain_impact = (
            await c.call_tool(
                "analyze_impact", {"target_type": "symbol", "target": "m.Other.collect"}
            )
        ).data
        summary = (
            await c.call_tool(
                "analyze_impact",
                {"target_type": "symbol", "target": "m.Base.collect", "summary_only": True},
            )
        ).data

    over = {o["qualified_name"]: o for o in impact["overriding_methods"]}
    assert set(over) == {"m.Child.collect", "m.Leaf.collect", "m.Grand.collect"}
    assert "m.Other.collect" not in over
    assert over["m.Child.collect"]["calls_super"] is True
    assert over["m.Grand.collect"]["calls_super"] is False
    assert over["m.Child.collect"]["start_line"] == 6 and over["m.Child.collect"]["file_path"]
    assert impact["counts"]["overriding_methods"] == 3
    assert summary["counts"]["overriding_methods"] == 3
    assert orient["overridden_by"] == 3 and "overrides" not in orient
    assert orient_child["overrides"]["qualified_name"] == "m.Base.collect"
    assert orient_child["overridden_by"] == 1
    # no overrides: payloads carry none of the new keys
    assert "overridden_by" not in plain and "overrides" not in plain
    assert "overriding_methods" not in plain_impact and "overrides" not in plain_impact
    assert "overriding_methods" not in plain_impact["counts"]


@pytest.mark.asyncio
async def test_overriding_methods_are_bounded_with_exact_count(workspace: Path):
    body = "class Base:\n    def f(self):\n        pass\n\n"
    for i in range(5):
        body += f"class D{i}(Base):\n    def f(self):\n        pass\n\n"
    _write(workspace, {"m.py": body})
    async with Client(mcp) as c:
        await _index(c)
        impact = (
            await c.call_tool(
                "analyze_impact", {"target_type": "symbol", "target": "m.Base.f", "limit": 2}
            )
        ).data
    assert len(impact["overriding_methods"]) == 2
    assert impact["counts"]["overriding_methods"] == 5


@pytest.mark.asyncio
async def test_call_on_instance_of_known_class_reaches_the_override(workspace: Path):
    _write(
        workspace,
        {
            "m.py": (
                "class Base:\n    def collect(self, x):\n        return x\n\n"
                "class Child(Base):\n    def collect(self, x):\n        return x\n\n"
                "class Sub(Base):\n    pass\n\n"
                "def go():\n    c = Child()\n    return c.collect(1)\n\n"
                "def go_sub():\n    s = Sub()\n    return s.collect(1)\n\n"
                "def rebound(flag):\n    c = Child()\n    c = make()\n    return c.collect(1)\n\n"
                "def annotated(k: Child):\n    return k.collect(1)\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    calls = _calls(workspace)
    assert ("m.go", "m.Child.collect") in calls
    assert ("m.go", "m.Base.collect") not in calls
    # Sub does not override: inherited definition
    assert ("m.go_sub", "m.Base.collect") in calls
    assert ("m.go_sub", "m.Child.collect") not in calls
    assert ("m.annotated", "m.Child.collect") in calls
    # a name bound twice is no evidence: the name-based path decides
    assert ("m.rebound", "m.Child.collect") in calls  # same-file fan-out, as before


@pytest.mark.asyncio
async def test_typescript_super_and_overrides(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "a.ts": (
                "export class Base {\n  run(): number { return 1; }\n  constructor() {}\n}\n"
                "export class Child extends Base {\n"
                "  constructor() { super(); }\n"
                "  run(): number { return super.run() + 1; }\n}\n"
                "export function super_(): void {}\n"
                "export function x() {}\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
        impact = (
            await c.call_tool("analyze_impact", {"target_type": "symbol", "target": "a.Base.run"})
        ).data
    calls = _calls(workspace)
    assert ("a.Child.run", "a.Base.run") in calls
    assert ("a.Child.constructor", "a.Base.constructor") in calls
    assert [o["qualified_name"] for o in impact["overriding_methods"]] == ["a.Child.run"]


@pytest.mark.asyncio
async def test_java_super_call_and_overrides(workspace: Path):
    requires_grammar("java")
    _write(
        workspace,
        {
            "p/Base.java": "package p;\npublic class Base {\n  public int run() { return 1; }\n}\n",
            "p/Child.java": (
                "package p;\npublic class Child extends Base {\n"
                "  @Override public int run() { return super.run() + 1; }\n}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await _index(c)
        impact = (
            await c.call_tool(
                "analyze_impact", {"target_type": "symbol", "target": "p.Base.Base.run"}
            )
        ).data
    assert any(d == "p.Base.Base.run" for s, d in _calls(workspace) if s.endswith("Child.run"))
    assert len(impact["overriding_methods"]) == 1
    assert impact["overriding_methods"][0]["calls_super"] is True


@pytest.mark.asyncio
async def test_evident_class_with_unindexed_base_keeps_name_based_edges(workspace: Path):
    _write(
        workspace,
        {
            "m.py": (
                "from lib import Form\n\n"
                "class Mixin:\n    def as_p(self):\n        return 1\n\n"
                "class Local(Form):\n    pass\n\n"
                "class Closed:\n    pass\n\n"
                "def open_base():\n    p = Local()\n    return p.as_p()\n\n"
                "def closed_base():\n    q = Closed()\n    return q.as_p()\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    calls = _calls(workspace)
    # `Form` is a library class: `as_p` may live there, the name decides (as before)
    assert ("m.open_base", "m.Mixin.as_p") in calls
    # every ancestor of Closed is indexed and none defines it: no edge
    assert ("m.closed_base", "m.Mixin.as_p") not in calls


@pytest.mark.asyncio
async def test_unannotated_parameter_is_never_evident_even_when_rebound(workspace: Path):
    """Django's `setup_databases(time_keeper=None)`: `if time_keeper is None:
    time_keeper = NullTimeKeeper()` does not make the caller's value a
    NullTimeKeeper, so the edge to the base-class method must survive."""
    _write(
        workspace,
        {
            "m.py": (
                "class TimeKeeper:\n    def timed(self):\n        return 1\n\n"
                "class NullTimeKeeper(TimeKeeper):\n    def timed(self):\n        return 0\n\n"
                "def setup(tk=None):\n"
                "    if tk is None:\n        tk = NullTimeKeeper()\n"
                "    return tk.timed()\n\n"
                "def setup_always(tk):\n    tk = NullTimeKeeper()\n    return tk.timed()\n\n"
                "def setup_annotated(tk: TimeKeeper):\n    return tk.timed()\n\n"
                "def local():\n    tk = NullTimeKeeper()\n    return tk.timed()\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    calls = _calls(workspace)
    for fn in ("m.setup", "m.setup_always"):
        assert (fn, "m.TimeKeeper.timed") in calls
        assert (fn, "m.NullTimeKeeper.timed") in calls
    # an annotated parameter still resolves by its annotation (with overrides)
    assert ("m.setup_annotated", "m.TimeKeeper.timed") in calls
    # a plain local `x = Cls()` is still evident: only Cls's method, not the base's
    assert ("m.local", "m.NullTimeKeeper.timed") in calls
    assert ("m.local", "m.TimeKeeper.timed") not in calls


@pytest.mark.asyncio
async def test_variable_assigned_two_classes_is_not_evident(workspace: Path):
    _write(
        workspace,
        {
            "m.py": (
                "class A:\n    def m(self):\n        return 1\n\n"
                "class B:\n    def m(self):\n        return 2\n\n"
                "def pick(flag):\n"
                "    if flag:\n        c = A()\n    else:\n        c = B()\n"
                "    return c.m()\n\n"
                "def only_a():\n    c = A()\n    return c.m()\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    calls = _calls(workspace)
    assert ("m.pick", "m.A.m") in calls and ("m.pick", "m.B.m") in calls
    assert ("m.only_a", "m.A.m") in calls and ("m.only_a", "m.B.m") not in calls
