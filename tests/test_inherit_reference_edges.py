"""Native `inherits` / `references` edges (Python, TypeScript/TSX, Java).

Until now livespec's extractors produced `calls` and import edges only. A base
class used purely through inheritance, an interface used purely through
`implements`, and a type used purely in annotations had no inbound edge, so
`find_dead_code` listed them and `analyze_impact` on them showed nothing. The
edge types existed (`symbol_edge.edge_type` allowed them) but only a Graphify
ingest ever wrote them.

These tests drive the whole pipeline through FastMCP: extractor -> `symbol_ref`
-> `_resolve_refs` -> `symbol_edge` -> `find_dead_code` / `analyze_impact` /
`who_calls`. The negative cases matter as much as the positive ones: an edge to
a project class from a name that merely *looks* like it (a builtin, a library
type, an unimported name) would keep that class out of `find_dead_code` for no
reason, which is worse than the gap this closes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_ingest import EXTERNAL_ORIGIN
from livespec_mcp.domain.extractors import REF_INHERIT, REF_TYPE, extract
from livespec_mcp.server import mcp
from livespec_mcp.state import get_state

from .conftest import requires_grammar


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _edges(workspace: Path) -> set[tuple[str, str, str, str]]:
    """(src qname, dst qname, edge_type, origin) for every edge in the index."""
    st = get_state(str(workspace))
    return {
        (r[0], r[1], r[2], r[3])
        for r in st.conn.execute(
            """SELECT a.qualified_name, b.qualified_name, e.edge_type, e.origin
               FROM symbol_edge e
               JOIN symbol a ON a.id = e.src_symbol_id
               JOIN symbol b ON b.id = e.dst_symbol_id"""
        )
    }


def _typed(workspace: Path) -> set[tuple[str, str, str]]:
    return {(s, d, t) for s, d, t, _ in _edges(workspace) if t in ("inherits", "references")}


async def _dead(c: Client, **kw) -> set[str]:
    out = (
        await c.call_tool(
            "find_dead_code",
            {
                "include_non_python": True,
                "include_public": True,
                "include_infrastructure": True,
                "limit": 500,
                **kw,
            },
        )
    ).data
    return {d["qualified_name"] for d in out["dead_symbols"]}


async def _impact(c: Client, qname: str) -> dict:
    return (
        await c.call_tool(
            "analyze_impact", {"target_type": "symbol", "target": qname, "max_depth": 1}
        )
    ).data


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------

PY_FILES = {
    "pkg/__init__.py": "",
    "pkg/base.py": (
        "class Base:\n    pass\n\n"
        "class Mixin:\n    pass\n\n"
        "class Shape:\n    pass\n\n"
        "class Row:\n    pass\n\n"
        "class Boom(Exception):\n    pass\n\n"
        "class Orphan:\n    pass\n\n"
        "class Path:\n    pass\n"
    ),
    "pkg/child.py": (
        "from typing import Generic, TypeVar\n"
        "from pkg import base\n"
        "from pkg.base import Base, Mixin, Shape, Row, Boom\n\n"
        "T = TypeVar('T')\n\n"
        "class Child(Base, Mixin, metaclass=type):\n    pass\n\n"
        "class Box(Generic[T], base.Shape):\n    pass\n\n"
        "class Plain(object):\n    pass\n\n"
        "def total(rows: list[Row]) -> int:\n    return len(rows)\n\n"
        "def guard(x):\n"
        "    try:\n        return isinstance(x, Shape)\n"
        "    except Boom:\n        return False\n\n"
        "def uses_builtin(p: 'Path', n: int) -> str:\n    return str(n)\n"
    ),
}


def test_python_extractor_emits_typed_refs_for_bases_and_annotations(tmp_path: Path):
    _write(tmp_path, PY_FILES)
    _, res = extract(tmp_path / "pkg/child.py", PY_FILES["pkg/child.py"], tmp_path)
    by = {(r.src_qname, r.target_name, r.ref_type) for r in res.refs}
    assert ("pkg.child.Child", "Base", REF_INHERIT) in by
    assert ("pkg.child.Child", "Mixin", REF_INHERIT) in by
    assert ("pkg.child.Box", "Shape", REF_INHERIT) in by
    # object and the metaclass keyword are not bases.
    typed = [r for r in res.refs if r.ref_type in (REF_INHERIT, REF_TYPE)]
    assert not any(r.target_name in ("object", "type") for r in typed)
    assert ("pkg.child.total", "Row", REF_TYPE) in by
    assert ("pkg.child.guard", "Shape", REF_TYPE) in by
    assert ("pkg.child.guard", "Boom", REF_TYPE) in by
    # `int` / `str` / `list` are not imported or defined here: no ref at all.
    assert not any(r.target_name in ("int", "str", "list") for r in typed)


@pytest.mark.asyncio
async def test_python_base_class_used_only_by_inheritance_is_not_dead(workspace: Path):
    _write(workspace, PY_FILES)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        dead = await _dead(c)
        impact = await _impact(c, "pkg.base.Base")
        callers = (await c.call_tool("who_calls", {"qname": "pkg.base.Base"})).data

    assert "pkg.base.Base" not in dead
    assert "pkg.base.Mixin" not in dead
    assert "pkg.base.Shape" not in dead  # `Box(Generic[T], base.Shape)` + isinstance
    assert "pkg.base.Row" not in dead  # only a generic argument: list[Row]
    assert "pkg.base.Boom" not in dead  # only an `except` type
    assert "pkg.base.Orphan" in dead  # nothing names it: still reported

    edges = _typed(workspace)
    assert ("pkg.child.Child", "pkg.base.Base", "inherits") in edges
    assert ("pkg.child.Child", "pkg.base.Mixin", "inherits") in edges
    assert ("pkg.child.Box", "pkg.base.Shape", "inherits") in edges
    assert ("pkg.child.total", "pkg.base.Row", "references") in edges
    assert ("pkg.child.guard", "pkg.base.Shape", "references") in edges
    assert ("pkg.child.guard", "pkg.base.Boom", "references") in edges

    names = {r["qualified_name"]: r for r in impact["impacted_callers"]}
    assert names["pkg.child.Child"]["edge_type"] == "inherits"
    # who_calls keeps its default: a subclass is not a caller, but it is said.
    assert callers["count"] == 0
    assert callers["excluded_by_edge_type"] == {"inherits": 1}


@pytest.mark.asyncio
async def test_python_unresolved_and_builtin_names_create_no_edge(workspace: Path):
    _write(workspace, PY_FILES)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    edges = _typed(workspace)
    # `'Path'` in uses_builtin shares its name with a project class the file
    # never imported: not ours, no edge. `Plain(object)` inherits nothing.
    assert not any(d == "pkg.base.Path" for _, d, _ in edges)
    assert not any(s == "pkg.child.Plain" for s, _, _ in edges)
    assert not any(s == "pkg.child.uses_builtin" for s, _, _ in edges)


@pytest.mark.asyncio
async def test_python_field_and_return_and_variable_annotations(workspace: Path):
    _write(
        workspace,
        {
            "m.py": (
                "class Cfg:\n    pass\n\n"
                "class Out:\n    pass\n\n"
                "class Tmp:\n    pass\n\n"
                "class Holder:\n    cfg: Cfg\n\n"
                "def make() -> Out:\n    return Out.__new__(Out)\n\n"
                "def local():\n    x: Tmp = None\n    return x\n"
            )
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    edges = _typed(workspace)
    assert ("m.Holder", "m.Cfg", "references") in edges
    assert ("m.make", "m.Out", "references") in edges
    assert ("m.local", "m.Tmp", "references") in edges
    # the declaring class is never a target of its own methods
    assert not any(s == d for s, d, _ in edges)


# --------------------------------------------------------------------------
# TypeScript
# --------------------------------------------------------------------------

TS_FILES = {
    "src/types.ts": (
        "export interface Shape { area(): number }\n"
        "export interface Named { name: string }\n"
        "export class Base { id = 1 }\n"
        "export class Item { n = 0 }\n"
        "export type Id = { v: string };\n"
        "export class Kind { static A = 0 }\n"
        "export class Err extends Error {}\n"
        "export class Lonely { z = 0 }\n"
    ),
    "src/use.ts": (
        "import { Shape, Named, Base, Item, Id, Kind, Err } from './types';\n\n"
        "export class Square extends Base implements Shape, Named {\n"
        "  name = 'sq';\n"
        "  area(): number { return 4; }\n"
        "}\n\n"
        "export interface Labelled extends Named { label: string }\n\n"
        "export function take(items: Array<Item>, id: Id): Kind {\n"
        "  return Kind.A;\n"
        "}\n\n"
        "export function check(x: unknown) {\n"
        "  if (x instanceof Err) { return (x as Base).id; }\n"
        "  const k = { v: 1 } satisfies Record<string, number>;\n"
        "  return k;\n"
        "}\n\n"
        "export function generic<T extends Shape>(v: T, s: string, d: Date): T { return v; }\n"
    ),
}


@pytest.mark.asyncio
async def test_typescript_inheritance_and_type_position_edges(workspace: Path):
    requires_grammar("typescript")
    _write(workspace, TS_FILES)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        dead = await _dead(c)
        impact = await _impact(c, "src.types.Shape")
    edges = _typed(workspace)
    assert ("src.use.Square", "src.types.Base", "inherits") in edges
    assert ("src.use.Square", "src.types.Shape", "inherits") in edges
    assert ("src.use.Square", "src.types.Named", "inherits") in edges
    assert ("src.use.Labelled", "src.types.Named", "inherits") in edges
    assert ("src.use.take", "src.types.Item", "references") in edges  # generic argument
    assert ("src.use.take", "src.types.Id", "references") in edges  # alias, param
    assert ("src.use.take", "src.types.Kind", "references") in edges  # return type
    assert ("src.use.check", "src.types.Err", "references") in edges  # instanceof
    assert ("src.use.check", "src.types.Base", "references") in edges  # `as`
    assert ("src.use.generic", "src.types.Shape", "references") in edges  # type-param bound

    for used in ("Shape", "Named", "Base", "Item", "Id", "Kind", "Err"):
        assert f"src.types.{used}" not in dead, used
    assert "src.types.Lonely" in dead

    impacted = {r["qualified_name"]: r for r in impact["impacted_callers"]}
    assert impacted["src.use.Square"]["edge_type"] == "inherits"
    assert impacted["src.use.generic"]["edge_type"] == "references"


@pytest.mark.asyncio
async def test_typescript_builtin_and_type_variable_names_create_no_edge(workspace: Path):
    requires_grammar("typescript")
    _write(
        workspace,
        {
            "src/a.ts": "export class Date2 { x = 1 }\nexport class T { y = 1 }\n",
            "src/b.ts": (
                "export function f<T>(v: T, d: Date, r: Record<string, number>): T { return v; }\n"
            ),
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    # `T` here is the function's own type variable, not the class named T.
    assert not any(s == "src.b.f" for s, _, _ in _typed(workspace))


def test_tsx_extractor_emits_type_refs(tmp_path: Path):
    requires_grammar("tsx")
    src = (
        "import { Props } from './props';\n"
        "export function Card(p: Props) { return <div/>; }\n"
        "export class Panel extends Base {}\n"
    )
    _, res = extract(tmp_path / "card.tsx", src, tmp_path)
    got = {(r.src_qname, r.target_name, r.ref_type) for r in res.refs}
    assert ("card.Card", "Props", REF_TYPE) in got
    assert ("card.Panel", "Base", REF_INHERIT) in got


# --------------------------------------------------------------------------
# Java
# --------------------------------------------------------------------------

JAVA_FILES = {
    "src/main/java/com/acme/Animal.java": ("package com.acme;\npublic class Animal { }\n"),
    "src/main/java/com/acme/Walker.java": (
        "package com.acme;\npublic interface Walker { void walk(); }\n"
    ),
    "src/main/java/com/acme/Config.java": ("package com.acme;\npublic class Config { }\n"),
    "src/main/java/com/acme/Problem.java": (
        "package com.acme;\npublic class Problem extends Exception { }\n"
    ),
    "src/main/java/com/acme/Unused.java": ("package com.acme;\npublic class Unused { }\n"),
    "src/main/java/com/acme/Dog.java": (
        "package com.acme;\n"
        "import java.util.List;\n"
        "public class Dog extends Animal implements Walker {\n"
        "  private Config cfg;\n"
        "  public void walk() { }\n"
        "  public List<Config> all(String s) throws Problem {\n"
        "    Object o = s;\n"
        "    if (o instanceof Animal) { return null; }\n"
        "    return null;\n"
        "  }\n"
        "}\n"
    ),
}


@pytest.mark.asyncio
async def test_java_inheritance_and_type_position_edges(workspace: Path):
    requires_grammar("java")
    _write(workspace, JAVA_FILES)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        dead = await _dead(c)
        impact = await _impact(c, "src.main.java.com.acme.Walker.Walker")
    edges = {(s.rsplit(".", 1)[-1], d.rsplit(".", 1)[-1], t) for s, d, t in _typed(workspace)}
    assert ("Dog", "Animal", "inherits") in edges
    assert ("Dog", "Walker", "inherits") in edges
    assert ("Dog", "Config", "references") in edges  # field
    assert ("all", "Config", "references") in edges  # generic argument in return type
    assert ("all", "Problem", "references") in edges  # throws
    assert ("all", "Animal", "references") in edges  # instanceof
    # `List` / `String` / `Object` / `Exception` are not project symbols.
    assert not any(d in ("List", "String", "Object", "Exception") for _, d, _ in edges)

    simple = {d.rsplit(".", 1)[-1] for d in dead}
    for used in ("Animal", "Walker", "Config", "Problem"):
        assert used not in simple, used
    assert "Unused" in simple
    impacted = {r["qualified_name"].rsplit(".", 1)[-1]: r for r in impact["impacted_callers"]}
    assert impacted["Dog"]["edge_type"] == "inherits"


# --------------------------------------------------------------------------
# Partial re-index: refs from unchanged files survive, both directions
# --------------------------------------------------------------------------

REINDEX = {
    "p/__init__.py": "",
    "p/base.py": "class Base:\n    pass\n",
    "p/sub.py": "from p.base import Base\n\nclass Sub(Base):\n    pass\n",
}


@pytest.mark.asyncio
async def test_reindexing_only_the_subclass_file_keeps_the_edge(workspace: Path):
    _write(workspace, REINDEX)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        assert ("p.sub.Sub", "p.base.Base", "inherits") in _typed(workspace)
        (workspace / "p/sub.py").write_text(
            "from p.base import Base\n\nclass Sub(Base):\n    x = 1\n"
        )
        await c.call_tool("index_project", {})
        assert ("p.sub.Sub", "p.base.Base", "inherits") in _typed(workspace)
        assert "p.base.Base" not in await _dead(c)


@pytest.mark.asyncio
async def test_reindexing_only_the_base_file_keeps_the_edge(workspace: Path):
    _write(workspace, REINDEX)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        (workspace / "p/base.py").write_text("class Base:\n    y = 2\n")
        await c.call_tool("index_project", {})
        assert ("p.sub.Sub", "p.base.Base", "inherits") in _typed(workspace)
        assert "p.base.Base" not in await _dead(c)
        impact = await _impact(c, "p.base.Base")
    assert [r["qualified_name"] for r in impact["impacted_callers"]] == ["p.sub.Sub"]


@pytest.mark.asyncio
async def test_removing_the_subclass_makes_the_base_dead_again(workspace: Path):
    _write(workspace, REINDEX)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        (workspace / "p/sub.py").write_text("x = 1\n")
        await c.call_tool("index_project", {})
        assert "p.base.Base" in await _dead(c)


# --------------------------------------------------------------------------
# Defaults of the consumers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_dead_code_says_how_many_were_kept_alive_by_type_edges(workspace: Path):
    _write(workspace, PY_FILES)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "find_dead_code",
                {"include_public": True, "include_infrastructure": True, "summary_only": True},
            )
        ).data
    kept = out["kept_alive_by_edge_type"]
    assert kept["inherits"] >= 2 and kept["references"] >= 2
    assert kept["symbols"] >= 4


@pytest.mark.asyncio
async def test_a_call_plus_an_annotation_to_one_class_stays_a_caller(workspace: Path):
    """The graph keeps one edge per (src, dst): the `calls` row must win over a
    `references` row for the same pair whatever order they load in."""
    _write(
        workspace,
        {
            "m.py": (
                "class Thing:\n    pass\n\ndef build(seed: Thing) -> Thing:\n    return Thing()\n"
            )
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        callers = (await c.call_tool("who_calls", {"qname": "m.Thing"})).data
    assert [r["qualified_name"] for r in callers["callers"]] == ["m.build"]


# --------------------------------------------------------------------------
# Ownership against an ingested row for the same pair
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_graphify_row_for_a_native_pair_ends_up_livespec_owned(workspace: Path):
    _write(workspace, REINDEX)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        st = get_state(str(workspace))
        syms = {
            r["qualified_name"]: dict(r)
            for r in st.conn.execute(
                "SELECT s.qualified_name, s.name, s.start_line, f.path AS file_path "
                "FROM symbol s JOIN file f ON f.id = s.file_id"
            )
        }
        nodes = [
            {
                "id": f"n{i}",
                "label": syms[q]["name"],
                "source_file": syms[q]["file_path"],
                "source_location": f"L{syms[q]['start_line']}",
                "_callable": True,
                "_origin": "ast",
                "community": 0,
            }
            for i, q in enumerate(["p.sub.Sub", "p.base.Base"])
        ]
        graph = workspace / "g.json"
        graph.write_text(
            json.dumps(
                {
                    "directed": True,
                    "nodes": nodes,
                    "links": [
                        {
                            "source": "n0",
                            "target": "n1",
                            "relation": "inherits",
                            "confidence": "EXTRACTED",
                            "_origin": "ast",
                        }
                    ],
                }
            )
        )
        await c.call_tool("ingest_external_graph", {"graph_path": str(graph), "dry_run": False})

    rows = [e for e in _edges(workspace) if e[:3] == ("p.sub.Sub", "p.base.Base", "inherits")]
    assert rows == [("p.sub.Sub", "p.base.Base", "inherits", "livespec")]

    # And the other order: a row an ingest owns is reclaimed when the resolver
    # derives the same pair again (the reclaim rule, through the real resolver).
    from livespec_mcp.domain.indexer import _resolve_refs

    st = get_state(str(workspace))
    st.conn.execute(
        "UPDATE symbol_edge SET origin=? WHERE edge_type='inherits'", (EXTERNAL_ORIGIN,)
    )
    _resolve_refs(st.conn, project_id=st.project_id)
    rows = [e for e in _edges(workspace) if e[:3] == ("p.sub.Sub", "p.base.Base", "inherits")]
    assert rows == [("p.sub.Sub", "p.base.Base", "inherits", "livespec")]


# --------------------------------------------------------------------------
# Migration 30: an existing index re-extracts once
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_30_queues_one_reextract_on_a_populated_index(workspace: Path):
    from livespec_mcp.storage.db import MIGRATIONS

    assert MIGRATIONS[-1][:2] == (30, "inherit_reference_refs")
    _write(workspace, REINDEX)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        st = get_state(str(workspace))
        # An index built before this feature holds no typed edges.
        st.conn.execute("DELETE FROM symbol_edge WHERE edge_type IN ('inherits','references')")
        st.conn.execute("DELETE FROM symbol_ref WHERE ref_type IN ('inherit','type')")
        st.conn.commit()
        assert _typed(workspace) == set()
        MIGRATIONS[-1][2](st.conn)
        st.conn.commit()
        await c.call_tool("index_project", {})
    assert ("p.sub.Sub", "p.base.Base", "inherits") in _typed(workspace)
