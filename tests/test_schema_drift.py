"""v0.34 P3: field-level drift between the schemas bound to one entity
(`schema_drift` and the `project://schemas` resources)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from livespec_mcp.domain.schema_drift import SchemaField, SchemaSide, SymbolRef, compare_entity
from livespec_mcp.domain.schema_types import name_key
from livespec_mcp.server import mcp
from livespec_mcp.state import reset_state

from .conftest import requires_grammar

# ---------- comparator ----------


def _f(name: str, type_: str, *, item: str | None = None, opt=False, null=False, line=1):
    return SchemaField(
        name=name,
        field_key=name_key(name),
        type=type_,
        item_type=item,
        ref_target=None,
        raw_type=None,
        optional=opt,
        nullable=null,
        line=line,
    )


def _side(flavor: str, *fields: SchemaField, qname: str | None = None, line: int = 1):
    ref = SymbolRef(qname or f"m.{flavor}", "p", f"{flavor}.src", line)
    return SchemaSide(ref=ref, flavor=flavor, fields=fields)


def _kinds(sides: list[SchemaSide]) -> list[tuple[str, str]]:
    findings, _ = compare_entity("user", sides)
    return [(f["kind"], f["field_key"]) for f in findings]


def _compatible(a: SchemaSide, b: SchemaSide) -> bool:
    return ("type_mismatch", "x") not in _kinds([a, b])


def test_missing_field_names_present_and_missing_sides():
    py = _side("pydantic", _f("id", "int", line=3), _f("avatar_url", "string", line=4), line=2)
    ts = _side("ts_interface", _f("id", "float", line=8), line=7)

    findings, _ = compare_entity("user", [py, ts])

    assert [(f["kind"], f["field_key"]) for f in findings] == [("missing_field", "avatarurl")]
    (f,) = findings
    assert f["field"] == "avatar_url"
    assert [(s["file"], s["line"]) for s in f["sides"]] == [("pydantic.src", 4)]
    # A missing side's line is the schema symbol's line, not a field's.
    assert [(s["file"], s["line"]) for s in f["missing_in"]] == [("ts_interface.src", 7)]


def test_snake_and_camel_case_are_one_field():
    py = _side("pydantic", _f("created_at", "datetime"))
    ts = _side("ts_interface", _f("createdAt", "string"))
    assert _kinds([py, ts]) == []


def test_type_mismatch_lists_every_holding_side():
    py = _side("pydantic", _f("age", "int"))
    sql = _side("sql_table", _f("age", "string"))

    findings, _ = compare_entity("user", [py, sql])

    assert [(f["kind"], f["field_key"]) for f in findings] == [("type_mismatch", "age")]
    assert [s["type"] for s in findings[0]["sides"]] == ["int", "string"]


def test_nullability_mismatch_counts_sql_sides():
    sql = _side("sql_table", _f("email", "string"))
    py = _side("pydantic", _f("email", "string", null=True))
    assert _kinds([sql, py]) == [("nullability_mismatch", "email")]


def test_optional_mismatch_lists_only_non_sql_sides():
    ts = _side("ts_interface", _f("nickname", "string", opt=True))
    py = _side("pydantic", _f("nickname", "string"))
    sql = _side("sql_table", _f("nickname", "string"))

    findings, _ = compare_entity("user", [ts, py, sql])

    assert [(f["kind"], f["field_key"]) for f in findings] == [("optional_mismatch", "nickname")]
    assert [s["file"] for s in findings[0]["sides"]] == ["ts_interface.src", "pydantic.src"]


def test_optional_is_never_judged_against_sql():
    ts = _side("ts_interface", _f("nickname", "string", opt=True))
    sql = _side("sql_table", _f("nickname", "string"))
    assert _kinds([ts, sql]) == []


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # A wire side's `number` carries ints and decimals.
        (("ts_interface", "float"), ("pydantic", "int")),
        (("zod", "float"), ("pydantic", "decimal")),
        (("ts_type", "float"), ("zod", "int")),
        # A wire side's `string` carries dates, ids and decimals.
        (("ts_interface", "string"), ("pydantic", "datetime")),
        (("ts_type", "string"), ("dataclass", "date")),
        (("zod", "string"), ("sql_table", "uuid")),
        (("ts_interface", "string"), ("pydantic", "decimal")),
        # A SQL JSON column stores nested objects and arrays; text stores enums.
        (("sql_table", "json"), ("pydantic", "ref")),
        (("sql_table", "json"), ("ts_interface", "list")),
        (("sql_table", "string"), ("pydantic", "enum")),
    ],
)
def test_compatible_pairs_in_either_order(a, b):
    left = _side(a[0], _f("x", a[1], item="string" if a[1] == "list" else None))
    right = _side(b[0], _f("x", b[1], item="string" if b[1] == "list" else None))
    assert _compatible(left, right)
    assert _compatible(right, left)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # The rule belongs to the side that holds the looser type: a backend
        # `str` is not a wire `uuid`.
        (("pydantic", "string"), ("zod", "uuid")),
        (("pydantic", "float"), ("dataclass", "int")),
        (("ts_interface", "string"), ("pydantic", "enum")),
        (("pydantic", "json"), ("sql_table", "ref")),
        (("ts_interface", "bool"), ("pydantic", "string")),
    ],
)
def test_incompatible_pairs(a, b):
    left = _side(a[0], _f("x", a[1]))
    right = _side(b[0], _f("x", b[1]))
    assert not _compatible(left, right)
    assert not _compatible(right, left)


def test_ref_matches_ref_whatever_the_target():
    py = _side(
        "pydantic",
        SchemaField("x", "x", "ref", None, "Address", "Address", False, False, 1),
    )
    ts = _side(
        "ts_interface",
        SchemaField("x", "x", "ref", None, "Location", "Location", False, False, 1),
    )
    assert _compatible(py, ts)


def test_lists_compare_their_items_by_the_same_rules():
    ts = _side("ts_interface", _f("x", "list", item="string"))
    py_ids = _side("pydantic", _f("x", "list", item="uuid"))
    py_ints = _side("pydantic", _f("x", "list", item="int"))
    dc_strs = _side("dataclass", _f("x", "list", item="string"))

    assert _compatible(ts, py_ids)
    assert not _compatible(py_ints, dc_strs)
    assert not _compatible(_side("pydantic", _f("x", "list", item="string")), py_ids)


def test_unknown_is_counted_once_and_never_type_compared():
    py = _side("pydantic", _f("x", "unknown"), _f("tags", "list", item="unknown"))
    ts = _side("ts_interface", _f("x", "bool"), _f("tags", "list", item="string"))
    sql = _side("sql_table", _f("x", "string"), _f("tags", "json"))

    findings, uncomparable = compare_entity("user", [py, ts, sql])

    assert findings == []
    assert uncomparable == 2


def test_a_field_on_one_side_only_is_not_uncomparable():
    _, uncomparable = compare_entity("user", [_side("pydantic", _f("x", "unknown"))])
    assert uncomparable == 0


def test_first_field_by_position_wins_within_a_side():
    py = _side("pydantic", _f("userId", "string"), _f("user_id", "int"))
    ts = _side("ts_interface", _f("user_id", "string"))

    findings, _ = compare_entity("user", [py, ts])

    assert findings == []


def test_a_single_side_has_no_findings():
    assert _kinds([_side("pydantic", _f("x", "int", null=True))]) == []


def test_findings_are_sorted_by_kind_then_field_key_whatever_the_input_order():
    def sides(reverse: bool) -> list[SchemaSide]:
        py_fields = [
            _f("zeta", "int"),
            _f("alpha", "int"),
            _f("nick", "string"),
            _f("mail", "string", null=True),
            _f("only_py", "string"),
        ]
        ts_fields = [
            _f("zeta", "bool"),
            _f("alpha", "bool"),
            _f("nick", "string", opt=True),
            _f("mail", "string"),
        ]
        out = [
            _side("pydantic", *(reversed(py_fields) if reverse else py_fields)),
            _side("ts_interface", *(reversed(ts_fields) if reverse else ts_fields)),
        ]
        return list(reversed(out)) if reverse else out

    expected = [
        ("missing_field", "onlypy"),
        ("type_mismatch", "alpha"),
        ("type_mismatch", "zeta"),
        ("nullability_mismatch", "mail"),
        ("optional_mismatch", "nick"),
    ]
    assert _kinds(sides(False)) == expected
    assert _kinds(sides(True)) == expected


# ---------- end to end: a backend and a frontend in one group_db ----------

_PYDANTIC = '''from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class User(BaseModel):
    """A user as the API returns it.

    @schema: user
    """

    id: UUID
    email: str | None
    age: int
    nickname: str
    bio: str = ""
    avatar_url: str | None = None
    created_at: datetime
'''

_SQL = """-- Users.
-- @schema: user
CREATE TABLE users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL,
    age TEXT NOT NULL,
    nickname TEXT NOT NULL,
    bio TEXT NOT NULL,
    avatar_url TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_TS = """/** @schema: User */
export interface User {
  id: string;
  email: string | null;
  age: number;
  nickname?: string;
  bio?: string;
  createdAt: string;
}
"""

