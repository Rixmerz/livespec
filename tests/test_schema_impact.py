"""v0.34 P7: `schema_impact`, the cross-repo impact of one proposed field change.

One test per scenario of the `schemas` requirement 'Cross-repo schema impact',
over a two-repository `group_db`, plus units for the action table."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from livespec_mcp.domain.schema_drift import SchemaField, SchemaSide, SymbolRef
from livespec_mcp.domain.schema_impact import FieldChange, declaration_action
from livespec_mcp.domain.schema_types import name_key
from livespec_mcp.server import mcp

from .conftest import requires_grammar

# ---------- action table ----------


def _f(name: str, type_: str = "string", *, key: str | None = None, item: str | None = None):
    return SchemaField(
        name=name,
        field_key=key or name_key(name),
        type=type_,
        item_type=item,
        ref_target=None,
        raw_type=None,
        optional=False,
        nullable=False,
        line=1,
    )


def _side(flavor: str, *fields: SchemaField) -> SchemaSide:
    return SchemaSide(ref=SymbolRef("m.User", "p", "m.src", 1), flavor=flavor, fields=fields)


def _action(side: SchemaSide, kind: str, field: str, **kw: Any) -> str:
    return declaration_action(side, FieldChange(kind, field, **kw))[0]


def test_add_action_is_add_or_present():
    assert _action(_side("pydantic", _f("id")), "add", "email") == "add"
    assert _action(_side("ts_interface", _f("emailAddress")), "add", "email_address") == "present"


def test_remove_action_is_remove_or_absent():
    assert _action(_side("pydantic", _f("avatar_url")), "remove", "avatar_url") == "remove"
    assert _action(_side("sql_table", _f("id")), "remove", "avatar_url") == "absent"


def test_rename_actions_cover_every_side_state():
    def rename(side: SchemaSide) -> str:
        return _action(side, "rename", "email", new_name="contact_email")

    assert rename(_side("pydantic", _f("email"))) == "rename"
    assert rename(_side("pydantic", _f("email"), _f("contact_email"))) == "both_present"
    assert rename(_side("ts_interface", _f("contactEmail"))) == "renamed"
    assert rename(_side("sql_table", _f("id"))) == "absent"


def test_renamed_side_reports_the_field_holding_the_new_name():
    side = _side("ts_interface", _f("contactEmail"))
    action, field = declaration_action(
        side, FieldChange("rename", "email", new_name="contactEmail")
    )
    assert action == "renamed"
    assert field is not None and field.name == "contactEmail"


def test_retype_compatible_follows_the_declarations_own_flavor():
    def retype(side: SchemaSide, new_type: str) -> str:
        return _action(side, "retype", "id", new_type=new_type)

    # A JSON payload carries a UUID as a string: nothing to change there.
    assert retype(_side("ts_interface", _f("id", "string")), "uuid") == "compatible"
    assert retype(_side("pydantic", _f("id", "int")), "uuid") == "retype"
    # Python keeps int and float apart, whatever a wire schema would accept.
    assert retype(_side("pydantic", _f("id", "int")), "float") == "retype"
    # A Zod uuid validates more than a plain string does: widening it is a change.
    assert retype(_side("zod", _f("id", "uuid")), "string") == "retype"
    assert retype(_side("pydantic", _f("id", "int")), "int") == "compatible"
    # The neutral type names no item type: a list already is a list.
    tags = _side("pydantic", _f("tags", "list", item="string"))
    assert _action(tags, "retype", "tags", new_type="list") == "compatible"
    assert retype(_side("pydantic", _f("name")), "uuid") == "absent"


def test_field_matches_by_its_key_or_its_name_as_written():
    # A Python attribute whose wire name differs: both spellings find it.
    aliased = _side("pydantic", _f("display", key="fullname"))
    assert _action(aliased, "remove", "display") == "remove"
    assert _action(aliased, "remove", "fullName") == "remove"


# ---------- two-repository group ----------

_MODELS = '''from pydantic import BaseModel


class User(BaseModel):
    """@schema: user

    @spec:accounts-user-profile
    """

    id: int
    email: str
    avatar_url: str | None = None
'''

_SERVICE = """from app.models import User


def build_user(data):
    return User(**data)
