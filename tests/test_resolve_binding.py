"""Edge binding by span, enum/annotation symbols, TS barrels and aliases, Java
method references and nested imports.

Each group drives extractor -> `symbol_ref` -> `_resolve_refs` -> `symbol_edge`
through FastMCP. The negative cases (an alias that shadows another import, a
barrel cycle, an unexported name, an unresolved class) matter as much as the
positives: a wrong edge keeps a symbol out of `find_dead_code`.
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


def _edges(workspace: Path, *types: str) -> set[tuple[str, int, str, int, str]]:
    """(src qname, src start line, dst qname, dst start line, edge type)."""
    st = get_state(str(workspace))
    return {
        tuple(r)
        for r in st.conn.execute(
            """SELECT a.qualified_name, a.start_line, b.qualified_name, b.start_line, e.edge_type
               FROM symbol_edge e JOIN symbol a ON a.id = e.src_symbol_id
               JOIN symbol b ON b.id = e.dst_symbol_id"""
        )
        if not types or r[4] in types
    }


def _pairs(workspace: Path, *types: str) -> set[tuple[str, str, str]]:
    return {(s, d, t) for s, _sl, d, _dl, t in _edges(workspace, *types)}


async def _index(c: Client) -> None:
    await c.call_tool("index_project", {})


# --------------------------------------------------------------------------
# 3. edges bind to the symbol whose span contains the reference
# --------------------------------------------------------------------------

JAVA_OVERLOADS = {
    "p/Strategy.java": "package p;\npublic interface Strategy {}\n",
    "p/Marker.java": "package p;\npublic @interface Marker {}\n",
    "p/Other.java": "package p;\npublic class Other {}\n",
    "p/User.java": (
        "package p;\n\n"
        "public class User {\n"
        "  public void put(Strategy s) {}\n"
        "  public void put(Marker m) {}\n"
        "  public void put(Other o) {}\n"
        "}\n"
    ),
}


@pytest.mark.asyncio
async def test_type_use_binds_to_the_overload_that_contains_it(workspace: Path):
    requires_grammar("java")
    _write(workspace, JAVA_OVERLOADS)
    async with Client(mcp) as c:
        await _index(c)
    refs = {
        (sl, d) for s, sl, d, _dl, t in _edges(workspace, "references") if s == "p.User.User.put"
    }
    assert refs == {(4, "p.Strategy.Strategy"), (5, "p.Marker.Marker"), (6, "p.Other.Other")}


@pytest.mark.asyncio
async def test_call_in_an_anonymous_class_binds_to_that_method(workspace: Path):
    requires_grammar("java")
    _write(
        workspace,
        {
            "p/Helper.java": "package p;\npublic class Helper {\n  public static void a() {}\n"
            "  public static void b() {}\n}\n",
            "p/Host.java": (
                "package p;\n\n"
                "public class Host {\n"
                "  Runnable one() {\n"
                "    return new Runnable() { public void run() { Helper.a(); } };\n"
                "  }\n"
                "  Runnable two() {\n"
                "    return new Runnable() { public void run() { Helper.b(); } };\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    calls = {(sl, d) for s, sl, d, _dl, t in _edges(workspace, "calls") if s.endswith(".run")}
    assert (5, "p.Helper.Helper.a") in calls
    assert (8, "p.Helper.Helper.b") in calls
    assert (5, "p.Helper.Helper.b") not in calls and (8, "p.Helper.Helper.a") not in calls


@pytest.mark.asyncio
async def test_python_function_defined_in_both_branches_binds_by_line(workspace: Path):
    _write(
        workspace,
        {
            "m.py": (
                "class K:\n    pass\n\nclass J:\n    pass\n\n"
                "import sys\n"
                "if sys.version_info >= (3, 9):\n"
                "    def shim(a: K):\n        return a\n"
                "else:\n"
                "    def shim(a: J):\n        return a\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    by_line = {(sl, d) for s, sl, d, _dl, t in _edges(workspace, "references") if s == "m.shim"}
    assert by_line == {(9, "m.K"), (12, "m.J")}


@pytest.mark.asyncio
async def test_reference_to_a_name_defined_twice_gets_one_target_documented(workspace: Path):
    """Overloads: the FIRST one; a class defined in both branches: the one in
    scope at the reference line."""
    requires_grammar("java")
    _write(
        workspace,
        {
            "p/Use.java": (
                "package p;\n\n"
                "public class Use {\n"
                '  void go() { f(1); f("x"); }\n'
                "  void f(int a) {}\n"
                "  void f(String a) {}\n"
                "}\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    targets = [(dl, d) for s, _sl, d, dl, t in _edges(workspace, "calls") if s == "p.Use.Use.go"]
    assert targets == [(5, "p.Use.Use.f")]  # first overload, once


# --------------------------------------------------------------------------
# 4. enums, annotation types, TS aliases are symbols
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_java_enum_and_annotation_type_are_symbols_with_edges(workspace: Path):
    requires_grammar("java")
    _write(
        workspace,
        {
            "p/Strategy.java": "package p;\npublic interface Strategy {\n  String name();\n}\n",
            "p/Marker.java": "package p;\npublic @interface Marker {}\n",
            "p/Policy.java": (
                "package p;\npublic enum Policy implements Strategy {\n  A, B;\n"
                '  public String name2() { return "x"; }\n}\n'
            ),
            "p/User.java": (
                "package p;\npublic class User {\n"
                "  public Policy pick(Policy p) { return p; }\n"
                "  public void tag(Marker m) {}\n}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await _index(c)
        dead = (
            await c.call_tool(
                "find_dead_code",
                {"include_non_python": True, "include_public": True, "limit": 500},
            )
        ).data
    st = get_state(str(workspace))
    kinds = {r[0]: r[1] for r in st.conn.execute("SELECT qualified_name, kind FROM symbol")}
    assert kinds["p.Policy.Policy"] == "class"
    assert kinds["p.Marker.Marker"] == "class"
    assert kinds["p.Policy.Policy.name2"] == "method"
    pairs = _pairs(workspace, "inherits", "references")
    assert ("p.Policy.Policy", "p.Strategy.Strategy", "inherits") in pairs
    assert ("p.User.User.pick", "p.Policy.Policy", "references") in pairs
    assert ("p.User.User.tag", "p.Marker.Marker", "references") in pairs
    assert "p.Policy.Policy" not in {d["qualified_name"] for d in dead["dead_symbols"]}


@pytest.mark.asyncio
async def test_typescript_enum_and_plain_alias_are_symbols(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "a.ts": "export enum Role { Admin, User }\nexport type Id = string | number;\n",
            "b.ts": (
                "import { Role, Id } from './a';\nexport function f(r: Role, i: Id): void {}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    st = get_state(str(workspace))
    kinds = dict(st.conn.execute("SELECT qualified_name, kind FROM symbol").fetchall())
    assert kinds["a.Role"] == "class" and kinds["a.Id"] == "type_alias"
    pairs = _pairs(workspace, "references")
    assert ("b.f", "a.Role", "references") in pairs
    assert ("b.f", "a.Id", "references") in pairs


# --------------------------------------------------------------------------
# 5. TypeScript barrels and aliases
# --------------------------------------------------------------------------

BARREL = {
    "lib/index.ts": "export * from './interfaces';\nexport * from './decorators';\n",
    "lib/interfaces/index.ts": (
        "export * from './pipe.interface';\nexport { Hidden as Shown } from './hidden';\n"
    ),
    "lib/interfaces/pipe.interface.ts": (
        "export interface PipeTransform { transform(v: number): number }\n"
        "export function makePipe(): PipeTransform { return { transform: (v) => v }; }\n"
    ),
    "lib/interfaces/hidden.ts": "export interface Hidden { h: number }\n",
    "lib/interfaces/unexported.ts": "export interface Unexported { u: number }\n",
    "lib/decorators/index.ts": "export * from './param';\n",
    "lib/decorators/param.ts": (
        "import { PipeTransform, makePipe, Shown } from '../index';\n"
        "export function a(p: PipeTransform): void {}\n"
        "export function b(): void { makePipe(); }\n"
        "export function c(s: Shown): void {}\n"
    ),
}


@pytest.mark.asyncio
async def test_two_level_barrel_reaches_the_defining_symbol(workspace: Path):
    requires_grammar("typescript")
    _write(workspace, BARREL)
    async with Client(mcp) as c:
        await _index(c)
    pairs = _pairs(workspace, "references", "calls")
    pipe = "lib.interfaces.pipe.interface.PipeTransform"
    assert ("lib.decorators.param.a", pipe, "references") in pairs
    assert ("lib.decorators.param.b", "lib.interfaces.pipe.interface.makePipe", "calls") in pairs
    # `export { Hidden as Shown }`: the import is `Shown`, the symbol `Hidden`
    assert ("lib.decorators.param.c", "lib.interfaces.hidden.Hidden", "references") in pairs
    # a file under the barrel's directory that the barrel never exports
    assert not any(d.endswith("Unexported") for _, d, _ in pairs)


@pytest.mark.asyncio
async def test_barrel_cycle_terminates_without_an_edge(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "a/index.ts": "export * from '../b';\n",
            "b/index.ts": "export * from '../a';\n",
            "c.ts": "import { Ghost } from './a';\nexport function f(g: Ghost): void {}\n",
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    assert not any(s == "c.f" for s, _, _ in _pairs(workspace, "references"))


def test_barrel_depth_is_bounded():
    from livespec_mcp.domain.ts_modules import MAX_BARREL_DEPTH, TsModules

    paths = {0: "n0/index.ts"}
    sources = {0: "export interface Deep { d: number }\n"}
    for i in range(1, 9):
        paths[i] = f"n{i}/index.ts"
        sources[i] = f"export * from '../n{i - 1}';\n"
    symbols = [(100, "n0.index.Deep", 0)]
    mods = TsModules(paths, sources.get)
    assert MAX_BARREL_DEPTH == 5

    def of(name: str):
        return symbols if name == "Deep" else []

    assert mods.lookup("n5", "Deep", of) == (symbols[0], True)  # five hops
    assert mods.lookup("n6", "Deep", of) is None  # six hops: past the bound
    assert mods.lookup("n0", "Deep", of) == (symbols[0], False)  # defined there


@pytest.mark.asyncio
async def test_aliased_import_binds_the_original_name(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "types.ts": "export interface Widget { id: number }\nexport function mk(): Widget { return { id: 1 }; }\n",
            "other.ts": "export interface W { other: boolean }\n",
            "use.ts": (
                "import { Widget as W, mk as make } from './types';\n"
                "export function f(w: W): void { make(); }\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    pairs = _pairs(workspace, "references", "calls")
    assert ("use.f", "types.Widget", "references") in pairs
    assert ("use.f", "types.mk", "calls") in pairs
    # the alias `W` happens to be the name of ANOTHER project interface: no edge
    assert not any(d == "other.W" for _, d, _ in pairs)


@pytest.mark.asyncio
async def test_alias_shadows_a_different_import(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "a.ts": "export interface Foo { a: number }\n",
            "b.ts": "export interface Bar { b: number }\nexport interface Foo { b: string }\n",
            "use.ts": (
                "import { Foo } from './a';\n"
                "import { Bar as Baz } from './b';\n"
                "export function f(x: Foo, y: Baz): void {}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    pairs = _pairs(workspace, "references")
    assert ("use.f", "a.Foo", "references") in pairs
    assert ("use.f", "b.Bar", "references") in pairs
    assert ("use.f", "b.Foo", "references") not in pairs


# --------------------------------------------------------------------------
# 6. Java method references and nested types imported through the outer class
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_java_method_reference_and_nested_import(workspace: Path):
    requires_grammar("java")
    _write(
        workspace,
        {
            "p/NamedEntity.java": (
                'package p;\npublic class NamedEntity {\n  public String getName() { return ""; }\n}\n'
            ),
            "p/Outer.java": (
                "package p;\npublic class Outer {\n  public enum Inner { A }\n"
                "  public static class Deep {}\n}\n"
            ),
            "q/Use.java": (
                "package q;\n"
                "import p.NamedEntity;\nimport p.Outer;\nimport p.Outer.Inner;\n"
                "import java.util.List;\n"
                "public class Use {\n"
                "  void go(List<NamedEntity> xs) { xs.forEach(NamedEntity::getName); }\n"
                "  Inner one;\n"
                "  Outer.Deep two;\n"
                "  void lib(List<String> s) { s.forEach(String::length); }\n"
                "}\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    pairs = _pairs(workspace, "calls", "references")
    assert ("q.Use.Use.go", "p.NamedEntity.NamedEntity.getName", "calls") in pairs
    assert not any(s == "q.Use.Use.lib" and t == "calls" for s, _, t in pairs)
    assert ("q.Use.Use", "p.Outer.Outer.Inner", "references") in pairs
    assert ("q.Use.Use", "p.Outer.Outer.Deep", "references") in pairs


@pytest.mark.asyncio
async def test_overload_delegating_to_a_sibling_overload_reaches_it(workspace: Path):
    requires_grammar("java")
    _write(
        workspace,
        {
            "p/Parser.java": (
                "package p;\n\n"
                "public class Parser {\n"
                "  public static int parse(String s) { return parse(s.length()); }\n"
                "  public static int parse(int n) { return n; }\n"
                "}\n"
            )
        },
    )
    async with Client(mcp) as c:
        await _index(c)
    assert {(sl, dl) for _s, sl, _d, dl, t in _edges(workspace, "calls")} == {(4, 5)}
