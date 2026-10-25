"""v0.34 phase 3: `plan_schema_change`, the coordinated cross-repo edit.

Every test runs the tool through `Client(mcp)` over the three-repository
group in `tests/fixtures/schema_group/` (api: Pydantic, web: TS + Zod, db:
SQL), copied into `tmp_path` with a shared `group_db`."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

from .conftest import requires_grammar

FIXTURE = Path(__file__).parent / "fixtures" / "schema_group"
REPOS = ("api", "web", "db")

MODELS = "app/models.py"
USER_TS = "src/user.ts"
SQL = "migrations/001_users.sql"


def _group(tmp_path: Path) -> dict[str, Path]:
    shared = tmp_path / "grp" / "shared.db"
    roots: dict[str, Path] = {}
    for name in REPOS:
        root = tmp_path / name
        shutil.copytree(FIXTURE / name, root)
        (root / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{shared}"\n')
        roots[name] = root
    return roots


async def _index(c: Client, root: Path) -> None:
    out = (await c.call_tool("index_project", {"workspace": str(root)})).data
    assert "error" not in out, out


async def _indexed(c: Client, tmp_path: Path) -> dict[str, Path]:
    requires_grammar("typescript")
    roots = _group(tmp_path)
    for name in REPOS:
        await _index(c, roots[name])
    return roots


async def _plan(c: Client, ws: Path, **args: Any) -> dict[str, Any]:
    return (await c.call_tool("plan_schema_change", {"workspace": str(ws), **args})).data


def _decl(out: dict[str, Any], flavor: str) -> dict[str, Any]:
    (row,) = [d for d in out["declarations"] if d["flavor"] == flavor]
    return row


def _source(root: Path, rel: str) -> str:
    return (root / rel).read_text()


def _lines(text: str, start: int, end: int) -> str:
    return "".join(text.splitlines(keepends=True)[start - 1 : end])


@pytest.mark.asyncio
async def test_rename_plans_every_declaration_across_the_group(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        out = await _plan(
            c, roots["api"], entity="user", change="rename", field="email", new_name="contact_email"
        )

    assert "error" not in out, out
    assert out["mode"] == "plan"
    assert out["scope"] == {"grouped": True, "projects": ["api", "web", "db"]}
    assert [(d["project"], d["file"], d["flavor"]) for d in out["declarations"]] == [
        ("api", MODELS, "pydantic"),
        ("web", USER_TS, "ts_interface"),
        ("web", USER_TS, "zod"),
        ("db", SQL, "sql_table"),
    ]
    expected = {
        "pydantic": ("email: str", "contact_email: str"),
        "ts_interface": ("email: string;", "contactEmail: string;"),
        "zod": ("email: z.string(),", "contactEmail: z.string(),"),
        "sql_table": ("email TEXT NOT NULL,", "contact_email TEXT NOT NULL,"),
    }
    for d in out["declarations"]:
        assert (d["action"], d["status"], d["reason"]) == ("rename", "edit", None), d
        (edit,) = d["edits"]
        assert set(edit) == {"file", "start_line", "end_line", "old_text", "new_text"}
        root = roots[d["project"]]
        assert edit["file"] == d["file"]
        assert edit["old_text"] == _lines(
            _source(root, d["file"]), edit["start_line"], edit["end_line"]
        )
        old, new = expected[d["flavor"]]
        assert edit["old_text"].strip() == old
        assert edit["new_text"].strip() == new
    assert _decl(out, "sql_table")["migration_hint"] == (
        "ALTER TABLE users RENAME COLUMN email TO contact_email;"
    )
    assert all(d["migration_hint"] is None for d in out["declarations"][:3])
    assert out["counts"]["declarations"] == 4
    assert out["counts"]["to_change"] == 4
    assert out["counts"]["edits"] == 4
    assert out["counts"]["files"] == 3
    assert (out["counts"]["manual"], out["counts"]["stale"]) == (0, 0)
    assert "apply" not in out


@pytest.mark.asyncio
async def test_rename_through_a_pydantic_alias_rewrites_only_the_alias(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        out = await _plan(
            c,
            roots["api"],
            entity="user",
            change="rename",
            field="fullName",
            new_name="displayName",
        )

    py = _decl(out, "pydantic")
    assert (py["action"], py["status"]) == ("rename", "edit")
    assert py["field"]["name"] == "display"
    (edit,) = py["edits"]
    assert edit["old_text"] == '    display: str = Field(alias="fullName")\n'
    assert edit["new_text"] == '    display: str = Field(alias="displayName")\n'
    assert _decl(out, "ts_interface")["edits"][0]["new_text"].strip() == "displayName: string;"
    assert _decl(out, "zod")["edits"][0]["new_text"].strip() == "displayName: z.string(),"
    sql = _decl(out, "sql_table")
    assert sql["edits"][0]["new_text"].strip() == "display_name TEXT NOT NULL"
    assert sql["migration_hint"] == "ALTER TABLE users RENAME COLUMN full_name TO display_name;"


@pytest.mark.asyncio
async def test_retype_to_uuid_leaves_wire_strings_alone_and_imports_uuid(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        out = await _plan(
            c, roots["api"], entity="user", change="retype", field="id", new_type="uuid"
        )

    for flavor in ("ts_interface", "zod"):
        d = _decl(out, flavor)
        assert (d["action"], d["status"], d["edits"]) == ("compatible", "none", []), d
    py = _decl(out, "pydantic")
    assert (py["action"], py["status"]) == ("retype", "edit")
    imports, annotation = py["edits"]
    assert imports["old_text"] == "from pydantic import BaseModel, Field\n"
    assert imports["new_text"] == "from pydantic import BaseModel, Field\nfrom uuid import UUID\n"
    assert (annotation["old_text"], annotation["new_text"]) == ("    id: int\n", "    id: UUID\n")
    sql = _decl(out, "sql_table")
    assert sql["edits"][0]["new_text"].strip() == "id UUID PRIMARY KEY,"
    assert sql["migration_hint"] == "ALTER TABLE users ALTER COLUMN id TYPE UUID;"
    assert (out["counts"]["to_change"], out["counts"]["edits"], out["counts"]["files"]) == (2, 3, 2)


@pytest.mark.asyncio
async def test_optionality_makes_a_field_nullable_on_every_side(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        out = await _plan(
            c, roots["api"], entity="user", change="optionality", field="email", nullable=True
        )

    assert out["change"] == {
        "kind": "optionality",
        "field": "email",
        "field_key": "email",
        "new_name": None,
        "new_key": None,
        "new_type": None,
        "optional": None,
        "nullable": True,
    }
    expected = {
        "pydantic": "email: str | None",
        "ts_interface": "email: string | null;",
        "zod": "email: z.string().nullable(),",
        "sql_table": "email TEXT,",
    }
    for d in out["declarations"]:
        assert (d["action"], d["status"]) == ("set", "edit"), d
        assert d["edits"][0]["new_text"].strip() == expected[d["flavor"]]
    assert _decl(out, "sql_table")["migration_hint"] == (
        "ALTER TABLE users ALTER COLUMN email DROP NOT NULL;"
    )


@pytest.mark.asyncio
async def test_add_and_remove(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        added = await _plan(
            c, roots["api"], entity="user", change="add", field="avatar_url", new_type="string"
        )
        removed = await _plan(c, roots["api"], entity="user", change="remove", field="email")

    new_lines = {
        "pydantic": "avatar_url: str",
        "ts_interface": "avatarUrl: string;",
        "zod": "avatarUrl: z.string(),",
        "sql_table": "avatar_url TEXT NOT NULL",
    }
    for d in added["declarations"]:
        assert (d["action"], d["status"], d["field"]) == ("add", "edit", None), d
        (edit,) = d["edits"]
        # An insertion keeps its anchor line and adds the field after it.
        assert edit["new_text"].startswith(edit["old_text"].rstrip("\n"))
        assert edit["new_text"].splitlines()[-1].strip() == new_lines[d["flavor"]]
    assert _decl(added, "sql_table")["migration_hint"] == (
        "ALTER TABLE users ADD COLUMN avatar_url TEXT NOT NULL;"
        " -- NOT NULL needs a DEFAULT or a backfill on a table with rows"
    )

    for d in removed["declarations"]:
        assert (d["action"], d["status"]) == ("remove", "edit"), d
        (edit,) = d["edits"]
        assert edit["new_text"] == ""
        assert edit["start_line"] == edit["end_line"] == d["field"]["line"]
    assert _decl(removed, "sql_table")["migration_hint"] == "ALTER TABLE users DROP COLUMN email;"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"change": "add", "field": "avatar_url", "new_type": "string"},
        {"change": "remove", "field": "email"},
        {"change": "rename", "field": "email", "new_name": "contact_email"},
        {"change": "retype", "field": "id", "new_type": "uuid"},
    ],
    ids=["add", "remove", "rename", "retype"],
)
async def test_readers_are_schema_impacts(tmp_path, change):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        args = {"workspace": str(roots["api"]), "entity": "user", **change}
        plan = (await c.call_tool("plan_schema_change", args)).data
        impact = (await c.call_tool("schema_impact", args)).data

    assert "error" not in impact, impact
    assert plan["readers"] == impact["readers"]
    assert plan["readers"], "the fixture's call graph must reach the declarations"
    assert [r["project"] for r in plan["readers"]] == ["api", "api", "web"]
    for key in ("readers", "endpoints", "tests", "symbols"):
        assert plan["counts"][key] == impact["counts"][key], key
    assert [d["action"] for d in plan["declarations"]] == [
        d["action"] for d in impact["declarations"]
    ]


def _snapshot(roots: dict[str, Path]) -> dict[Path, bytes]:
    return {
        p: p.read_bytes()
        for root in roots.values()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".livespec" not in p.parts
    }


@pytest.mark.asyncio
async def test_plan_mode_writes_nothing(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        before = _snapshot(roots)
        for change in (
            {"change": "add", "field": "avatar_url", "new_type": "string"},
            {"change": "remove", "field": "email"},
            {"change": "rename", "field": "email", "new_name": "contact_email"},
            {"change": "retype", "field": "id", "new_type": "uuid"},
            {"change": "optionality", "field": "email", "nullable": True},
        ):
            out = await _plan(c, roots["api"], entity="user", **change)
            assert out["mode"] == "plan" and out["counts"]["edits"] > 0, out

    assert _snapshot(roots) == before
    assert any(p.name == "models.py" for p in before), "the snapshot must cover the fixture"


@pytest.mark.asyncio
async def test_summary_only_and_pagination_keep_exact_counts(tmp_path):
    rename = {"entity": "user", "change": "rename", "field": "email", "new_name": "contact_email"}
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        full = await _plan(c, roots["api"], **rename)
        first = await _plan(c, roots["api"], **rename, limit=2)
        rest = await _plan(c, roots["api"], **rename, limit=2, cursor=2)
        summary = await _plan(c, roots["api"], **rename, summary_only=True)

    assert full["counts"]["readers"] == 3
    assert (first["next_cursor"], rest["next_cursor"], full["next_cursor"]) == (2, None, None)
    assert first["readers"] + rest["readers"] == full["readers"]
    assert "readers" not in summary and summary["next_cursor"] is None
    for page in (first, rest, summary):
        assert page["counts"] == full["counts"]
        assert page["by_project"] == full["by_project"]
        # Declarations are never paged.
        assert page["declarations"] == full["declarations"]
    assert [p["project"] for p in full["by_project"]] == ["api", "web", "db"]
    api = full["by_project"][0]
    assert (api["declarations"], api["to_change"], api["edits"], api["readers"]) == (1, 1, 1, 2)


# ---------- apply ----------

RENAME = {"entity": "user", "change": "rename", "field": "email", "new_name": "contact_email"}
_ERROR_KEYS = {"error", "isError", "did_you_mean", "hint"}


def _applied(text: str, edits: list[dict[str, Any]]) -> str:
    lines = text.splitlines(keepends=True)
    for e in sorted(edits, key=lambda e: e["start_line"], reverse=True):
        lines[e["start_line"] - 1 : e["end_line"]] = [e["new_text"]]
    return "".join(lines)


@pytest.mark.asyncio
async def test_apply_writes_only_inside_apply_workspaces(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        before = _snapshot(roots)
        plan = await _plan(c, roots["api"], **RENAME)
        out = await _plan(
            c,
            roots["api"],
            **RENAME,
            apply=True,
            apply_workspaces=[str(roots["api"]), str(roots["web"])],
        )
        assert "error" not in out, out
        after = _snapshot(roots)
        for name in ("api", "web"):
            await _index(c, roots[name])
        again = await _plan(c, roots["api"], **RENAME)

    assert out["mode"] == "apply"
    assert out["apply"]["written"] == [
        {"project": "api", "file": MODELS, "edits": 1},
        {"project": "web", "file": USER_TS, "edits": 2},
    ]
    assert out["apply"]["not_applied"] == [
        {
            "project": "db",
            "file": SQL,
            "qualified_name": _decl(plan, "sql_table")["qualified_name"],
            "reason": "outside apply_workspaces",
        }
    ]
    assert out["apply"]["reindex"] == [str(roots["api"]), str(roots["web"])]

    written = {roots["api"] / MODELS, roots["web"] / USER_TS}
    assert {p for p in before if before[p] != after[p]} == written
    for name, rel in (("api", MODELS), ("web", USER_TS)):
        edits = [e for d in plan["declarations"] if d["project"] == name for e in d["edits"]]
        assert after[roots[name] / rel].decode() == _applied(
            before[roots[name] / rel].decode(), edits
        )
    assert "contact_email: str" in after[roots["api"] / MODELS].decode()
    assert after[roots["web"] / USER_TS].decode().count("contactEmail") == 2
    # The migration hint is text for a person, never written.
    assert after[roots["db"] / SQL] == before[roots["db"] / SQL]

    for d in again["declarations"]:
        if d["project"] == "db":
            assert (d["action"], d["status"]) == ("rename", "edit"), d
        else:
            assert (d["action"], d["status"], d["edits"]) == ("renamed", "none", []), d


@pytest.mark.asyncio
async def test_apply_defaults_to_the_calling_workspace(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        before = _snapshot(roots)
        out = await _plan(c, roots["web"], **RENAME, apply=True)
        after = _snapshot(roots)

    assert "error" not in out, out
    assert out["apply"]["written"] == [{"project": "web", "file": USER_TS, "edits": 2}]
    assert [(n["project"], n["reason"]) for n in out["apply"]["not_applied"]] == [
        ("api", "outside apply_workspaces"),
        ("db", "outside apply_workspaces"),
    ]
    assert {p for p in before if before[p] != after[p]} == {roots["web"] / USER_TS}


@pytest.mark.asyncio
async def test_apply_lists_manual_declarations_and_writes_the_rest(tmp_path):
    optional = {"entity": "user", "change": "optionality", "field": "email", "optional": True}
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        before = _snapshot(roots)
        out = await _plan(
            c,
            roots["api"],
            **optional,
            apply=True,
            apply_workspaces=[str(roots["api"]), str(roots["web"])],
        )
        after = _snapshot(roots)

    assert "error" not in out, out
    py = _decl(out, "pydantic")
    assert py["status"] == "manual" and py["reason"], py
    # A column's "optional" is not a payload's: SQL is never edited for it.
    assert (_decl(out, "sql_table")["action"], _decl(out, "sql_table")["status"]) == (
        "unchanged",
        "none",
    )
    assert out["apply"]["not_applied"] == [
        {
            "project": "api",
            "file": MODELS,
            "qualified_name": py["qualified_name"],
            "reason": py["reason"],
        }
    ]
    assert out["apply"]["written"] == [{"project": "web", "file": USER_TS, "edits": 2}]
    assert {p for p in before if before[p] != after[p]} == {roots["web"] / USER_TS}
    text = after[roots["web"] / USER_TS].decode()
    assert "email?: string;" in text and "email: z.string().optional()," in text


@pytest.mark.asyncio
async def test_apply_keeps_file_mode_and_line_endings(tmp_path):
    requires_grammar("typescript")
    roots = _group(tmp_path)
    sql = roots["db"] / SQL
    sql.write_bytes(sql.read_bytes().replace(b"\n", b"\r\n"))
    sql.chmod(0o640)
    async with Client(mcp) as c:
        for name in REPOS:
            await _index(c, roots[name])
        out = await _plan(c, roots["db"], **RENAME, apply=True)

    assert "error" not in out, out
    assert out["apply"]["written"] == [{"project": "db", "file": SQL, "edits": 1}]
    raw = sql.read_bytes()
    assert b"  contact_email TEXT NOT NULL,\r\n" in raw
    assert raw.count(b"\r\n") == raw.count(b"\n") == 6
    assert sql.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in sql.parent.iterdir()] == ["001_users.sql"], "no temp file left"


@pytest.mark.asyncio
async def test_a_target_edited_after_indexing_refuses_the_whole_apply(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        ts = roots["web"] / USER_TS
        ts.write_text(ts.read_text() + "// edited after indexing\n")
        before = _snapshot(roots)
        plan = await _plan(c, roots["api"], **RENAME)
        out = await _plan(
            c,
            roots["api"],
            **RENAME,
            apply=True,
            apply_workspaces=[str(roots["api"]), str(roots["web"])],
        )
        after = _snapshot(roots)

    for flavor in ("ts_interface", "zod"):
        d = _decl(plan, flavor)
        assert (d["status"], d["reason"], d["edits"]) == ("stale", "changed since indexing", [])
    assert _decl(plan, "pydantic")["status"] == "edit"
    assert plan["counts"]["stale"] == 2
    assert f"re-run index_project on {roots['web']}" in plan["hint"]

    assert out["isError"] is True and set(out) <= _ERROR_KEYS, out
    assert "Nothing was written" in out["error"] and USER_TS in out["error"]
    assert after == before


@pytest.mark.asyncio
async def test_a_stale_target_outside_apply_workspaces_does_not_refuse(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        sql = roots["db"] / SQL
        sql.write_text(sql.read_text() + "-- edited after indexing\n")
        out = await _plan(c, roots["api"], **RENAME, apply=True)

    assert "error" not in out, out
    assert _decl(out, "sql_table")["status"] == "stale"
    assert out["apply"]["written"] == [{"project": "api", "file": MODELS, "edits": 1}]
    assert {n["project"] for n in out["apply"]["not_applied"]} == {"web", "db"}


@pytest.mark.asyncio
async def test_a_declaration_reached_through_a_symlink_outside_its_root_is_refused(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        models = roots["api"] / MODELS
        outside = tmp_path / "elsewhere" / "models.py"
        outside.parent.mkdir()
        outside.write_bytes(models.read_bytes())
        models.unlink()
        # Same bytes, so the hash still matches: only the path gives it away.
        models.symlink_to(outside)
        before = outside.read_bytes()
        plan = await _plan(c, roots["api"], **RENAME)
        out = await _plan(c, roots["api"], **RENAME, apply=True)

    py = _decl(plan, "pydantic")
    assert (py["status"], py["reason"], py["edits"]) == (
        "stale",
        "resolves outside its project root",
        [],
    )
    assert out["isError"] is True and set(out) <= _ERROR_KEYS, out
    assert "resolves outside its project root" in out["error"]
    assert outside.read_bytes() == before
    assert models.is_symlink()


@pytest.mark.asyncio
async def test_an_apply_workspace_outside_the_group_is_an_error(tmp_path):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        stranger = tmp_path / "stranger"
        stranger.mkdir()
        before = _snapshot(roots)
        out = await _plan(c, roots["api"], **RENAME, apply=True, apply_workspaces=[str(stranger)])

    assert out["isError"] is True and set(out) <= _ERROR_KEYS, out
    assert str(stranger) in out["error"]
    assert out["did_you_mean"] == [str(roots[n]) for n in REPOS]
    assert _snapshot(roots) == before


@pytest.mark.asyncio
async def test_a_write_failure_names_the_files_already_written(tmp_path, monkeypatch):
    from livespec_mcp.domain import schema_plan

    real = schema_plan._write_bytes
    calls: list[Path] = []

    def fail_second(target: Path, data: bytes) -> None:
        calls.append(target)
        if len(calls) == 2:
            raise PermissionError(13, "Permission denied")
        real(target, data)

    monkeypatch.setattr(schema_plan, "_write_bytes", fail_second)
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        out = await _plan(
            c,
            roots["api"],
            **RENAME,
            apply=True,
            apply_workspaces=[str(roots["api"]), str(roots["web"])],
        )

    assert out["isError"] is True and set(out) <= _ERROR_KEYS, out
    assert f"web:{USER_TS}" in out["error"] and "Permission denied" in out["error"]
    assert f"already written: api:{MODELS}" in out["error"]
    assert "contact_email" in (roots["api"] / MODELS).read_text()
    assert "contactEmail" not in (roots["web"] / USER_TS).read_text()


@pytest.mark.asyncio
async def test_a_file_changed_between_check_and_write_is_not_overwritten(tmp_path, monkeypatch):
    from livespec_mcp.domain import schema_plan

    real = schema_plan._write_bytes
    roots: dict[str, Path] = {}

    def touch_web_after_first_write(target: Path, data: bytes) -> None:
        real(target, data)
        ts = roots["web"] / USER_TS
        if b"// raced" not in ts.read_bytes():
            ts.write_text(ts.read_text() + "// raced\n")

    monkeypatch.setattr(schema_plan, "_write_bytes", touch_web_after_first_write)
    async with Client(mcp) as c:
        roots.update(await _indexed(c, tmp_path))
        out = await _plan(
            c,
            roots["api"],
            **RENAME,
            apply=True,
            apply_workspaces=[str(roots["api"]), str(roots["web"])],
        )

    assert out["isError"] is True and set(out) <= _ERROR_KEYS, out
    assert f"web:{USER_TS} (changed since indexing)" in out["error"]
    assert f"already written: api:{MODELS}" in out["error"]
    assert "contactEmail" not in (roots["web"] / USER_TS).read_text()


# ---------- validation ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "error", "did_you_mean", "hint"),
    [
        (
            {"entity": "usr", "change": "remove", "field": "email"},
            "Entity 'usr' is not bound",
            ["user"],
            "@schema: <entity>",
        ),
        (
            {"entity": "user", "change": "drop", "field": "email"},
            "Unknown change 'drop'",
            ["add", "remove", "rename", "retype", "optionality"],
            None,
        ),
        (
            {"entity": "user", "change": "rename", "field": "email"},
            "rename needs new_name",
            None,
            None,
        ),
        (
            {"entity": "user", "change": "retype", "field": "id"},
            "retype needs new_type",
            None,
            None,
        ),
        (
            {"entity": "user", "change": "remove", "field": "emial"},
            "holds field 'emial'",
            ["email"],
            None,
        ),
        (
            {"entity": "user", "change": "add", "field": "avatar_url"},
            "add needs new_type",
            None,
            None,
        ),
        (
            {"entity": "user", "change": "add", "field": "tags", "new_type": "list"},
            "new_type 'list' cannot be written",
            None,
            "hand edit",
        ),
        (
            {"entity": "user", "change": "retype", "field": "id", "new_type": "uuuid"},
            "new_type 'uuuid' cannot be written",
            ["uuid"],
            "schema_impact",
        ),
        (
            {"entity": "user", "change": "optionality", "field": "email"},
            "optionality needs optional, nullable or both",
            None,
            None,
        ),
        (
            {"entity": "user", "change": "optionality", "field": "emial", "nullable": True},
            "holds field 'emial'",
            ["email"],
            None,
        ),
        (
            {
                "entity": "user",
                "change": "rename",
                "field": "email",
                "new_name": "x",
                "nullable": True,
            },
            "optional and nullable do not apply to rename",
            None,
            None,
        ),
        (
            {"entity": "user", "change": "remove", "field": "email", "optional": False},
            "optional and nullable do not apply to remove",
            None,
            None,
        ),
        (
            {
                "entity": "user",
                "change": "retype",
                "field": "id",
                "new_type": "uuid",
                "nullable": True,
            },
            "optional and nullable do not apply to retype",
            None,
            None,
        ),
        (
            {"entity": "user", "change": "remove", "field": "email", "apply_workspaces": ["."]},
            "apply_workspaces needs apply=True",
            None,
            "plan first",
        ),
    ],
    ids=[
        "unknown-entity",
        "unknown-change",
        "rename-without-new-name",
        "retype-without-new-type",
        "unknown-field",
        "add-without-new-type",
        "unrenderable-type",
        "unknown-type",
        "optionality-without-flags",
        "optionality-unknown-field",
        "flags-with-rename",
        "flags-with-remove",
        "flags-with-retype",
        "apply-workspaces-without-apply",
    ],
)
async def test_validation_errors_have_the_mcp_error_shape(
    tmp_path, args, error, did_you_mean, hint
):
    async with Client(mcp) as c:
        roots = await _indexed(c, tmp_path)
        before = _snapshot(roots)
        out = await _plan(c, roots["api"], **args)

    assert out["isError"] is True and set(out) <= _ERROR_KEYS, out
    assert error in out["error"], out
    assert out.get("did_you_mean") == did_you_mean
    if hint is None:
        assert "hint" not in out
    else:
        assert hint in out["hint"]
    assert _snapshot(roots) == before