"""

_API = """from fastapi import FastAPI

from app.service import build_user

app = FastAPI()


@app.get("/api/users/{user_id}")
def get_user(user_id):
    return build_user({"id": user_id})
"""

_AUDIT = '''def log_user_change(payload):
    """@schema: user"""
    return payload
'''

_PROFILE = '''def render_profile(user_id):
    """@spec:accounts-user-profile"""
    return {"id": user_id}
'''

_TEST = """from app.models import User


def test_user_has_email():
    assert User(id=1, email="a@b.c").email == "a@b.c"
"""

_SQL = """-- @schema: user
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL
);

-- @schema: audit_log
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  action TEXT NOT NULL
);
"""

_SPEC = """# accounts

### Requirement: User profile

#### Scenario: show a profile
- **WHEN** a profile is shown
- **THEN** it names the user
"""

_TS_USER = """/** @schema: user */
export interface User {
  id: string;
  contactEmail: string;
  avatarUrl?: string;
}
"""

_TS_API = """export async function loadUser() {
  return fetch('/api/users/42');
}
"""

_TS_AUDIT = """/** @schema: audit_log */
export interface AuditLog {
  id: number;
  action: string;
}
"""


def _line(text: str, needle: str) -> int:
    return next(i for i, ln in enumerate(text.splitlines(), 1) if needle in ln)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _group(tmp_path: Path) -> tuple[Path, Path]:
    shared = tmp_path / "grp" / "shared.db"
    toml = f'[workspace]\ngroup_db = "{shared}"\n'
    back = tmp_path / "back"
    _write(
        back,
        {
            ".livespec.toml": toml,
            "app/__init__.py": "",
            "app/models.py": _MODELS,
            "app/service.py": _SERVICE,
            "app/api.py": _API,
            "app/audit.py": _AUDIT,
            "app/profile.py": _PROFILE,
            "tests/test_users.py": _TEST,
            "migrations/001.sql": _SQL,
            "openspec/specs/accounts/spec.md": _SPEC,
        },
    )
    front = tmp_path / "front"
    _write(
        front,
        {
            ".livespec.toml": toml,
            "src/user.ts": _TS_USER,
            "src/api.ts": _TS_API,
            "src/audit.ts": _TS_AUDIT,
        },
    )
    return back, front


async def _indexed_group(c: Client, tmp_path: Path) -> tuple[Path, Path]:
    back, front = _group(tmp_path)
    for repo in (front, back):
        out = (await c.call_tool("index_project", {"workspace": str(repo)})).data
        assert "error" not in out, out
    synced = (await c.call_tool("sync_openspec", {"workspace": str(back)})).data
    assert "error" not in synced, synced
    # `@spec:` annotations link at index time, and the Spec exists only now.
    out = (await c.call_tool("index_project", {"workspace": str(back), "force": True})).data
    assert "error" not in out, out
    return back, front


async def _impact(c: Client, ws: Path, **args: Any) -> dict[str, Any]:
    return (await c.call_tool("schema_impact", {"workspace": str(ws), **args})).data


async def _remove_avatar(tmp_path: Path, **args: Any) -> dict[str, Any]:
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _impact(c, back, entity="user", change="remove", field="avatar_url", **args)
    assert "error" not in out, out
    return out


def _reader(out: dict[str, Any], suffix: str) -> dict[str, Any]:
    (row,) = [r for r in out["readers"] if r["qualified_name"].endswith(suffix)]
    return row


@pytest.mark.asyncio
async def test_remove_across_repositories(tmp_path):
    out = await _remove_avatar(tmp_path)

    assert out["scope"] == {"grouped": True, "projects": ["back", "front"]}
    assert out["change"] == {
        "kind": "remove",
        "field": "avatar_url",
        "field_key": "avatarurl",
        "new_name": None,
        "new_key": None,
        "new_type": None,
    }
    assert [(d["project"], d["file"], d["flavor"], d["action"]) for d in out["declarations"]] == [
        ("back", "app/models.py", "pydantic", "remove"),
        ("back", "migrations/001.sql", "sql_table", "absent"),
        ("front", "src/user.ts", "ts_interface", "remove"),
    ]
    py, sql, ts = out["declarations"]
    assert (py["field"]["name"], py["field"]["line"]) == (
        "avatar_url",
        _line(_MODELS, "avatar_url"),
    )
    assert (ts["field"]["name"], ts["field"]["line"]) == ("avatarUrl", _line(_TS_USER, "avatarUrl"))
    assert sql["field"] is None
    assert out["counts"]["declarations"] == 3
    assert out["counts"]["to_change"] == 2
    assert out["counts"]["both_present"] == 0


@pytest.mark.asyncio
async def test_reader_through_the_call_graph(tmp_path):
    out = await _remove_avatar(tmp_path)

    builder = _reader(out, "build_user")
    assert (builder["via"], builder["depth"], builder["role"]) == (["graph"], 1, "symbol")
    assert (builder["project"], builder["file"]) == ("back", "app/service.py")
    assert builder["line"] == _line(_SERVICE, "def build_user")
    # Only the Pydantic model is constructed; the TS interface has no call edge.
    assert builder["reads"] == ["app.models.User"]

    handler = _reader(out, "get_user")
    assert (handler["via"], handler["depth"], handler["role"]) == (["graph"], 2, "endpoint")
    assert (handler["http_method"], handler["http_path"]) == ("GET", "/api/users/{user_id}")


@pytest.mark.asyncio
async def test_reader_across_a_route(tmp_path):
    out = await _remove_avatar(tmp_path)

    caller = _reader(out, "loadUser")
    assert (caller["project"], caller["file"]) == ("front", "src/api.ts")
    assert (caller["via"], caller["depth"], caller["role"]) == (["graph"], 3, "symbol")
    assert caller["http_method"] is None and caller["http_path"] is None
    assert [p["project"] for p in out["by_project"]] == ["back", "front"]
    front = out["by_project"][1]
    assert (front["declarations"], front["to_change"], front["readers"]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_test_reader(tmp_path):
    out = await _remove_avatar(tmp_path)

    test = _reader(out, "test_user_has_email")
    assert (test["file"], test["role"], test["via"], test["depth"]) == (
        "tests/test_users.py",
        "test",
        ["graph"],
        1,
    )


@pytest.mark.asyncio
async def test_binding_and_spec_readers(tmp_path):
    out = await _remove_avatar(tmp_path)

    bound = _reader(out, "log_user_change")
    assert (bound["via"], bound["depth"], bound["reads"]) == (["schema_binding"], None, [])

    shared = _reader(out, "render_profile")
    assert shared["via"] == ["spec:accounts-user-profile"]
    assert (shared["depth"], shared["reads"]) == (None, ["app.models.User"])

    # Ordered by project, then file and line; never a declaration of the entity.
    assert [(r["project"], r["file"]) for r in out["readers"]] == [
        ("back", "app/api.py"),
        ("back", "app/audit.py"),
        ("back", "app/profile.py"),
        ("back", "app/service.py"),
        ("back", "tests/test_users.py"),
        ("front", "src/api.ts"),
    ]
    readers = {k: out["counts"][k] for k in ("readers", "endpoints", "tests", "symbols")}
    assert readers == {"readers": 6, "endpoints": 1, "tests": 1, "symbols": 4}
    assert "hint" not in out


@pytest.mark.asyncio
async def test_rename_already_applied_on_one_side(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _impact(
            c, back, entity="user", change="rename", field="email", new_name="contact_email"
        )

    assert "error" not in out, out
    assert [(d["flavor"], d["action"]) for d in out["declarations"]] == [
        ("pydantic", "rename"),
        ("sql_table", "rename"),
        ("ts_interface", "renamed"),
    ]
    assert out["declarations"][2]["field"]["name"] == "contactEmail"
    assert (out["change"]["new_name"], out["change"]["new_key"]) == (
        "contact_email",
        "contactemail",
    )
    assert out["counts"]["to_change"] == 2


@pytest.mark.asyncio
async def test_retype_carried_by_the_wire_type(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _impact(c, back, entity="user", change="retype", field="id", new_type="uuid")

    assert "error" not in out, out
    assert [(d["flavor"], d["action"], d["field"]["type"]) for d in out["declarations"]] == [
        ("pydantic", "retype", "int"),
        ("sql_table", "retype", "int"),
        ("ts_interface", "compatible", "string"),
    ]


@pytest.mark.asyncio
async def test_unknown_field(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _impact(c, back, entity="user", change="remove", field="avatar")
        added = await _impact(c, back, entity="user", change="add", field="avatar")

    assert out["isError"] is True
    assert "avatar" in out["error"]
    assert out["did_you_mean"] == ["avatarUrl", "avatar_url"]
    # Adding a field nobody holds yet is the point of `add`.
    assert "error" not in added, added


@pytest.mark.asyncio
async def test_type_outside_the_neutral_types(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _impact(c, back, entity="user", change="retype", field="id", new_type="varchar")
        close = await _impact(c, back, entity="user", change="retype", field="id", new_type="uid")

    assert out["isError"] is True
    assert "varchar" in out["error"]
    assert close["isError"] is True and "uuid" in close["did_you_mean"]


@pytest.mark.asyncio
async def test_no_call_graph_reader(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        out = await _impact(c, back, entity="audit_log", change="add", field="note")

    assert "error" not in out, out
    assert [(d["flavor"], d["action"]) for d in out["declarations"]] == [
        ("sql_table", "add"),
        ("ts_interface", "add"),
    ]
    assert out["readers"] == []
    assert "type positions" in out["hint"]
    assert "grep_in_indexed_files" in out["hint"]


@pytest.mark.asyncio
async def test_pagination(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        full = await _impact(c, back, entity="user", change="remove", field="avatar_url")
        pages: list[dict[str, Any]] = []
        cursor: int | None = 0
        while cursor is not None:
            page = await _impact(
                c, back, entity="user", change="remove", field="avatar_url", limit=4, cursor=cursor
            )
            pages.append(page)
            cursor = page["next_cursor"]
        summary = await _impact(
            c, back, entity="user", change="remove", field="avatar_url", summary_only=True
        )

    assert [len(p["readers"]) for p in pages] == [4, 2]
    assert pages[0]["next_cursor"] == 4
    assert [r for p in pages for r in p["readers"]] == full["readers"]
    assert all(p["counts"] == full["counts"] for p in pages)
    assert "readers" not in summary
    assert summary["declarations"] == full["declarations"]
    assert summary["counts"] == full["counts"]
    assert summary["next_cursor"] is None


# ---------- errors and scope ----------


@pytest.mark.asyncio
async def test_unknown_entity_and_change_kind_are_mcp_errors(tmp_path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        entity = await _impact(c, back, entity="usr", change="remove", field="email")
        kind = await _impact(c, back, entity="user", change="drop", field="email")
        rename = await _impact(c, back, entity="user", change="rename", field="email")
        retype = await _impact(c, back, entity="user", change="retype", field="id")

    assert set(entity) == {"error", "isError", "did_you_mean", "hint"}
    assert entity["did_you_mean"] == ["user"]
    assert "@schema: <entity>" in entity["hint"]
    assert kind["isError"] is True
    assert kind["did_you_mean"] == ["add", "remove", "rename", "retype"]
    assert rename["isError"] is True and "new_name" in rename["error"]
    assert retype["isError"] is True and "new_type" in retype["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [-1, 51])
async def test_max_depth_outside_the_call_graph_bounds_is_rejected(tmp_path, depth):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        back, _ = await _indexed_group(c, tmp_path)
        with pytest.raises(ToolError, match="max_depth"):
            await c.call_tool(
                "schema_impact",
                {
                    "workspace": str(back),
                    "entity": "user",
                    "change": "remove",
                    "field": "email",
                    "max_depth": depth,
                },
            )


@pytest.mark.asyncio
async def test_ungrouped_workspace_says_other_repositories_are_not_listed(tmp_path):
    repo = tmp_path / "solo"
    _write(repo, {"app/__init__.py": "", "app/models.py": _MODELS, "app/service.py": _SERVICE})
    async with Client(mcp) as c:
        out = (await c.call_tool("index_project", {"workspace": str(repo)})).data
        assert "error" not in out, out
        out = await _impact(c, repo, entity="user", change="remove", field="avatar_url")

    assert "error" not in out, out
    assert out["scope"] == {"grouped": False, "projects": ["solo"]}
    assert [r["qualified_name"] for r in out["readers"]] == ["app.service.build_user"]
    assert "group_db" in out["hint"]
