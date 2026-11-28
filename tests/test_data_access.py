"""`find_data_access`: the code -> database table map, read at index time.

Fixtures are in `tests/fixtures/data_access/{py,ts,java}`; every test runs the
tool through `Client(mcp)`. The SQL recogniser is also exercised directly,
because "prose must produce nothing" is a property of its own.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from livespec_mcp.domain.sql_access import HOLE, sql_tables
from livespec_mcp.server import mcp

from .conftest import requires_grammar

FIXTURES = Path(__file__).parent / "fixtures" / "data_access"


async def _repo(c: Client, tmp_path: Path, kind: str) -> Path:
    root = tmp_path / kind
    shutil.copytree(FIXTURES / kind, root)
    out = (await c.call_tool("index_project", {"workspace": str(root)})).data
    assert "error" not in out, out
    return root


async def _fda(c: Client, ws: Path, **args: Any) -> dict[str, Any]:
    res = await c.call_tool(
        "find_data_access", {"workspace": str(ws), **args}, raise_on_error=False
    )
    return res.data


def _rows(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for kind in out["accesses"].values() for r in kind]


def _inv(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["table"]: r for r in out["tables"]}


# --------------------------------------------------------------------------
# The SQL recogniser on its own
# --------------------------------------------------------------------------


def _t(text: str, **kw: Any) -> set[tuple[str, str]]:
    return {(t.access, t.name.lower()) for t in sql_tables(text, **kw)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SELECT a FROM t", {("read", "t")}),
        ("select a from t1 join t2 on t1.id = t2.id", {("read", "t1"), ("read", "t2")}),
        ("SELECT * FROM a, b WHERE 1=1", {("read", "a"), ("read", "b")}),
        ("INSERT INTO t (a) VALUES (1)", {("write", "t")}),
        ("UPDATE t SET a = 1", {("write", "t")}),
        ("DELETE FROM t WHERE a = 1", {("write", "t")}),
        ("REPLACE INTO t (a) VALUES (1)", {("write", "t")}),
        (
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = 1",
            {("write", "t"), ("read", "s")},
        ),
        ("CREATE TABLE IF NOT EXISTS t (id int)", {("ddl", "t")}),
        ("ALTER TABLE t ADD COLUMN c int", {("ddl", "t")}),
        ("DROP TABLE t", {("ddl", "t")}),
        ("INSERT INTO a (x) SELECT x FROM b", {("write", "a"), ("read", "b")}),
        ("SELECT * FROM t WHERE id IN (SELECT id FROM u)", {("read", "t"), ("read", "u")}),
        ("WITH c AS (SELECT * FROM base) SELECT * FROM c", {("read", "base")}),
        ('SELECT * FROM "Sch"."Tbl"', {("read", "tbl")}),
        ("SELECT * FROM `db`.`tbl`", {("read", "tbl")}),
        (f"SELECT * FROM users WHERE id = {HOLE}", {("read", "users")}),
        ("SELECT\n  a\nFROM\n  multi\n  JOIN other ON 1=1", {("read", "multi"), ("read", "other")}),
        ("UPDATE t SET a = 1; DELETE FROM u", {("write", "t"), ("write", "u")}),
    ],
)
def test_sql_statement_shapes(text: str, expected: set[tuple[str, str]]):
    assert _t(text) == expected


def test_schema_qualified_name_keeps_its_schema():
    (t,) = sql_tables("SELECT * FROM Billing.Invoices")
    assert (t.schema, t.name) == ("Billing", "Invoices")


@pytest.mark.parametrize(
    "text",
    [
        "Select a file from the list",
        "Update the record",
        "Delete from the cart",
        "select the best option from your list of choices",
        "Insert into the box a coin",
        "Please select a file from the list",
        "update it when you are ready",
        "SELECT",
        f"SELECT * FROM {HOLE}",
        "",
    ],
)
def test_prose_and_holes_in_table_position_yield_nothing(text: str):
    assert sql_tables(text) == []


def test_a_hole_inside_a_table_name_does_not_invent_a_table():
    assert all(HOLE not in t.name for t in sql_tables(f"SELECT * FROM reports_{HOLE}"))


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_sql_in_strings_every_statement_kind(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        inv = _inv(await _fda(c, ws))

    # JOIN, quoted name and LEFT JOIN
    assert inv["customers"]["read"] == 1
    # CTE `recent` is not a table; `orders` is read three times (join, WITH, INSERT..SELECT)
    assert "recent" not in inv
    assert inv["orders"]["read"] == 3 and inv["orders"]["write"] == 0
    # INSERT ... SELECT writes one table and reads another
    assert inv["orders_archive"]["write"] == 1
    # write kinds
    assert inv["settings"]["write"] == 1  # REPLACE INTO
    assert inv["inventory"]["write"] == 1  # MERGE INTO
    assert inv["users"]["write"] == 3  # INSERT, UPDATE, DELETE "users"
    # ddl
    assert inv["feature_flags"]["ddl"] == 1
    assert inv["legacy_things"]["ddl"] == 1
    assert inv["users"]["ddl"] == 1  # ALTER TABLE
    # subquery: the table inside it is read
    assert inv["audit_log"]["read"] >= 1


@pytest.mark.asyncio
async def test_python_constants_fstring_holes_and_prose(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        inv = _inv(await _fda(c, ws))
        users = await _fda(c, ws, table="users")
        prose = await _fda(c, ws, qname="app.sqlq.prose")
        dyn = await _fda(c, ws, qname="app.sqlq.dynamic")

    # `USERS_SQL` is a module constant: counted where `list_users` uses it,
    # with its schema, and not at the definition.
    reads = {r["symbol"]: r for r in users["accesses"]["read"]}
    assert reads["app.sqlq.list_users"]["schema"] == "public"
    assert "app.sqlq" not in reads
    # f-string with a hole in the table position: no table, no `reports_`
    assert not any(t.startswith("reports") for t in inv)
    assert dyn["tables"] == [] and dyn["counts"]["total"] == 0
    # prose containing select/update/delete/from
    assert prose["tables"] == []
    # f-string with a hole elsewhere still reads the table
    assert inv["orders_archive"]["write"] == 1


@pytest.mark.asyncio
async def test_python_sqlalchemy_shapes_and_model_to_table(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        acc = await _fda(c, ws, table="accounts")
        inv_ = await _fda(c, ws, table="billing.invoices")
        audit = await _fda(c, ws, table="audit_log")

    rows = {(r["symbol"].rsplit(".", 1)[1], k) for k, rs in acc["accesses"].items() for r in rs}
    assert rows == {
        ("find_account", "read"),  # session.query(Account)
        ("stmt_writes", "write"),  # insert(Account) and delete(Account): one symbol, two lines
        ("add_account", "write"),  # session.add(Account(...)) through a local
    }
    assert acc["counts"]["write"] == 3 and acc["counts"]["read"] == 1
    assert all(r["mechanism"] == "sqlalchemy" and r["model"] == "Account" for r in _rows(acc))
    assert all(r["confidence"] == "model" for r in _rows(acc))
    # __table_args__ schema, select()/update()
    assert inv_["schema"] == "billing"
    assert {r["symbol"].rsplit(".", 1)[1] for r in _rows(inv_)} == {"stmt_reads", "stmt_writes"}
    # Table("audit_log", ...) bound to a module variable, then select(audit)
    assert any(r["symbol"].endswith("touch_audit") for r in _rows(audit))


@pytest.mark.asyncio
async def test_python_django_objects_and_meta_db_table(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        books = await _fda(c, ws, table="library_books")
        authors = await _fda(c, ws, table="blog_author")

    # Meta.db_table is declared; objects.all().delete() writes
    (row,) = _rows(books)
    assert list(books["accesses"]) == ["write"]
    assert (row["model"], row["mechanism"], row["confidence"]) == ("Book", "django", "model")
    # no db_table: Django's `<app>_<model>` convention, and the row says so
    assert {r["symbol"].rsplit(".", 1)[1] for r in _rows(authors)} == {"authors", "make_author"}
    assert {r["confidence"] for r in _rows(authors)} == {"convention"}
    assert set(authors["accesses"]) == {"read", "write"}


@pytest.mark.asyncio
async def test_inventory_joins_sql_files_and_counts_are_exact(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        out = await _fda(c, ws)
        undeclared_only = await _fda(c, ws, summary_only=True)

    inv = _inv(out)
    assert inv["users"]["status"] == "ok" and inv["users"]["declared_in"] == ["sql/001_init.sql:1"]
    assert inv["never_used"]["status"] == "unaccessed"
    assert inv["never_used"]["read"] == inv["never_used"]["write"] == 0
    assert inv["accounts"]["status"] == "undeclared"
    counts = out["counts"]
    assert counts["declared_unaccessed"] == 1
    assert counts["accessed_undeclared"] == sum(r["status"] == "undeclared" for r in out["tables"])
    assert counts["read"] == sum(r["read"] for r in out["tables"])
    assert counts["write"] == sum(r["write"] for r in out["tables"])
    assert counts["ddl"] == sum(r["ddl"] for r in out["tables"])
    # summary_only drops the rows and keeps every count
    assert "tables" not in undeclared_only
    assert undeclared_only["counts"]["tables"] == counts["tables"]


@pytest.mark.asyncio
async def test_unknown_table_is_an_error_with_did_you_mean(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        err = await _fda(c, ws, table="usres")
        bad_access = await _fda(c, ws, access="reed")
        no_symbol = await _fda(c, ws, qname="app.sqlq.no_such_function")

    assert err["isError"] is True and "users" in err["did_you_mean"]
    assert set(err) <= {"error", "isError", "did_you_mean", "hint"}
    assert bad_access["isError"] is True and "read" in bad_access["did_you_mean"]
    assert no_symbol["isError"] is True


@pytest.mark.asyncio
async def test_table_lookup_ignores_case_quotes_and_schema(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        plain = await _fda(c, ws, table="users")
        shouty = await _fda(c, ws, table='"PUBLIC"."USERS"')
        other_schema = await _fda(c, ws, table="other.users")

    assert plain["counts"]["total"] == shouty["counts"]["total"] == 8
    # a different explicit schema rules out the `public.users` read only
    assert other_schema["counts"]["total"] == plain["counts"]["total"] - 1
    assert shouty["schema"] == "public"


@pytest.mark.asyncio
async def test_access_filter_keeps_counts_of_every_kind(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        out = await _fda(c, ws, table="users", access="write")
        inv = await _fda(c, ws, access="ddl")

    assert list(out["accesses"]) == ["write"]
    assert out["counts"]["matched"] == 3
    assert out["counts"]["read"] == 4 and out["counts"]["ddl"] == 1
    assert {r["table"] for r in inv["tables"]} == {"feature_flags", "legacy_things", "users"}


@pytest.mark.asyncio
async def test_symbol_mode_and_include_callers(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        direct = await _fda(c, ws, qname="app.service.register")
        deep = await _fda(c, ws, qname="app.service.register", include_callers=True)
        own = await _fda(c, ws, qname="app.sqlq.add_user")
        only_users = await _fda(
            c, ws, qname="app.service.register", include_callers=True, table="users", access="read"
        )

    assert direct["tables"] == [] and "include_callers" in direct["hint"]
    assert own["tables"][0]["table"] == "users" and "via" not in own["tables"][0]
    assert own["counts"]["direct"] == 1
    # add_user is one hop away; list_users is two hops away through audit_trail
    by_via = {r["via"]: r for r in deep["tables"]}
    assert by_via["app.sqlq.add_user"]["depth"] == 1
    assert by_via["app.sqlq.add_user"]["kind"] == "write"
    assert by_via["app.sqlq.list_users"]["depth"] == 2
    assert by_via["app.sqlq.list_users"]["through"] == "app.service.audit_trail"
    assert deep["counts"] == {
        "total": 2,
        "direct": 0,
        "via_callees": 2,
        "tables": 1,
        "unresolved": 0,
        "read": 1,
        "write": 1,
        "ddl": 0,
    }
    assert [r["via"] for r in only_users["tables"]] == ["app.sqlq.list_users"]


@pytest.mark.asyncio
async def test_pagination_keeps_exact_counts(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        full = await _fda(c, ws, table="users")
        first = await _fda(c, ws, table="users", limit=3)
        second = await _fda(c, ws, table="users", limit=3, cursor=first["next_cursor"])
        tail = await _fda(c, ws, table="users", limit=3, cursor=6)
        inv1 = await _fda(c, ws, limit=5)

    assert first["counts"] == second["counts"] == full["counts"]
    assert len(_rows(first)) == 3 and first["next_cursor"] == 3
    assert len(_rows(second)) == 3 and second["next_cursor"] == 6
    assert len(_rows(tail)) == 2 and tail["next_cursor"] is None
    paged = {r["file"] for p in (first, second, tail) for r in _rows(p)}
    assert paged == {r["file"] for r in _rows(full)}
    assert len(inv1["tables"]) == 5 and inv1["next_cursor"] == 5
    assert inv1["counts"]["tables"] == 15 and inv1["counts"]["listed"] == 15


@pytest.mark.asyncio
async def test_reindex_after_an_edit_replaces_only_that_files_rows(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        before = await _fda(c, ws)
        sqlq = ws / "app" / "sqlq.py"
        sqlq.write_text(
            sqlq.read_text().replace(
                'DELETE FROM "users" WHERE id = 1', "DELETE FROM sessions WHERE id = 1"
            )
        )
        out = (await c.call_tool("index_project", {"workspace": str(ws)})).data
        assert out["files_changed"] == 1
        after = await _fda(c, ws)
        accounts_after = await _fda(c, ws, table="accounts")

    b, a = _inv(before), _inv(after)
    assert a["users"]["write"] == b["users"]["write"] - 1
    assert a["sessions"]["write"] == 1 and "sessions" not in b
    # rows of the untouched files are intact
    assert a["accounts"] == b["accounts"] and accounts_after["counts"]["total"] == 4
    assert a["library_books"] == b["library_books"]
    # and nothing was duplicated
    assert after["counts"]["read"] == before["counts"]["read"]


@pytest.mark.asyncio
async def test_a_model_edited_in_another_file_re_resolves_its_accesses(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        models = ws / "app" / "sa_models.py"
        models.write_text(models.read_text().replace('"accounts"', '"customer_accounts"'))
        await c.call_tool("index_project", {"workspace": str(ws)})
        renamed = await _fda(c, ws, table="customer_accounts")
        old = await _fda(c, ws, table="accounts")

    assert renamed["counts"]["total"] == 4
    assert old["isError"] is True


@pytest.mark.asyncio
async def test_deleting_a_file_drops_its_accesses(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        (ws / "app" / "sa_models.py").unlink()
        await c.call_tool("index_project", {"workspace": str(ws)})
        inv = _inv(await _fda(c, ws))

    assert "accounts" not in inv and "invoices" not in inv
    assert inv["users"]["write"] == 3


# --------------------------------------------------------------------------
# TypeScript / JavaScript
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typescript_knex_template_sql_and_typeorm(tmp_path: Path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "ts")
        out = await _fda(c, ws)
        orders = await _fda(c, ws, table="orders")
        widgets = await _fda(c, ws, table="widgets")

    inv = _inv(out)
    assert inv["order_lines"]["write"] == 1  # db.insert(row).into("order_lines")
    assert inv["sessions"]["write"] == 1  # knex("sessions")...del()
    assert inv["payments"]["write"] == 1  # raw INSERT in a plain string
    assert inv["products"]["read"] == 1  # JOIN inside a multi-line template with ${} holes
    mech = {r["symbol"].rsplit(".", 1)[1]: r["mechanism"] for r in _rows(orders)}
    assert mech == {"ordersFor": "knex", "rawSql": "sql"}
    # `message()` is prose with select/update/from: nothing
    assert not any(r["symbol"].endswith("message") for r in _rows(orders) + _rows(widgets))
    # TypeORM @Entity("widgets") + Repository<Widget>: read via find, write via remove
    assert widgets["counts"]["read"] == 1 and widgets["counts"]["write"] == 1
    assert {r["model"] for r in _rows(widgets)} == {"Widget"}


@pytest.mark.asyncio
async def test_typescript_prisma_model_is_listed_unresolved_never_guessed(tmp_path: Path):
    requires_grammar("typescript")
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "ts")
        out = await _fda(c, ws)
        load = await _fda(c, ws, qname="src.repo.loadUser")
        save = await _fda(c, ws, qname="src.repo.saveUser")
        guess = await _fda(c, ws, table="user")

    assert out["unresolved_models"] == [
        {"model": "user", "mechanism": "prisma", "read": 1, "write": 1, "ddl": 0}
    ]
    assert "user" not in _inv(out) and "users" not in _inv(out)
    (r,) = load["tables"]
    assert (r["table"], r["kind"], r["model"], r["confidence"]) == (
        None,
        "read",
        "user",
        "unresolved",
    )
    assert save["tables"][0]["kind"] == "write"
    assert guess["isError"] is True


# --------------------------------------------------------------------------
# Java
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_java_jpa_spring_data_query_and_text_blocks(tmp_path: Path):
    requires_grammar("java")
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "java")
        out = await _fda(c, ws)
        customers = await _fda(c, ws, table="crm.customers")
        legacy = await _fda(c, ws, table="legacy_users")
        service = await _fda(
            c, ws, qname="src.main.java.shop.CustomerService.CustomerService.rename"
        )

    inv = _inv(out)
    assert inv["customers"]["schema"] == "crm"
    by_sym = {
        (".".join(r["symbol"].split(".")[-2:]), k, r["mechanism"])
        for k, rs in customers["accesses"].items()
        for r in rs
    }
    assert ("CustomerRepository.byName", "read", "jpa") in by_sym  # @Query JPQL -> entity -> table
    assert ("CustomerRepository.withOrders", "read", "sql") in by_sym  # nativeQuery: real SQL
    assert ("CustomerService.load", "read", "jpa") in by_sym  # em.find(Customer.class, ..)
    assert (
        "CustomerService.purge",
        "write",
        "jpa",
    ) in by_sym  # createQuery("DELETE FROM Customer")
    assert inv["orders"]["read"] == 1  # JOIN inside the native query
    # repository method-name prefixes: findBy -> read, save -> write
    kinds = {r["kind"] for r in service["tables"]}
    assert kinds == {"read", "write"} and {r["table"] for r in service["tables"]} == {"customers"}
    # concatenated literals
    assert legacy["counts"]["write"] == 1
    # string "Delete from the cart" is prose
    assert not any(r["symbol"].endswith("CustomerService.text") for r in _rows(customers))


@pytest.mark.asyncio
async def test_java_entity_without_table_annotation_stays_unresolved(tmp_path: Path):
    requires_grammar("java")
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "java")
        out = await _fda(c, ws)
        save = await _fda(
            c, ws, qname="src.main.java.shop.CustomerService.CustomerService.saveBasket"
        )

    assert out["unresolved_models"] == [
        {"model": "Basket", "mechanism": "jpa", "read": 0, "write": 1, "ddl": 0}
    ]
    assert "basket" not in _inv(out)
    assert save["tables"][0]["table"] is None and save["tables"][0]["model"] == "Basket"


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_carry_the_literal_vs_inferred_marker(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
    conn = sqlite3.connect(ws / ".mcp-docs" / "docs.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT mechanism, confidence, COUNT(*) n FROM data_access GROUP BY 1, 2"
    ).fetchall()
    seen = {(r["mechanism"], r["confidence"]) for r in rows}
    assert ("sql", "literal") in seen
    assert ("sqlalchemy", "model") in seen
    assert ("django", "convention") in seen
    assert (
        conn.execute("PRAGMA foreign_key_list(data_access)").fetchall()[0]["on_delete"] == "CASCADE"
    )
