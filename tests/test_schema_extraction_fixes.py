"""Phase-1 schema extraction fixes (OpenSpec change virtual-schema-first).

A backslash-escaped quote in a SQL string literal must not swallow the rest
of the file, and a Pydantic `Field(alias=...)` is the wire name a field is
matched by.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from livespec_mcp.domain.extractors import extract
from livespec_mcp.domain.sql_schema import extract_sql_schemas
from livespec_mcp.server import mcp

from .conftest import requires_grammar


def _tables(source: str) -> dict[str, list[str]]:
    r = extract_sql_schemas(source, "m")
    return {s.src_qname: [f.name for f in s.fields] for s in r.schemas}


def test_sql_backslash_escaped_quote_keeps_later_tables():
    src = (
        r"CREATE TABLE a (id INT, note TEXT DEFAULT 'it\'s');" + "\n"
        "CREATE TABLE b (id INT);\n"
        "CREATE TABLE c (id INT);\n"
    )
    assert _tables(src) == {"m.a": ["id", "note"], "m.b": ["id"], "m.c": ["id"]}


def test_sql_doubled_quote_still_escapes():
    src = "CREATE TABLE a (id INT, note TEXT DEFAULT 'it''s');\nCREATE TABLE b (id INT);\n"
    assert _tables(src) == {"m.a": ["id", "note"], "m.b": ["id"]}


def test_sql_unclosed_escape_reading_falls_back_to_standard():
    # Standard SQL: the backslash is a plain character and the quote after it
    # closes the literal. Read as an escape, the literal would never close.
    src = r"CREATE TABLE p (id INT, path TEXT DEFAULT 'C:\', n INT);" + "\n"
    assert _tables(src) == {"m.p": ["id", "path", "n"]}


_ALIASED = """from typing import Annotated

from pydantic import BaseModel, Field


class User(BaseModel):
    display: str = Field(alias="fullName")
    nick: Annotated[str, Field(alias="nickName")]
    user_id: int
"""


def test_pydantic_field_alias_is_the_match_key():
    (schema,) = extract(Path("m.py"), _ALIASED, Path("."))[1].schemas
    assert [(f.name, f.field_key) for f in schema.fields] == [
        ("display", "fullname"),
        ("nick", "nickname"),
        ("user_id", "userid"),
    ]
    by_name = {f.name: f for f in schema.fields}
    assert (by_name["display"].type, by_name["display"].optional) == ("string", False)
    assert (by_name["nick"].type, by_name["nick"].optional) == ("string", False)


_BACKEND = '''from pydantic import BaseModel, Field


class User(BaseModel):
    """A user as the API returns it.

    @schema: user
    """

    id: int
    display: str = Field(alias="fullName")
'''

_FRONTEND = """/** @schema: user */
export interface User {
  id: number;
  fullName: string;
}
"""


def _aliased_group(tmp_path: Path) -> tuple[Path, Path]:
    toml = f'[workspace]\ngroup_db = "{tmp_path / "grp" / "shared.db"}"\n'
    back = tmp_path / "back"
    (back / "app").mkdir(parents=True)
    (back / "app" / "__init__.py").write_text("")
    (back / "app" / "models.py").write_text(_BACKEND)
    (back / ".livespec.toml").write_text(toml)
    front = tmp_path / "front"
    (front / "src").mkdir(parents=True)
    (front / "src" / "user.ts").write_text(_FRONTEND)
    (front / ".livespec.toml").write_text(toml)
    return back, front


@pytest.mark.asyncio
async def test_alias_removes_false_missing_field_in_schema_drift(tmp_path):
    requires_grammar("typescript")
    back, front = _aliased_group(tmp_path)
    async with Client(mcp) as c:
        for repo in (front, back):
            out = (await c.call_tool("index_project", {"workspace": str(repo)})).data
            assert "error" not in out, out
        drift: dict[str, Any] = (
            await c.call_tool("schema_drift", {"workspace": str(back), "entity": "user"})
        ).data

    assert "error" not in drift, drift
    assert drift["scope"] == {"grouped": True, "projects": ["back", "front"]}
    (entity,) = drift["entities"]
    assert sorted(s["flavor"] for s in entity["schemas"]) == ["pydantic", "ts_interface"]
    missing = [f for f in drift["findings"] if f["kind"] == "missing_field"]
    assert missing == []
    assert drift["counts"]["by_kind"]["missing_field"] == 0
