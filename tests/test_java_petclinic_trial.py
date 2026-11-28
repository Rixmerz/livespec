"""Defects a Spring PetClinic trial found in the Java support.

A small project shaped like the one that exposed them: a typed receiver, a
controller with a class-level `@RequestMapping`, Spring Data repositories, and
entities reached only through JPA relationships. Whole pipeline through FastMCP.
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


def _short(qname: str) -> str:
    return ".".join(qname.split(".")[-2:])


def _calls(workspace: Path) -> dict[tuple[str, str], float]:
    st = get_state(str(workspace))
    return {
        (_short(r[0]), _short(r[1])): r[2]
        for r in st.conn.execute(
            """SELECT a.qualified_name, b.qualified_name, e.weight FROM symbol_edge e
               JOIN symbol a ON a.id = e.src_symbol_id
               JOIN symbol b ON b.id = e.dst_symbol_id WHERE e.edge_type = 'calls'"""
        )
    }


async def _index(c: Client) -> None:
    await c.call_tool("index_project", {})


# --------------------------------------------------------------------------
# 1. typed receivers
# --------------------------------------------------------------------------

RECEIVERS = {
    "p/Owner.java": (
        "package p;\npublic class Owner extends Base {\n"
        "  public void addVisit(Integer id, String v) {}\n"
        "  public void addVisit(Integer id) {}\n}\n"
    ),
    "p/Base.java": "package p;\npublic class Base {\n  public void touch() {}\n}\n",
    "p/Pet.java": "package p;\npublic class Pet {\n  public void addVisit(String v) {}\n}\n",
    "p/Lib.java": (
        "package p;\npublic class Lib extends ext.Other {\n  public void close() {}\n}\n"
    ),
    "p/Lib2.java": "package p;\npublic class Lib2 {\n  public void close() {}\n}\n",
    "p/Use.java": (
        "package p;\nimport java.util.List;\npublic class Use {\n"
        "  private Owner owners;\n  private Pet pet;\n  private List<Owner> all;\n"
        '  void viaParam(Owner owner) { owner.addVisit(1, "x"); }\n'
        "  void viaLocal() { Owner o = new Owner(); o.addVisit(1); }\n"
        "  void viaField() { owners.addVisit(1); }\n"
        '  void viaThisField() { this.pet.addVisit("x"); }\n'
        "  void viaInherited(Owner owner) { owner.touch(); }\n"
        '  void shadowed(Pet owners) { owners.addVisit("x"); }\n'
        "  void viaGeneric() { all.add(null); }\n"
        "  void unknownType(java.io.Closeable c) { c.close(); }\n"
        "  void viaVar() { var o = new Owner(); o.addVisit(1); }\n"
        "  void inLambda() { all.forEach(owners -> owners.addVisit(1)); }\n}\n"
    ),
}


@pytest.mark.asyncio
async def test_typed_receiver_binds_to_the_declared_class_at_full_weight(workspace: Path):
    requires_grammar("java")
    _write(workspace, RECEIVERS)
    async with Client(mcp) as c:
        await _index(c)
    calls = _calls(workspace)
    for caller in ("viaParam", "viaLocal", "viaField"):
        assert calls.get((f"Use.{caller}", "Owner.addVisit")) == 1.0, caller
        # the class is known: no fan-out to the same-named method of Pet
        assert (f"Use.{caller}", "Pet.addVisit") not in calls, caller
    assert calls.get(("Use.viaThisField", "Pet.addVisit")) == 1.0
    assert ("Use.viaThisField", "Owner.addVisit") not in calls
    # a method of an ancestor is found on the receiver's class
    assert calls.get(("Use.viaInherited", "Base.touch")) == 1.0
    # a parameter shadows a field of the same name
    assert calls.get(("Use.shadowed", "Pet.addVisit")) == 1.0
    assert ("Use.shadowed", "Owner.addVisit") not in calls
    # a lambda parameter shadows the field too: nothing is claimed about it
    assert calls.get(("Use.inLambda", "Owner.addVisit"), 0) < 1.0
    assert calls.get(("Use.inLambda", "Pet.addVisit"), 0) < 1.0
    # `var` declares no class: unchanged ambiguous fan-out, never confirmed
    assert calls.get(("Use.viaVar", "Owner.addVisit"), 0) < 1.0


@pytest.mark.asyncio
async def test_typed_receiver_is_visible_to_who_calls_and_not_for_library_types(workspace: Path):
    requires_grammar("java")
    _write(workspace, RECEIVERS)
    async with Client(mcp) as c:
        await _index(c)
        out = (await c.call_tool("who_calls", {"qname": _qn(workspace, "Owner.addVisit")})).data
    callers = {_short(x["qualified_name"]) for x in out["callers"]}
    assert callers == {"Use.viaParam", "Use.viaLocal", "Use.viaField"}, callers
    # a library-typed receiver (`List.add`, `Closeable.close`) is as before:
    # only an unconfirmed name match, below the default traversal weight
    calls = _calls(workspace)
    assert calls.get(("Use.unknownType", "Lib.close"), 0) < 0.6
    assert calls.get(("Use.unknownType", "Lib2.close"), 0) < 0.6
    refs = (
        get_state(str(workspace))
        .conn.execute(
            "SELECT receiver, receiver_class FROM symbol_ref WHERE target_name='addVisit'"
            " AND src_symbol_id IN (SELECT id FROM symbol WHERE name='viaParam')"
        )
        .fetchall()
    )
    assert [(r[0], r[1]) for r in refs] == [("object", "Owner")]


def _qn(workspace: Path, short: str) -> str:
    row = (
        get_state(str(workspace))
        .conn.execute(
            "SELECT qualified_name FROM symbol WHERE qualified_name LIKE ? ORDER BY id LIMIT 1",
            (f"%{short.split('.')[0]}.{short}",),
        )
        .fetchone()
    )
    return str(row[0])


# --------------------------------------------------------------------------
# 2. Spring routes
# --------------------------------------------------------------------------

CONTROLLERS = {
    "p/PetController.java": (
        "package p;\nimport org.springframework.web.bind.annotation.*;\n"
        '@Controller\n@RequestMapping("/owners/{ownerId}")\npublic class PetController {\n'
        '  @GetMapping("/pets/new") String a() { return ""; }\n'
        '  @PostMapping(value = "/pets/new") String b() { return ""; }\n'
        '  @GetMapping(path = "pets/{id}") String c() { return ""; }\n'
        '  @GetMapping String d() { return ""; }\n'
        '  @RequestMapping(value = "/any", method = RequestMethod.PUT) String e() { return ""; }\n'
        "}\n"
    ),
    "p/VetController.java": (
        "package p;\nimport org.springframework.web.bind.annotation.*;\n"
        "@Controller\npublic class VetController {\n"
        '  @GetMapping({ "/vets" }) String f() { return ""; }\n'
        '  @GetMapping(value = {"/x", "/y"}) String g() { return ""; }\n'
        '  @GetMapping(BASE + "/z") String h() { return ""; }\n'
        "}\n"
    ),
}


@pytest.mark.asyncio
async def test_spring_class_prefix_and_array_paths(workspace: Path):
    requires_grammar("java")
    _write(workspace, CONTROLLERS)
    async with Client(mcp) as c:
        await _index(c)
        out = (await c.call_tool("find_endpoints", {"framework": "spring"})).data
    routes = {
        (e["qualified_name"].rsplit(".", 1)[-1], e.get("http_method"), e.get("http_path"))
        for e in out["endpoints"]
        if e.get("http_path")
    }
    assert routes == {
        ("a", "GET", "/owners/{ownerId}/pets/new"),
        ("b", "POST", "/owners/{ownerId}/pets/new"),
        ("c", "GET", "/owners/{ownerId}/pets/{id}"),
        ("d", "GET", "/owners/{ownerId}"),
        ("e", "PUT", "/owners/{ownerId}/any"),
        ("f", "GET", "/vets"),
        ("g", "GET", "/x"),
        ("g", "GET", "/y"),
    }, routes
    # a path built from a constant is not guessed
    h = [e for e in out["endpoints"] if e["qualified_name"].endswith(".h")]
    assert h and all(e.get("http_path") is None for e in h)


# --------------------------------------------------------------------------
# 3. dead code: entry points the framework or the JVM calls
# --------------------------------------------------------------------------

FRAMEWORK = {
    "p/App.java": (
        "package p;\npublic class App {\n"
        "  public static void main(String[] args) {}\n"
        "  public static void main2(String[] args) {}\n}\n"
    ),
    "p/Entity.java": "package p;\npublic class Entity {}\n",
    "p/VetRepository.java": (
        "package p;\nimport org.springframework.data.repository.Repository;\n"
        "public interface VetRepository extends Repository<Entity, Integer> {\n"
        "  java.util.List<Entity> findAll();\n}\n"
    ),
    "p/ChildRepository.java": (
        "package p;\npublic interface ChildRepository extends VetRepository {\n"
        "  Entity findByName(String n);\n}\n"
    ),
    "p/PlainService.java": "package p;\npublic interface PlainService {\n  void run();\n}\n",
    "p/Hints.java": (
        "package p;\nimport org.springframework.aot.hint.RuntimeHintsRegistrar;\n"
        "public class Hints implements RuntimeHintsRegistrar {\n"
        "  @Override public void registerHints(Object hints, ClassLoader cl) {}\n"
        "  public void unused() {}\n}\n"
    ),
    "p/Own.java": (
        "package p;\npublic class Own implements PlainService {\n"
        "  @Override public void run() {}\n}\n"
    ),
}


@pytest.mark.asyncio
async def test_java_entry_points_and_framework_implemented_are_not_dead(workspace: Path):
    requires_grammar("java")
    _write(workspace, FRAMEWORK)
    async with Client(mcp) as c:
        await _index(c)
        out = (
            await c.call_tool(
                "find_dead_code", {"include_public": True, "include_infrastructure": True}
            )
        ).data
    dead = {_short(d["qualified_name"]) for d in out["dead_symbols"]}
    assert "App.main" not in dead
    assert "VetRepository.findAll" not in dead
    assert "ChildRepository.findByName" not in dead  # through a project interface
    assert "Hints.registerHints" not in dead  # overrides a base outside the project
    # control: what is not any of those is still reported
    assert {"App.main2", "Hints.unused", "PlainService.run", "Own.run"} <= dead
    assert out["filtered_out"]["framework_implemented"] == 4


