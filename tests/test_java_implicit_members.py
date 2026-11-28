"""Java members and scopes the source never spells out.

Measured on a real Java repository (java-design-patterns): `Car::year` on a
record, Lombok accessors, `this::m` / a bare `m()` in a constructor, and one
class name in `src/main/java/<pkg>` and `src/test/java/<pkg>` each lost their
edge or took the wrong one. Drive the whole pipeline through FastMCP.
"""

from __future__ import annotations

import json
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


def _members(workspace: Path, cls: str) -> dict[str, tuple[str, int, list[str], str | None]]:
    """name -> (kind, line, decorators, visibility) of the symbols under class `cls`."""
    st = get_state(str(workspace))
    out = {}
    for r in st.conn.execute(
        "SELECT name, kind, start_line, decorators, visibility FROM symbol "
        "WHERE qualified_name LIKE ?",
        (f"%.{cls}.%",),
    ):
        out[r["name"]] = (
            r["kind"],
            r["start_line"],
            json.loads(r["decorators"] or "[]"),
            r["visibility"],
        )
    return out


def _edges(workspace: Path, edge_type: str = "calls") -> set[tuple[str, str, str, str]]:
    """(src file, src Class.member, dst file, dst Class.member)."""
    st = get_state(str(workspace))
    return {
        (r[0], _short(r[1]), r[2], _short(r[3]))
        for r in st.conn.execute(
            """SELECT fa.path, a.qualified_name, fb.path, b.qualified_name
               FROM symbol_edge e
               JOIN symbol a ON a.id = e.src_symbol_id JOIN file fa ON fa.id = a.file_id
               JOIN symbol b ON b.id = e.dst_symbol_id JOIN file fb ON fb.id = b.file_id
               WHERE e.edge_type = ?""",
            (edge_type,),
        )
    }


def _pairs(workspace: Path, edge_type: str = "calls") -> set[tuple[str, str]]:
    return {(s, d) for _a, s, _b, d in _edges(workspace, edge_type)}


async def _index(workspace: Path, files: dict[str, str]) -> None:
    _write(workspace, files)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})


# ---- 1. records ------------------------------------------------------------

RECORDS = {
    "p/Car.java": (
        "package p;\n"
        "public record Car(String make,\n"
        "                  int year,\n"
        "                  String... tags) {\n"
        "  public String make() { return make.toUpperCase(); }\n"
        "}\n"
    ),
    "p/Other.java": "package p;\npublic class Other {\n  public int year() { return 0; }\n}\n",
    "p/Use.java": (
        "package p;\nimport java.util.List;\n"
        "public class Use {\n"
        "  int ref(List<Car> l) { return l.stream().mapToInt(Car::year).sum(); }\n"
        "  int call(Car car) { return car.year(); }\n"
        "}\n"
    ),
}


@pytest.mark.asyncio
async def test_record_is_a_class_with_an_accessor_per_component(workspace: Path):
    requires_grammar("java")
    await _index(workspace, RECORDS)
    st = get_state(str(workspace))
    kind = st.conn.execute(
        "SELECT kind FROM symbol WHERE qualified_name LIKE '%.Car.Car'"
    ).fetchone()
    assert kind is not None and kind["kind"] == "class", "a record is a class symbol"
    members = _members(workspace, "Car")
    assert members["year"][0] == "method"
    assert members["year"][1] == 3, "the accessor sits on its component's line"
    assert any(d.startswith("livespec:generated") for d in members["year"][2])
    assert members["year"][3] == "public"
    assert "tags" in members, "a varargs component has an accessor too"
    # `make` is declared in the body: that one stays, with no generated twin
    rows = st.conn.execute(
        "SELECT decorators, start_line FROM symbol WHERE qualified_name LIKE '%.Car.make'"
    ).fetchall()
    assert len(rows) == 1 and not json.loads(rows[0]["decorators"] or "[]")
    assert rows[0]["start_line"] == 5


@pytest.mark.asyncio
async def test_method_reference_and_call_reach_the_record_accessor(workspace: Path):
    requires_grammar("java")
    await _index(workspace, RECORDS)
    calls = _pairs(workspace)
    assert ("Use.ref", "Car.year") in calls, "Car::year reaches the component accessor"
    assert ("Use.ref", "Other.year") not in calls, "Car::year is not Other.year"
    assert ("Use.call", "Car.year") in calls, "car.year() reaches it by name"


@pytest.mark.asyncio
async def test_record_declared_method_is_not_attributed_to_the_outer_scope(workspace: Path):
    requires_grammar("java")
    await _index(workspace, RECORDS)
    st = get_state(str(workspace))
    parent = st.conn.execute(
        "SELECT p.qualified_name FROM symbol s JOIN symbol p ON p.id = s.parent_symbol_id "
        "WHERE s.qualified_name LIKE '%.Car.make'"
    ).fetchone()
    assert parent is not None and parent[0].endswith(".Car.Car")


