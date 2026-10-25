"""Field-level schema extraction (OpenSpec change virtual-schema-first, R1/R2).

Pure extraction: `extract()` returns schemas with their fields in the neutral
type model. Nothing here touches storage.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from livespec_mcp.domain import extractors, languages, schema_types, sql_schema
from livespec_mcp.domain.extractors import ExtractResult, extract
from livespec_mcp.domain.schema_types import (
    FLAVORS,
    NEUTRAL_TYPES,
    WIRE_FLAVORS,
    name_key,
)

FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
REPO_SCHEMA_SQL = Path(__file__).parent.parent / "src" / "livespec_mcp" / "storage" / "schema.sql"


def _extract(filename: str, source: str) -> ExtractResult:
    return extract(Path(filename), source, Path("."))[1]


def _fixture(filename: str) -> ExtractResult:
    path = FIXTURES / filename
    return extract(path, path.read_text(encoding="utf-8"), FIXTURES)[1]


def _schema(result: ExtractResult, name: str):
    found = [s for s in result.schemas if s.src_qname.rsplit(".", 1)[-1] == name]
    assert len(found) == 1, [s.src_qname for s in result.schemas]
    return found[0]


def _fields(result: ExtractResult, name: str) -> dict:
    return {f.name: f for f in _schema(result, name).fields}


def _symbol(result: ExtractResult, qname: str):
    found = [s for s in result.symbols if s.qualified_name == qname]
    assert len(found) == 1, [s.qualified_name for s in result.symbols]
    return found[0]


# ---------- R1: Schema field extraction ----------


def test_pydantic_model():
    r = _extract(
        "m.py",
        "from pydantic import BaseModel\n"
        "class User(BaseModel):\n"
        "    id: int\n"
        "    email: str | None\n"
        "    nickname: str = 'x'\n",
    )
    assert _schema(r, "User").flavor == "pydantic"
    f = _fields(r, "User")
    assert (f["id"].type, f["id"].optional, f["id"].nullable) == ("int", False, False)
    assert (f["email"].type, f["email"].optional, f["email"].nullable) == ("string", False, True)
    assert (f["nickname"].type, f["nickname"].optional, f["nickname"].nullable) == (
        "string",
        True,
        False,
    )
    assert [x.line for x in f.values()] == [3, 4, 5]


def test_dataclass_default_factory():
    r = _extract(
        "m.py",
        "from dataclasses import dataclass, field\n"
        "@dataclass\n"
        "class Bag:\n"
        "    tags: list[str] = field(default_factory=list)\n",
    )
    assert _schema(r, "Bag").flavor == "dataclass"
    tags = _fields(r, "Bag")["tags"]
    assert (tags.type, tags.item_type, tags.optional, tags.nullable) == (
        "list",
        "string",
        True,
        False,
    )


def test_typescript_interface():
    r = _extract(
        "u.ts",
        "interface User { id: number; nickname?: string; deletedAt: string | null }\n",
    )
    assert _schema(r, "User").flavor == "ts_interface"
    assert _symbol(r, "u.User").kind == "class"
    f = _fields(r, "User")
    assert (f["nickname"].optional, f["nickname"].nullable) == (True, False)
    assert (f["deletedAt"].optional, f["deletedAt"].nullable) == (False, True)


def test_typescript_object_type_alias():
    r = _extract("u.ts", "type User = { id: number };\ntype Id = string;\n")
    assert _symbol(r, "u.User").kind == "type_alias"
    assert _schema(r, "User").flavor == "ts_type"
    assert list(_fields(r, "User")) == ["id"]
    assert [s.src_qname for s in r.schemas] == ["u.User"]
    assert not [s for s in r.symbols if s.name == "Id"]


def test_zod_object():
    r = _extract(
        "u.ts",
        "import { z } from 'zod';\n"
        "const User = z.object({ id: z.string().uuid(), age: z.number().int().optional() });\n",
    )
    assert _symbol(r, "u.User").kind == "variable"
    assert _schema(r, "User").flavor == "zod"
    f = _fields(r, "User")
    assert f["id"].type == "uuid"
    assert (f["age"].type, f["age"].optional) == ("int", True)


def test_sql_table():
    r = _extract(
        "db.sql",
        "CREATE TABLE IF NOT EXISTS users "
        "(id INTEGER PRIMARY KEY, email TEXT NOT NULL, bio TEXT, UNIQUE(email))",
    )
    sym = _symbol(r, "db.users")
    assert (sym.kind, sym.name) == ("table", "users")
    assert _schema(r, "users").flavor == "sql_table"
    f = _fields(r, "users")
    assert list(f) == ["id", "email", "bio"]
    assert f["bio"].nullable is True
    assert f["email"].nullable is False


def test_plain_class_has_no_schema():
    r = _extract("m.py", "class Plain:\n    name: str\n\nclass Other(object):\n    x: int\n")
    assert r.schemas == []
    assert {s.name for s in r.symbols} == {"Plain", "Other"}


def test_sql_is_read_without_a_grammar(monkeypatch):
    def refuse(language):
        raise AssertionError(f"get_parser({language!r}) called for SQL")

    monkeypatch.setattr(extractors, "get_parser", refuse)
    monkeypatch.setattr(languages, "get_parser", refuse)
    lang, r = extract(Path("db.sql"), "CREATE TABLE t (id INTEGER NOT NULL);", Path("."))
    assert lang == "sql"
    assert r.grammar_missing is None and r.parse_error is False
    assert list(_fields(r, "t")) == ["id"]


def test_sql_language_tables():
    assert languages.detect_language(Path("x/schema.SQL")) == "sql"
    assert languages.SCHEMA_ONLY_LANGUAGES == frozenset({"sql"})
    assert "sql" in languages.ANNOTATION_SUPPORTED_LANGUAGES
    # EXTRACTOR_SUPPORTED drives the grammar prefetch: SQL must stay out of it.
    assert "sql" not in languages.EXTRACTOR_SUPPORTED


# ---------- R2: Neutral field types ----------


def test_nullable_is_not_optional():
    r = _extract("m.py", "from pydantic import BaseModel\nclass M(BaseModel):\n    x: int | None\n")
    x = _fields(r, "M")["x"]
    assert (x.type, x.nullable, x.optional) == ("int", True, False)


def test_optional_is_not_nullable():
    x = _fields(_extract("m.ts", "interface M { x?: number }"), "M")["x"]
    assert (x.type, x.optional, x.nullable) == ("float", True, False)


def test_zod_nullish():
    r = _extract("m.ts", "const M = z.object({ x: z.string().nullish() });")
    x = _fields(r, "M")["x"]
    assert (x.optional, x.nullable) == (True, True)


def test_sql_default():
    r = _extract(
        "db.sql",
        "CREATE TABLE t (created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);",
    )
    c = _fields(r, "t")["created_at"]
    assert (c.type, c.optional, c.nullable) == ("datetime", True, False)


def test_list_of_references():
    r = _extract(
        "m.py", "from pydantic import BaseModel\nclass M(BaseModel):\n    items: list[OrderItem]\n"
    )
    items = _fields(r, "M")["items"]
    assert (items.type, items.item_type, items.ref_target) == ("list", "ref", "OrderItem")


def test_unmapped_type():
    r = _extract("m.py", "from pydantic import BaseModel\nclass M(BaseModel):\n    x: str | int\n")
    assert _fields(r, "M")["x"].type == "unknown"


def test_same_file_enum():
    r = _extract(
        "m.py",
        "from enum import Enum\nfrom pydantic import BaseModel\n"
        "class M(BaseModel):\n    color: Color\n    other: Elsewhere\n"
        "class Color(Enum):\n    RED = 1\n",
    )
    f = _fields(r, "M")
    assert f["color"].type == "enum"
    assert (f["other"].type, f["other"].ref_target) == ("ref", "Elsewhere")
    ts = _fields(_extract("m.ts", "enum Color { Red }\ninterface M { c: Color }"), "M")
    assert ts["c"].type == "enum"


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("z.string().optional()", ("string", True, False)),
        ("z.string().nullable()", ("string", False, True)),
        ("z.string().nullish()", ("string", True, True)),
        ("z.number().default(1)", ("float", True, False)),
        ("z.number().int()", ("int", False, False)),
        ("z.string().uuid()", ("uuid", False, False)),
        ("z.string().datetime()", ("datetime", False, False)),
    ],
)
def test_zod_modifiers(expr, expected):
    r = _extract("m.ts", f"const M = z.object({{ x: {expr} }});")
    x = _fields(r, "M")["x"]
    assert (x.type, x.optional, x.nullable) == expected


def test_name_key_ignores_conventions():
    assert {name_key(s) for s in ("user_id", "userId", "UserID", "user-id", "user.id")} == {
        "userid"
    }


def test_contract_constants():
    assert NEUTRAL_TYPES == (
        "string",
        "int",
        "float",
        "decimal",
        "bool",
        "datetime",
        "date",
        "uuid",
        "json",
        "enum",
        "list",
        "ref",
        "unknown",
    )
    assert FLAVORS == ("pydantic", "dataclass", "ts_interface", "ts_type", "zod", "sql_table")
    assert frozenset({"ts_interface", "ts_type", "zod"}) == WIRE_FLAVORS


# ---------- Same-file inheritance ----------


def test_python_same_file_inheritance():
    r = _fixture("models.py")
    user = _fields(r, "User")
    # Base fields first; the redeclared `id` keeps its base position.
    assert list(user)[:3] == ["id", "created_at", "email"]
    assert (user["id"].optional, user["id"].line) == (True, 47)
    gift = _fields(r, "GiftItem")
    assert list(gift) == ["sku", "qty", "labels", "note", "message"]
    assert _schema(r, "GiftItem").flavor == "dataclass"
    assert gift["qty"].line == 74


def test_typescript_same_file_extends():
    r = _fixture("types.ts")
    user = _fields(r, "User")
    assert list(user)[:3] == ["id", "createdAt", "nickname"]
    admin = _fields(r, "Admin")
    assert next(iter(admin)) == "id" and list(admin)[-1] == "level"
    assert admin["id"].line == 44
    assert len(admin) == len(user) + 1


def test_zod_same_file_extend():
    r = _fixture("zod_schemas.ts")
    user, admin = _fields(r, "User"), _fields(r, "Admin")
    assert list(admin) == [*user, "level"]
    assert (user["age"].optional, admin["age"].optional) == (True, False)


# ---------- Fixture details per language ----------


def test_python_fixture_rules():
    r = _fixture("models.py")
    assert {s.src_qname.rsplit(".", 1)[-1] for s in r.schemas} == {
        "Base",
        "User",
        "Settings",
        "OrderItem",
        "GiftItem",
    }
    user = _fields(r, "User")
    for skipped in ("model_config", "registry", "_secret"):
        assert skipped not in user
    assert (user["alias"].optional, user["alias"].nullable) == (True, True)
    assert (user["login"].type, user["login"].optional) == ("string", False)
    assert user["code"].type == "string"
    assert user["kind"].type == "enum"
    assert user["status"].type == "enum"
    assert user["ratio"].type == "float"
    assert user["extra"].type == "json"
    assert user["many"].item_type == "int"
    assert user["pair"].type == "unknown"
    assert (user["owner"].type, user["owner"].ref_target) == ("ref", "OrderItem")
    assert user["external_id"].type == "uuid"
    assert user["balance"].type == "decimal"
    assert user["born"].type == "date"
    item = _fields(r, "OrderItem")
    assert "seed" not in item
    assert (item["note"].optional, item["note"].nullable) == (False, True)
    assert _schema(r, "Settings").flavor == "pydantic"


def test_typescript_fixture_rules():
    r = _fixture("types.ts")
    assert {s.src_qname.rsplit(".", 1)[-1] for s in r.schemas} == {
        "Base",
        "User",
        "Admin",
        "Address",
    }
    names = {s.name for s in r.symbols}
    assert not names & {"Role", "Id", "Point"}
    user = _fields(r, "User")
    assert "greet" not in user and "extra" not in user
    assert (user["email"].optional, user["email"].nullable) == (True, False)
    assert user["display-name"].field_key == "displayname"
    assert user["role"].type == "enum"
    assert user["createdAt"].type == "datetime"
    assert (user["friends"].type, user["friends"].ref_target) == ("list", "User")
    assert user["meta"].type == "json"
    assert user["score"].type == "int"
    assert user["active"].type == "bool"
    assert {user[k].type for k in ("either", "pair", "callback")} == {"unknown"}
    address = _symbol(r, "types.Address")
    assert address.kind == "type_alias" and address.visibility == "exported"
    assert "@schema: user-profile" in (_symbol(r, "types.User").docstring or "")


def test_zod_fixture_rules():
    r = _fixture("zod_schemas.ts")
    user = _fields(r, "User")
    assert user["createdAt"].type == "datetime"
    assert user["birthday"].type == "date"
    assert user["seenAt"].type == "datetime"
    assert user["big"].type == "int"
    assert user["role"].type == "enum"
    assert (user["tags"].type, user["tags"].item_type) == ("list", "string")
    assert (user["aliases"].type, user["aliases"].item_type) == ("list", "string")
    assert (user["home"].type, user["home"].ref_target) == ("ref", "Address")
    assert (user["others"].item_type, user["others"].optional) == ("ref", True)
    assert {user["meta"].type, user["inline"].type} == {"json"}
    assert user["either"].type == "unknown"
    assert user["count"].type == "int"
    assert _symbol(r, "zod_schemas.Address").docstring == "A postal address.\n@schema: address"
    assert list(_fields(r, "Checked")) == ["n"]
    # `zod` is a namespace import of the zod module; `other` is not.
    assert list(_fields(r, "Loose")) == ["label"]
    assert not [s for s in r.symbols if s.name == "NotZod"]


def test_zod_keeps_call_attribution():
    r = _fixture("zod_schemas.ts")
    # A Zod object holding a callback stays the function symbol it was; no
    # second symbol of the same name.
    assert _symbol(r, "zod_schemas.Loose").kind == "function"
    qnames = [s.qualified_name for s in r.symbols]
    assert len(qnames) == len(set(qnames))
    # The z.object()/z.string() calls of a schema const still belong to the
    # module pseudo-symbol: the new `variable` symbol owns no calls.
    owners = {ref.src_qname for ref in r.refs}
    assert "zod_schemas.User" not in owners
    assert {"object", "string", "uuid"} <= {
        ref.target_name for ref in r.refs if ref.src_qname == "zod_schemas" and ref.line <= 34
    }
    assert ("zod_schemas.Loose", "makeLabel") in {(x.src_qname, x.target_name) for x in r.refs}


def test_javascript_zod_and_non_module_level():
    r = _extract(
        "m.js",
        "const zz = require('x');\n"
        "export const A = z.object({ a: z.string() });\n"
        "function f() { const Local = z.object({ b: z.string() }); return Local; }\n",
    )
    assert [s.src_qname for s in r.schemas] == ["m.A"]


def test_sql_fixture_rules():
    r = _fixture("tables.sql")
    assert [s.qualified_name for s in r.symbols] == [
        "tables.users",
        "tables.orders",
        "tables.order_items",
    ]
    users = _fields(r, "users")
    assert "UNIQUE" not in users and "CONSTRAINT" not in users and "extra" not in users
    assert users["id"].optional and not users["id"].nullable
    assert (users["display name"].raw_type, users["display name"].optional) == (
        "VARCHAR(80)",
        True,
    )
    assert users["balance"].type == "decimal"
    assert users["feeling"].type == "enum"
    assert (users["tags"].type, users["tags"].item_type) == ("list", "string")
    assert users["payload"].type == "json"
    assert users["ratio"].type == "float"
    assert users["seen_at"].type == "datetime"
    assert users["avatar"].type == "unknown"
    assert users["email"].line == 10
    orders = _fields(r, "orders")
    assert list(orders) == ["id", "user_id", "code", "total"]
    assert (orders["id"].optional, orders["id"].nullable) == (True, False)
    assert orders["total"].optional
    items = _fields(r, "order_items")
    assert not items["order_id"].nullable and not items["order_id"].optional
    users_sym = _symbol(r, "tables.users")
    assert users_sym.docstring == "Application users.\n@schema: user-profile"
    assert (users_sym.start_line, users_sym.end_line) == (8, 26)
    assert _symbol(r, "tables.orders").docstring == "Orders placed by users.\nOne row per checkout."


def test_sql_body_hash_seed_survives_reformat_and_comments():
    original = "-- users\nCREATE TABLE users (id INTEGER PRIMARY KEY, bio TEXT);\n"
    reformatted = (
        "-- the people who log in\n"
        "CREATE TABLE users (\n"
        "    id  INTEGER PRIMARY KEY,   -- rowid\n"
        "    /* free text */ bio TEXT\n"
        ")\n;\n"
    )
    changed = "CREATE TABLE users (id INTEGER PRIMARY KEY, bio TEXT NOT NULL);\n"
    seed = _symbol(_extract("db.sql", original), "db.users").body_hash_seed
    assert _symbol(_extract("db.sql", reformatted), "db.users").body_hash_seed == seed
    assert _symbol(_extract("db.sql", changed), "db.users").body_hash_seed != seed


def test_repo_schema_sql():
    text = REPO_SCHEMA_SQL.read_text(encoding="utf-8")
    expected = re.findall(r"^CREATE TABLE IF NOT EXISTS (\w+)", text, flags=re.M)
    lang, r = extract(REPO_SCHEMA_SQL, text, REPO_SCHEMA_SQL.parent)
    assert lang == "sql"
    tables = [s.src_qname.rsplit(".", 1)[-1] for s in r.schemas]
    assert tables == expected
    assert "chunk_fts" not in tables
    assert {s.kind for s in r.symbols} == {"table"}
    start_line = _fields(r, "symbol")["start_line"]
    assert (start_line.type, start_line.nullable) == ("int", False)
    norm_path = _fields(r, "route_ref")["norm_path"]
    assert (norm_path.type, norm_path.nullable) == ("string", False)
    all_fields = [f.name for s in r.schemas for f in s.fields]
    assert not [n for n in all_fields if n.upper() in sql_schema._TABLE_CONSTRAINTS]
    assert not [n for n in all_fields if n.startswith(("idx_", "chunk_a"))]


# ---------- Contract invariants over every fixture ----------


@pytest.mark.parametrize("filename", ["models.py", "types.ts", "zod_schemas.ts", "tables.sql"])
def test_schema_invariants(filename):
    r = _fixture(filename)
    assert r.schemas
    qnames = {s.qualified_name for s in r.symbols}
    for schema in r.schemas:
        assert schema.src_qname in qnames
        assert schema.flavor in FLAVORS
        names = [f.name for f in schema.fields]
        assert len(names) == len(set(names)), schema.src_qname
        for f in schema.fields:
            assert f.type in NEUTRAL_TYPES
            assert f.field_key == name_key(f.name)
            assert (f.item_type is not None) == (f.type == "list")
            assert (f.ref_target is not None) == ("ref" in (f.type, f.item_type))


def test_new_code_carries_its_spec_ids():
    r1 = "@spec:schemas-schema-field-extraction"
    r2 = "@spec:schemas-neutral-field-types"
    for fn in (
        extractors._py_class_fields,
        extractors._ts_object_fields,
        extractors._zod_object_fields,
        sql_schema.extract_sql_schemas,
    ):
        assert (inspect.getdoc(fn) or "").splitlines()[0] == r1, fn.__name__
    for fn in (
        schema_types.py_type,
        schema_types.py_optional,
        schema_types.ts_type,
        schema_types.zod_type,
        schema_types.sql_type,
    ):
        assert (inspect.getdoc(fn) or "").splitlines()[0] == r2, fn.__name__


def test_zod_object_with_object_level_refine_is_extracted():
    src = "import { z } from 'zod'; export const W = z.object({ s: z.string() }).refine(v => true).describe('w');"
    sch = _schema(_extract("a.ts", src), "W")
    assert [f.name for f in sch.fields] == ["s"]


def test_ts_unknown_generics_do_not_become_refs():
    src = "interface A { m: Map<string,number>; s: Set<string>; p: Promise<number> }"
    sch = _schema(_extract("a.ts", src), "A")
    got = {f.name: (f.type, f.ref_target) for f in sch.fields}
    assert got["m"][0] == "json"
    assert got["s"][0] == "list"
    assert got["p"][0] == "unknown"
    assert all(t[1] is None for t in got.values())