_ZOD = """import { z } from "zod";

/** @schema: user */
export const UserSchema = z.object({
  id: z.string().uuid(),
  email: z.string().nullable(),
  age: z.number().int(),
  nickname: z.string().optional(),
  bio: z.string().optional(),
  avatarUrl: z.string().nullable().optional(),
  createdAt: z.string().datetime(),
});
"""

# Planted drift, one finding per R5 scenario. Everything else agrees: `id`
# is a UUID carried as a string, `created_at` is `createdAt`, and `bio` is
# optional everywhere but in SQL, which is never judged on optional.
_EXPECTED = [
    ("missing_field", "avatarurl"),
    ("type_mismatch", "age"),
    ("nullability_mismatch", "email"),
    ("optional_mismatch", "nickname"),
]


def _line(text: str, needle: str) -> int:
    return next(i for i, ln in enumerate(text.splitlines(), 1) if needle in ln)


def _group(tmp_path: Path) -> tuple[Path, Path]:
    shared = tmp_path / "grp" / "shared.db"
    toml = f'[workspace]\ngroup_db = "{shared}"\n'
    back = tmp_path / "back"
    (back / "app").mkdir(parents=True)
    (back / "app" / "__init__.py").write_text("")
    (back / "app" / "models.py").write_text(_PYDANTIC)
    (back / "migrations").mkdir()
    (back / "migrations" / "001.sql").write_text(_SQL)
    (back / ".livespec.toml").write_text(toml)
    front = tmp_path / "front"
    (front / "src").mkdir(parents=True)
    (front / "src" / "user.ts").write_text(_TS)
    (front / "src" / "schemas.ts").write_text(_ZOD)
    (front / ".livespec.toml").write_text(toml)
    return back, front