# ---- 2. Lombok -------------------------------------------------------------

LOMBOK = {
    "p/Person.java": (
        "package p;\n"  # 1
        "import lombok.Data;\n"  # 2
        "import lombok.AccessLevel;\n"  # 3
        "import lombok.Setter;\n"  # 4
        "@Data\n"  # 5
        "public class Person {\n"  # 6
        "  private String name;\n"  # 7
        "  private boolean active;\n"  # 8
        "  private final int id = 1;\n"  # 9
        "  private static int counter;\n"  # 10
        "  private int age;\n"  # 11
        "  @Setter(AccessLevel.NONE) private int secret;\n"  # 12
        "  public int getAge() { return age; }\n"  # 13
        "}\n"
    ),
    "p/Holder.java": (
        "package p;\n"  # 1
        "import lombok.Value;\n"  # 2
        "@Value\n"  # 3
        "public class Holder {\n"  # 4
        "  String label;\n"  # 5
        "  boolean ready;\n"  # 6
        "}\n"
    ),
    "p/Mixed.java": (
        "package p;\n"  # 1
        "import lombok.Getter;\n"  # 2
        "import lombok.Setter;\n"  # 3
        "public class Mixed {\n"  # 4
        "  @Getter private String shown;\n"  # 5
        "  @Setter private String edited;\n"  # 6
        "  @Getter @Setter private boolean isOn;\n"  # 7
        "  private String plain;\n"  # 8
        "}\n"
    ),
    "p/AllGet.java": (
        "package p;\n"  # 1
        "import lombok.Getter;\n"  # 2
        "@Getter\n"  # 3
        "public class AllGet {\n"  # 4
        "  private int a;\n"  # 5
        "  private final int b = 2;\n"  # 6
        "}\n"
    ),
    "p/NotLombok.java": ("package p;\n@Getter\npublic class NotLombok {\n  private int a;\n}\n"),
}


@pytest.mark.asyncio
async def test_lombok_data_generates_accessors_respecting_final_static_and_declared(
    workspace: Path,
):
    requires_grammar("java")
    await _index(workspace, LOMBOK)
    m = _members(workspace, "Person")
    assert m["getName"][:2] == ("method", 7) and m["setName"][1] == 7
    assert m["isActive"][1] == 8 and m["setActive"][1] == 8, "primitive boolean: isX / setX"
    assert "getActive" not in m
    assert "getId" in m and "setId" not in m, "a final field has no setter"
    assert not any("ounter" in n for n in m), "a static field has no accessors"
    assert m["getAge"][2] == [], "a getter the class declares is not generated"
    assert "setAge" in m
    assert "getSecret" in m and "setSecret" not in m, "AccessLevel.NONE suppresses the setter"
    assert all(d.startswith("livespec:generated") for d in m["getName"][2])


@pytest.mark.asyncio
async def test_lombok_value_has_getters_only_and_field_level_is_respected(workspace: Path):
    requires_grammar("java")
    await _index(workspace, LOMBOK)
    holder = _members(workspace, "Holder")
    assert {"getLabel", "isReady"} <= set(holder)
    assert not any(n.startswith("set") for n in holder), "@Value has no setters"
    mixed = _members(workspace, "Mixed")
    assert "getShown" in mixed and "setShown" not in mixed
    assert "setEdited" in mixed and "getEdited" not in mixed
    assert "isOn" in mixed and "setOn" in mixed, "a field already named isOn keeps its name"
    assert not any("lain" in n for n in mixed), "an unannotated field has none"
    allget = _members(workspace, "AllGet")
    assert {"getA", "getB"} <= set(allget) and not any(n.startswith("set") for n in allget)


@pytest.mark.asyncio
async def test_annotation_without_a_lombok_import_generates_nothing(workspace: Path):
    requires_grammar("java")
    await _index(workspace, LOMBOK)
    assert not any(n.startswith("get") for n in _members(workspace, "NotLombok"))


@pytest.mark.asyncio
async def test_lombok_getter_is_reached_by_method_reference_and_not_listed_dead(
    workspace: Path,
):
    requires_grammar("java")
    await _index(
        workspace,
        {
            **LOMBOK,
            "p/Use.java": (
                "package p;\nimport java.util.List;\npublic class Use {\n"
                "  void go(List<Person> l) { l.forEach(Person::getName); }\n}\n"
            ),
        },
    )
    assert ("Use.go", "Person.getName") in _pairs(workspace)
    async with Client(mcp) as c:
        res = await c.call_tool(
            "find_dead_code", {"include_public": True, "include_non_python": True}
        )
    names = {d["qualified_name"] for d in res.data["dead"]} if "dead" in res.data else set()
    assert not any(
        n.endswith((".Person.setName", ".Person.getId", ".Holder.getLabel")) for n in names
    )


# ---- 3. this::m and bare m() in a constructor ------------------------------

