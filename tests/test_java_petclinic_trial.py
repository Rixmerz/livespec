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