async def _drift(c: Client, ws: Path, **args: Any) -> dict[str, Any]:
    return (await c.call_tool("schema_drift", {"workspace": str(ws), **args})).data


async def _indexed_group(c: Client, tmp_path: Path) -> tuple[Path, Path]:
    back, front = _group(tmp_path)
    for repo in (front, back):
        out = (await c.call_tool("index_project", {"workspace": str(repo)})).data
        assert "error" not in out, out
    return back, front


def _by_kind(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["kind"]: f for f in payload["findings"]}


@pytest.mark.asyncio
async def test_group_drift_reports_every_scenario_with_file_and_line(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _drift(c, back)

    assert "error" not in out, out
    assert out["scope"] == {"grouped": True, "projects": ["back", "front"]}
    assert [(f["kind"], f["field_key"]) for f in out["findings"]] == _EXPECTED
    assert out["counts"] == {
        "entities": 1,
        "entities_with_drift": 1,
        "findings": 4,
        "by_kind": {
            "missing_field": 1,
            "type_mismatch": 1,
            "nullability_mismatch": 1,
            "optional_mismatch": 1,
        },
    }
    assert out["next_cursor"] is None

    (row,) = out["entities"]
    assert row["entity"] == "user"
    assert [(s["project"], s["file"], s["flavor"]) for s in row["schemas"]] == [
        ("back", "app/models.py", "pydantic"),
        ("back", "migrations/001.sql", "sql_table"),
        ("front", "src/schemas.ts", "zod"),
        ("front", "src/user.ts", "ts_interface"),
    ]
    assert row["links"] == 6
    assert row["counts"] == {
        "missing_field": 1,
        "type_mismatch": 1,
        "nullability_mismatch": 1,
        "optional_mismatch": 1,
        "uncomparable": 0,
    }

    found = _by_kind(out)
    missing = found["missing_field"]
    assert missing["field"] == "avatar_url"
    assert [(s["file"], s["line"]) for s in missing["sides"]] == [
        ("app/models.py", _line(_PYDANTIC, "avatar_url:")),
        ("migrations/001.sql", _line(_SQL, "avatar_url TEXT")),
        ("src/schemas.ts", _line(_ZOD, "avatarUrl:")),
    ]
    assert [(m["project"], m["file"], m["line"]) for m in missing["missing_in"]] == [
        ("front", "src/user.ts", _line(_TS, "export interface User"))
    ]

    mismatch = found["type_mismatch"]
    assert [(s["file"], s["line"], s["type"]) for s in mismatch["sides"]] == [
        ("app/models.py", _line(_PYDANTIC, "age:"), "int"),
        ("migrations/001.sql", _line(_SQL, "age TEXT"), "string"),
        ("src/schemas.ts", _line(_ZOD, "age:"), "int"),
        ("src/user.ts", _line(_TS, "age:"), "float"),
    ]

    nullability = found["nullability_mismatch"]
    assert [(s["file"], s["line"], s["nullable"]) for s in nullability["sides"]] == [
        ("app/models.py", _line(_PYDANTIC, "email:"), True),
        ("migrations/001.sql", _line(_SQL, "email TEXT"), False),
        ("src/schemas.ts", _line(_ZOD, "email:"), True),
        ("src/user.ts", _line(_TS, "email:"), True),
    ]

    optional = found["optional_mismatch"]
    assert [(s["file"], s["line"], s["optional"]) for s in optional["sides"]] == [
        ("app/models.py", _line(_PYDANTIC, "nickname:"), False),
        ("src/schemas.ts", _line(_ZOD, "nickname:"), True),
        ("src/user.ts", _line(_TS, "nickname?:"), True),
    ]


@pytest.mark.asyncio
async def test_limit_one_walks_every_finding_with_exact_counts(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        full = await _drift(c, back)
        seen: list[dict[str, Any]] = []
        cursor: int | None = 0
        pages = 0
        while cursor is not None:
            page = await _drift(c, back, limit=1, cursor=cursor)
            assert len(page["findings"]) == 1
            assert page["counts"] == full["counts"]
            seen.extend(page["findings"])
            cursor = page["next_cursor"]
            pages += 1

    assert pages == 4
    assert seen == full["findings"]


@pytest.mark.asyncio
async def test_summary_only_drops_findings_and_keeps_counts(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        full = await _drift(c, back)
        summary = await _drift(c, back, summary_only=True)

    assert "findings" not in summary
    assert summary["counts"] == full["counts"]
    assert summary["entities"] == full["entities"]


@pytest.mark.asyncio
async def test_entity_filter_accepts_any_spelling(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        _, front = await _indexed_group(c, tmp_path)
        out = await _drift(c, front, entity="USER")

    assert [(f["kind"], f["field_key"]) for f in out["findings"]] == _EXPECTED
    # Called from the frontend: its project comes first.
    assert out["scope"]["projects"] == ["front", "back"]


@pytest.mark.asyncio
async def test_unknown_entity_is_an_mcp_error_with_close_names(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _drift(c, back, entity="usr")

    assert set(out) == {"error", "isError", "did_you_mean", "hint"}
    assert out["isError"] is True
    assert "usr" in out["error"]
    assert out["did_you_mean"] == ["user"]
    assert "@schema: <entity>" in out["hint"]


@pytest.mark.asyncio
async def test_ungrouped_single_sided_entity_has_no_findings_and_a_group_db_hint(tmp_path):
    root = tmp_path / "solo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "models.py").write_text(_PYDANTIC)
    (root / "app" / "svc.py").write_text(
        'def load_user(uid):\n    """Fetch one user.\n\n    @schema: user\n    """\n    return uid\n'
    )

    async with Client(mcp) as c:
        out = (await c.call_tool("index_project", {"workspace": str(root)})).data
        assert "error" not in out, out
        out = await _drift(c, root)

    assert out["scope"] == {"grouped": False, "projects": ["solo"]}
    assert out["findings"] == []
    assert out["counts"]["findings"] == 0
    assert out["counts"]["entities"] == 1
    assert out["counts"]["entities_with_drift"] == 0
    (row,) = out["entities"]
    assert [s["qualified_name"] for s in row["schemas"]] == ["app.models.User"]
    assert row["not_schemas"] == [
        {"qualified_name": "app.svc.load_user", "project": "solo", "file": "app/svc.py", "line": 1}
    ]
    assert "group_db" in out["hint"]


@pytest.mark.asyncio
async def test_no_bindings_gives_an_annotation_hint(tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 1\n")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(tmp_path)})
        out = await _drift(c, tmp_path)

    assert out["entities"] == [] and out["findings"] == []
    assert "@schema: <entity>" in out["hint"]


def _resource_json(res: Any) -> dict[str, Any]:
    text = res[0].text if isinstance(res, list) else res.contents[0].text
    return json.loads(text)


@pytest.mark.asyncio
async def test_schemas_resource_lists_entities_with_drift_counts(tmp_path):
    requires_grammar("typescript")
    reset_state()
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        # Resources bind to the most recently used workspace.
        summary = await _drift(c, back, summary_only=True)
        data = _resource_json(await c.read_resource("project://schemas"))

    assert data == summary
    (row,) = data["entities"]
    assert row["entity"] == "user"
    assert len(row["schemas"]) == 4
    assert sum(v for k, v in row["counts"].items() if k != "uncomparable") > 0


@pytest.mark.asyncio
async def test_entity_resource_holds_every_finding_unpaginated(tmp_path):
    requires_grammar("typescript")
    reset_state()
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        full = await _drift(c, back, entity="user")
        data = _resource_json(await c.read_resource("project://schemas/user"))
        missing = _resource_json(await c.read_resource("project://schemas/usr"))

    assert data["findings"] == full["findings"]
    assert len(data["findings"]) == 4
    assert data["next_cursor"] is None
    assert missing["isError"] is True and missing["did_you_mean"] == ["user"]
