"""v0.34 P2: extracted schemas are persisted, `@schema: <entity>` binds a
symbol to an entity, and bound schemas are linked DB-wide (cross-repo in a
shared `[workspace] group_db`)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp import state as state_module
from livespec_mcp.config import load_repo_config
from livespec_mcp.domain.matcher import parse_schema_annotations
from livespec_mcp.server import mcp

from .conftest import requires_grammar

_PYDANTIC_USER = '''from pydantic import BaseModel


class User(BaseModel):
    """A user as the API returns it.

    @schema: user
    """

    id: int
    email: str | None
    nickname: str = "x"
'''

_SQL_USERS = """-- Users table.
-- @schema: user
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    bio TEXT
);
"""

_TS_USER = """/** @schema: User */
export interface User {
  id: number;
  nickname?: string;
  deletedAt: string | null;
}
"""


def _toml(root: Path, group_db: Path | None, extra: str = "") -> None:
    text = f'[workspace]\ngroup_db = "{group_db}"\n' if group_db is not None else ""
    if text or extra:
        (root / ".livespec.toml").write_text(text + extra)


def _backend(root: Path, *, group_db: Path | None, extra_toml: str = "") -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "models.py").write_text(_PYDANTIC_USER)
    (root / "migrations").mkdir()
    (root / "migrations" / "001.sql").write_text(_SQL_USERS)
    _toml(root, group_db, extra_toml)
    return root


def _frontend(root: Path, *, group_db: Path | None) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "user.ts").write_text(_TS_USER)
    _toml(root, group_db)
    return root


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _repo_db(repo: Path) -> Path:
    return state_module.get_state(str(repo)).settings.db_path


def _fields(conn: sqlite3.Connection, qname: str) -> dict[str, tuple]:
    rows = conn.execute(
        """SELECT f.name, f.field_key, f.type, f.item_type, f.optional, f.nullable
           FROM schema_field f JOIN symbol s ON s.id = f.symbol_id
           WHERE s.qualified_name = ? ORDER BY f.ordinal""",
        (qname,),
    ).fetchall()
    return {r["name"]: (r["field_key"], r["type"], r["optional"], r["nullable"]) for r in rows}


def _linked_qnames(conn: sqlite3.Connection, entity_key: str = "user") -> set[frozenset[str]]:
    rows = conn.execute(
        """SELECT a.qualified_name AS a, b.qualified_name AS b FROM schema_link l
           JOIN symbol a ON a.id = l.a_symbol_id
           JOIN symbol b ON b.id = l.b_symbol_id
           WHERE l.entity_key = ?""",
        (entity_key,),
    ).fetchall()
    return {frozenset((r["a"], r["b"])) for r in rows}


_ALL_THREE = {
    frozenset(("app.models.User", "migrations.001.users")),
    frozenset(("app.models.User", "src.user.User")),
    frozenset(("migrations.001.users", "src.user.User")),
}


async def _index(c: Client, *repos: Path) -> None:
    for repo in repos:
        out = (await c.call_tool("index_project", {"workspace": str(repo)})).data
        assert "error" not in out, out


@pytest.mark.parametrize("order", ["back_first", "front_first"])
@pytest.mark.asyncio
async def test_group_persists_fields_and_links_all_bound_schemas(tmp_path, order):
    requires_grammar("typescript")
    shared = tmp_path / "grp" / "shared.db"
    back = _backend(tmp_path / "back", group_db=shared)
    front = _frontend(tmp_path / "front", group_db=shared)

    async with Client(mcp) as c:
        await _index(c, *((back, front) if order == "back_first" else (front, back)))

    conn = _db(shared)
    flavors = {
        r["qualified_name"]: r["flavor"]
        for r in conn.execute(
            "SELECT s.qualified_name, d.flavor FROM schema_decl d JOIN symbol s ON s.id = d.symbol_id"
        )
    }
    assert flavors == {
        "app.models.User": "pydantic",
        "migrations.001.users": "sql_table",
        "src.user.User": "ts_interface",
    }
    # (field_key, type, optional, nullable) as the contract maps them.
    assert _fields(conn, "app.models.User") == {
        "id": ("id", "int", 0, 0),
        "email": ("email", "string", 0, 1),
        "nickname": ("nickname", "string", 1, 0),
    }
    assert _fields(conn, "migrations.001.users") == {
        "id": ("id", "int", 1, 0),
        "email": ("email", "string", 0, 0),
        "bio": ("bio", "string", 0, 1),
    }
    assert _fields(conn, "src.user.User") == {
        "id": ("id", "float", 0, 0),
        "nickname": ("nickname", "string", 1, 0),
        "deletedAt": ("deletedat", "string", 0, 1),
    }
    bindings = {
        (r["qualified_name"], r["entity"], r["entity_key"])
        for r in conn.execute(
            "SELECT s.qualified_name, b.entity, b.entity_key FROM schema_binding b "
            "JOIN symbol s ON s.id = b.symbol_id"
        )
    }
    assert bindings == {
        ("app.models.User", "user", "user"),
        ("migrations.001.users", "user", "user"),
        ("src.user.User", "User", "user"),
    }
    assert _linked_qnames(conn) == _ALL_THREE
    conn.close()


@pytest.mark.asyncio
async def test_without_group_db_repos_are_not_linked(tmp_path):
    requires_grammar("typescript")
    back = _backend(tmp_path / "back", group_db=None)
    front = _frontend(tmp_path / "front", group_db=None)

    async with Client(mcp) as c:
        await _index(c, back, front)

    back_conn = _db(_repo_db(back))
    # A per-repo DB still links within the repo: the model and its table.
    assert _linked_qnames(back_conn) == {frozenset(("app.models.User", "migrations.001.users"))}
    back_conn.close()
    front_conn = _db(_repo_db(front))
    assert _linked_qnames(front_conn) == set()
    assert front_conn.execute("SELECT COUNT(*) FROM schema_binding").fetchone()[0] == 1
    front_conn.close()


@pytest.mark.asyncio
async def test_removing_the_binding_drops_its_links(tmp_path):
    requires_grammar("typescript")
    shared = tmp_path / "grp" / "shared.db"
    back = _backend(tmp_path / "back", group_db=shared)
    front = _frontend(tmp_path / "front", group_db=shared)

    async with Client(mcp) as c:
        await _index(c, back, front)
        (front / "src" / "user.ts").write_text(_TS_USER.replace("/** @schema: User */\n", ""))
        await _index(c, front)

    conn = _db(shared)
    assert _linked_qnames(conn) == {frozenset(("app.models.User", "migrations.001.users"))}
    # The interface is still a schema, just no longer bound.
    assert _fields(conn, "src.user.User")
    conn.close()


@pytest.mark.asyncio
async def test_schema_binding_is_not_a_spec_link(tmp_path):
    back = _backend(tmp_path / "back", group_db=None)

    async with Client(mcp) as c:
        await _index(c, back)
        created = (
            await c.call_tool(
                "create_spec", {"workspace": str(back), "spec_id": "user", "title": "User"}
            )
        ).data
        assert "error" not in created, created
        await c.call_tool("index_project", {"workspace": str(back), "force": True})
        verbs = (await c.call_tool("scan_annotation_verbs", {"workspace": str(back)})).data

    conn = _db(_repo_db(back))
    assert conn.execute("SELECT COUNT(*) FROM spec_symbol").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM schema_binding").fetchone()[0] == 2
    conn.close()
    assert verbs["total_findings"] == 0, verbs


@pytest.mark.asyncio
async def test_binding_on_a_function_is_kept(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "svc.py").write_text(
        'def load_user(uid):\n    """Fetch one user.\n\n    @schema: user\n    """\n    return uid\n'
    )

    async with Client(mcp) as c:
        await _index(c, root)

    conn = _db(_repo_db(root))
    row = conn.execute(
        """SELECT s.qualified_name, s.start_line, b.entity_key, b.line,
                  (SELECT COUNT(*) FROM schema_decl d WHERE d.symbol_id = s.id) AS decls
           FROM schema_binding b JOIN symbol s ON s.id = b.symbol_id"""
    ).fetchone()
    conn.close()
    assert row["qualified_name"] == "app.svc.load_user"
    assert row["entity_key"] == "user"
    assert row["line"] == row["start_line"]
    assert row["decls"] == 0


@pytest.mark.asyncio
async def test_sql_files_are_indexed_as_sql(tmp_path):
    back = _backend(tmp_path / "back", group_db=None)

    async with Client(mcp) as c:
        out = (await c.call_tool("index_project", {"workspace": str(back)})).data

    assert out["languages"].get("sql") == 1, out
    assert "sql" not in out.get("languages_unsupported", {}), out
    conn = _db(_repo_db(back))
    assert (
        conn.execute("SELECT language FROM file WHERE path LIKE '%001.sql'").fetchone()[0] == "sql"
    )
    conn.close()


@pytest.mark.asyncio
async def test_index_languages_accepts_sql(tmp_path):
    back = _backend(
        tmp_path / "back", group_db=None, extra_toml="[index]\nlanguages = ['python', 'sql']\n"
    )
    assert load_repo_config(back).languages == {"python", "sql"}

    async with Client(mcp) as c:
        out = (await c.call_tool("index_project", {"workspace": str(back)})).data

    assert out["languages"] == {"python": 2, "sql": 1}, out


def test_index_languages_still_rejects_unknown(tmp_path):
    (tmp_path / ".livespec.toml").write_text("[index]\nlanguages = ['python', 'cobol']\n")
    with pytest.raises(ValueError, match="unsupported languages"):
        load_repo_config(tmp_path)


def test_parse_schema_annotations():
    text = (
        "Doc.\n\n@schema: user-profile and more prose\n * @schema: Order\n# @schema:\n@schemas: x\n"
    )
    got = [(a.entity, a.entity_key) for a in parse_schema_annotations(text)]
    assert got == [("user-profile", "userprofile"), ("Order", "order")]
    assert parse_schema_annotations("") == []
    assert parse_schema_annotations("see @schema: user inline") == []
    assert parse_schema_annotations("@schema is used here") == []


@pytest.mark.asyncio
async def test_find_dead_code_skips_sql_tables_and_zod_variables(workspace):
    requires_grammar("typescript")
    (workspace / "m.sql").write_text(_SQL_USERS)
    (workspace / "z.ts").write_text(
        "import { z } from 'zod';\nexport const W = z.object({ s: z.string() });\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        dead = (
            await c.call_tool(
                "find_dead_code",
                {
                    "include_non_python": True,
                    "include_public": True,
                    "include_infrastructure": True,
                },
            )
        ).data
    assert not {d["kind"] for d in dead["dead_symbols"]} & {"table", "variable"}