CTOR = {
    "p/Base.java": (
        "package p;\npublic class Base {\n"
        "  protected void init() {}\n"
        "  protected void shared() {}\n"
        "}\n"
    ),
    "p/Child.java": (
        "package p;\nimport java.util.List;\n"
        "public class Child extends Base {\n"
        "  private final Runnable r;\n"
        "  Child(List<String> l) {\n"
        "    init();\n"
        "    this.shared();\n"
        "    r = this::helper;\n"
        '    l.add("x");\n'
        "  }\n"
        "  void helper() {}\n"
        "  void add(String s) {}\n"
        "  class Inner {\n"
        "    Inner() { helper(); }\n"
        "  }\n"
        "}\n"
    ),
    "p/Decoy.java": (
        "package p;\npublic class Decoy {\n"
        "  void init() {}\n  void shared() {}\n  void helper() {}\n}\n"
    ),
    "p/Decoy2.java": (
        "package p;\npublic class Decoy2 {\n  void init() {}\n  void helper() {}\n}\n"
    ),
}


@pytest.mark.asyncio
async def test_constructor_calls_resolve_on_the_class_then_its_ancestors(workspace: Path):
    requires_grammar("java")
    await _index(workspace, CTOR)
    calls = _pairs(workspace)
    assert ("Child.Child", "Base.init") in calls, "bare init() in a ctor reaches the ancestor"
    assert ("Child.Child", "Base.shared") in calls, "this.shared() too"
    assert ("Child.Child", "Child.helper") in calls, "this::helper reaches the own method"
    assert not any(d.startswith(("Decoy.", "Decoy2.")) for s, d in calls if s == "Child.Child")
    assert ("Child.Inner", "Child.helper") in calls, "a nested class reaches its outer class"


# ---- 4. one package across src/main/java and src/test/java ------------------

SOURCE_ROOTS = {
    "mod/src/main/java/p/Helper.java": (
        "package p;\npublic class Helper {\n  public static void run() {}\n}\n"
    ),
    "mod/src/test/java/p/Helper.java": (
        "package p;\npublic class Helper {\n  public static void run() {}\n}\n"
    ),
    "mod/src/main/java/p/Service.java": (
        "package p;\npublic class Service {\n  Helper h;\n  void go() { Runnable r = Helper::run; }\n}\n"
    ),
    "mod/src/test/java/p/ServiceTest.java": (
        "package p;\npublic class ServiceTest {\n  Helper h;\n  Service s;\n"
        "  void go(java.util.List<Helper> l) { l.forEach(Helper::run); }\n}\n"
    ),
    "other/src/main/java/p/Service.java": "package p;\npublic class Service {}\n",
    "other/src/main/java/p/Helper.java": (
        "package p;\npublic class Helper {\n  public static void run() {}\n}\n"
    ),
}


@pytest.mark.asyncio
async def test_main_and_test_roots_are_one_package_and_the_callers_root_wins(workspace: Path):
    requires_grammar("java")
    await _index(workspace, SOURCE_ROOTS)
    refs = {(a, b, c) for a, b, c, _ in _edges(workspace, "references")}
    main_h = "mod/src/main/java/p/Helper.java"
    test_h = "mod/src/test/java/p/Helper.java"
    # the test sees main's Service without an import (same package, other root)
    assert (
        "mod/src/test/java/p/ServiceTest.java",
        "ServiceTest.ServiceTest",
        "mod/src/main/java/p/Service.java",
    ) in refs
    # each caller prefers the Helper of its own source root
    assert ("mod/src/main/java/p/Service.java", "Service.Service", main_h) in refs
    assert ("mod/src/test/java/p/ServiceTest.java", "ServiceTest.ServiceTest", test_h) in refs
    assert not any(a.startswith("mod/") and c.startswith("other/") for a, _b, c in refs), (
        "another module's same-named class in the same package is not this module's"
    )
    calls = _edges(workspace)
    assert ("mod/src/test/java/p/ServiceTest.java", "ServiceTest.go", test_h, "Helper.run") in calls
    assert ("mod/src/main/java/p/Service.java", "Service.go", main_h, "Helper.run") in calls
    assert ("mod/src/main/java/p/Service.java", "Service.go", test_h, "Helper.run") not in calls


@pytest.mark.asyncio
async def test_a_call_on_another_object_is_not_drawn_to_the_callers_generated_getter(
    workspace: Path,
):
    requires_grammar("java")
    await _index(
        workspace,
        {
            "p/Model.java": "package p;\npublic class Model {\n  public int getItems() { return 1; }\n}\n",
            "p/View.java": (
                "package p;\nimport lombok.Getter;\n@Getter\npublic class View {\n"
                "  private int items;\n"
                "  int show(Model model) { return model.getItems() + getItems(); }\n}\n"
            ),
        },
    )
    calls = _pairs(workspace)
    assert ("View.show", "Model.getItems") in calls, "model.getItems() still reaches Model"
    assert ("View.show", "View.getItems") in calls, "a bare getItems() is the class's own getter"
