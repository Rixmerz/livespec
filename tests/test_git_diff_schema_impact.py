"""v0.34 P7: the `schema_impact` section of `git_diff_impact`.

One test per scenario of the `schemas` requirement 'Schema impact of a diff',
over a real git repository grouped with a second one through a `group_db`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

from .conftest import requires_grammar

_MODELS = '''from pydantic import BaseModel


class User(BaseModel):
    """@schema: user"""

    id: int
    email: str
    avatar_url: str | None = None
'''

_SERVICE = """from app.models import User


def build_user(data):
    return User(**data)
"""

_OTHER = """def ping():
    return "pong"
"""

_TS_USER = """/** @schema: user */
export interface User {
  id: string;
  email: string;
  avatarUrl?: string;
}
"""

# The keys git_diff_impact returned before it knew about schemas.
_FULL_KEYS = {
    "base_ref",
    "head_ref",
    "affected_specs",
    "suggested_tests",
    "counts",
    "changed_files",
    "changed_files_indexed",
    "changed_files_unindexed",
    "changed_symbols",
    "impacted_callers",
    "next_cursor",
}
_COUNT_KEYS = {
    "changed_files",
    "changed_symbols",
    "impacted_callers",
    "affected_specs",
    "suggested_tests",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


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
            "app/other.py": _OTHER,
        },
    )
    _git(back, "init", "-q")
    _git(back, "config", "user.email", "test@example.com")
    _git(back, "config", "user.name", "test")
    _commit(back, "base")
    front = tmp_path / "front"
    _write(front, {".livespec.toml": toml, "src/user.ts": _TS_USER})
    return back, front


def _edit(repo: Path, rel: str, old: str, new: str) -> None:
    p = repo / rel
    text = p.read_text()
    assert old in text
    p.write_text(text.replace(old, new))


async def _index(c: Client, *repos: Path) -> None:
    for repo in repos:
        out = (await c.call_tool("index_project", {"workspace": str(repo)})).data
        assert "error" not in out, out


async def _diff(c: Client, repo: Path, **args: Any) -> dict[str, Any]:
    out = (await c.call_tool("git_diff_impact", {"workspace": str(repo), **args})).data
    assert "error" not in out, out
    return out


async def _committed_diff(tmp_path: Path, old: str, new: str, **args: Any) -> dict[str, Any]:
    requires_grammar("typescript")
    back, front = _group(tmp_path)
    _edit(back, "app/models.py", old, new)
    _commit(back, "change the user schema")
    async with Client(mcp) as c:
        await _index(c, front, back)
        return await _diff(c, back, **args)


@pytest.mark.asyncio
async def test_field_removed_in_a_commit(tmp_path):
    out = await _committed_diff(tmp_path, "    avatar_url: str | None = None\n", "")

    assert out["counts"]["schema_entities"] == 1
    (entry,) = out["schema_impact"]
    assert (entry["entity"], entry["entity_key"]) == ("user", "user")

    (changed,) = entry["changed"]
    assert (changed["qualified_name"], changed["project"], changed["file"]) == (
        "app.models.User",
        "back",
        "app/models.py",
    )
    assert changed["flavor"] == "pydantic"
    (fc,) = changed["field_changes"]
    assert (fc["kind"], fc["field"], fc["field_key"], fc["after"]) == (
        "removed",
        "avatar_url",
        "avatarurl",
        None,
    )
    assert fc["before"]["type"] == "string" and fc["before"]["nullable"] is True
    assert "field_changes_unknown" not in changed

    (ts,) = entry["declarations"]
    assert (ts["project"], ts["file"], ts["flavor"]) == ("front", "src/user.ts", "ts_interface")
    assert ts["actions"] == [{"field_key": "avatarurl", "change": "remove", "action": "remove"}]
    assert entry["follow_up"] == ["schema_impact(entity=user, change=remove, field=avatar_url)"]

    readers = entry["readers"]
    assert readers["counts"] == {"readers": 1, "endpoints": 0, "tests": 0, "symbols": 1}
    assert readers["by_project"] == [
        {"project": "back", "readers": 1, "endpoints": 0, "tests": 0, "symbols": 1}
    ]
    assert [r["qualified_name"] for r in readers["sample"]] == ["app.service.build_user"]


@pytest.mark.asyncio
async def test_retype_in_a_commit_suggests_a_call_that_names_the_new_type(tmp_path):
    out = await _committed_diff(tmp_path, "    id: int\n", "    id: str\n")

    (entry,) = out["schema_impact"]
    assert entry["follow_up"] == [
        "schema_impact(entity=user, change=retype, field=id, new_type=string)"
    ]


@pytest.mark.asyncio
async def test_rename_in_a_commit(tmp_path):
    out = await _committed_diff(tmp_path, "    email: str\n", "    contact_email: str\n")

    (entry,) = out["schema_impact"]
    (changed,) = entry["changed"]
    assert [(f["kind"], f["field"]) for f in changed["field_changes"]] == [
        ("removed", "email"),
        ("added", "contact_email"),
    ]
    (ts,) = entry["declarations"]
    assert ts["actions"] == [
        {"field_key": "email", "change": "remove", "action": "remove"},
        {"field_key": "contactemail", "change": "add", "action": "add"},
    ]
    assert entry["follow_up"] == [
        "schema_impact(entity=user, change=remove, field=email)",
        "schema_impact(entity=user, change=add, field=contact_email)",
    ]


@pytest.mark.asyncio
async def test_no_schema_touched(tmp_path):
    requires_grammar("typescript")
    back, front = _group(tmp_path)
    _edit(back, "app/other.py", '"pong"', '"pong!"')
    _commit(back, "touch a plain function")
    async with Client(mcp) as c:
        await _index(c, front, back)
        out = await _diff(c, back)
        summary = await _diff(c, back, summary_only=True)
        empty = await _diff(c, back, base_ref="HEAD", head_ref="HEAD")

    assert [s["qualified_name"] for s in out["changed_symbols"]] == ["app.other.ping"]
    assert set(out) == _FULL_KEYS
    assert set(out["counts"]) == _COUNT_KEYS
    assert "schema_impact" not in summary
    assert set(summary["counts"]) == _COUNT_KEYS
    assert "schema_impact" not in empty
    assert "schema_entities" not in empty["counts"]


@pytest.mark.asyncio
async def test_summary_only(tmp_path):
    out = await _committed_diff(
        tmp_path, "    avatar_url: str | None = None\n", "", summary_only=True
    )

    (entry,) = out["schema_impact"]
    assert out["counts"]["schema_entities"] == 1
    assert "sample" not in entry["readers"]
    assert entry["readers"]["counts"]["readers"] == 1
    assert [p["project"] for p in entry["readers"]["by_project"]] == ["back"]
    assert entry["changed"][0]["field_changes"][0]["kind"] == "removed"


@pytest.mark.asyncio
async def test_unparsable_head_blob_gives_unknown_field_changes(tmp_path):
    requires_grammar("typescript")
    back, front = _group(tmp_path)
    async with Client(mcp) as c:
        await _index(c, front, back)
        # Broken at head: the index keeps the last good symbols of the file,
        # so the declaration is still known while its fields cannot be read.
        _edit(back, "app/models.py", "    id: int\n", "    id: int (\n")
        _commit(back, "break the model")
        await _index(c, back)
        out = await _diff(c, back)

    (entry,) = out["schema_impact"]
    (changed,) = entry["changed"]
    assert changed["field_changes"] is None
    assert changed["field_changes_unknown"] == "parse error"
    (ts,) = entry["declarations"]
    assert ts["actions"] == []
    assert entry["follow_up"] == []
