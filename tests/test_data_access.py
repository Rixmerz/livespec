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
async def test_symbol_mode_and_include_callees(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "py")
        direct = await _fda(c, ws, qname="app.service.register")
        deep = await _fda(c, ws, qname="app.service.register", include_callees=True)
        own = await _fda(c, ws, qname="app.sqlq.add_user")
        only_users = await _fda(
            c, ws, qname="app.service.register", include_callees=True, table="users", access="read"
        )
        old = await _fda(c, ws, qname="app.service.register", include_callers=True)

    assert direct["tables"] == [] and "include_callees" in direct["hint"]
    assert "deprecated" not in deep and deep["include_callees"] is True
    # the old spelling still means "follow the callees", and says it is old
    assert old["tables"] == deep["tables"] and old["counts"] == deep["counts"]
    assert "include_callees" in old["deprecated"]
    assert own["tables"][0]["table"] == "users" and "via" not in own["tables"][0]
    # direct rows carry the same `file` key the `via` rows have
    assert own["tables"][0]["file"].endswith(f"sqlq.py:{own['tables'][0]['line']}")
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

    (u,) = out["unresolved_models"]
    assert {k: u[k] for k in ("model", "mechanism", "read", "write", "ddl")} == {
        "model": "user",
        "mechanism": "prisma",
        "read": 1,
        "write": 1,
        "ddl": 0,
    }
    # each unresolved access can be found in the code: symbol + file:line
    assert {a["symbol"] for a in u["accesses"]} == {"src.repo.loadUser", "src.repo.saveUser"}
    assert all(a["file"].startswith("src/repo.ts:") for a in u["accesses"])
    assert "user" not in _inv(out) and "users" not in _inv(out)
    (r,) = load["tables"]
    assert (r["table"], r["kind"], r["model"], r["confidence"]) == (
        None,
        "read",
        "user",
        "unresolved",
    )
    assert save["tables"][0]["kind"] == "write"
    # `user` is no table: the model of that name answers, and says it did
    assert guess["mode"] == "model" and guess["matched"] == "model"
    assert guess["tables"] == [] and "does not state" in guess["note"]
    assert guess["counts"]["total"] == 2
    assert {r["confidence"] for r in _rows(guess)} == {"unresolved"}


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

    (u,) = out["unresolved_models"]
    assert (u["model"], u["mechanism"], u["read"], u["write"], u["ddl"]) == (
        "Basket",
        "jpa",
        0,
        1,
        0,
    )
    assert u["accesses"][0]["file"].endswith("CustomerService.java:20")
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


# --------------------------------------------------------------------------
# Recall: SQL in constants and local variables, imports, declarations
# --------------------------------------------------------------------------


async def _sym(c: Client, ws: Path, qname: str, **kw: Any) -> dict[str, Any]:
    out = await _fda(c, ws, qname=qname, **kw)
    assert "error" not in out, out
    return out


def test_a_script_yields_one_row_per_statement_and_a_drop_does_not_hide_a_create():
    from livespec_mcp.domain.sql_access import sql_analyse

    tables, dynamic = sql_analyse(
        "DROP TABLE IF EXISTS a; CREATE TABLE a (id int); INSERT INTO a (id) VALUES (1);"
        " INSERT INTO a (id) VALUES (2); SELECT * FROM a JOIN a b ON 1=1"
    )
    got = [(t.access, t.verb or None, t.name) for t in tables]
    assert got == [
        ("ddl", "DROP", "a"),
        ("ddl", "CREATE", "a"),
        ("write", None, "a"),
        ("write", None, "a"),
        ("read", None, "a"),  # one statement joining a table to itself: once
    ]
    assert dynamic == 0


def test_an_interpolated_table_position_is_counted_not_guessed():
    from livespec_mcp.domain.sql_access import sql_analyse

    tables, dynamic = sql_analyse(f"DELETE FROM {HOLE}; SELECT * FROM users WHERE id = {HOLE}")
    assert [(t.access, t.name) for t in tables] == [("read", "users")]
    assert dynamic == 1
    assert sql_analyse(f"Select a file from {HOLE}") == ([], 0)


@pytest.mark.asyncio
async def test_sql_constants_are_attributed_to_their_uses_and_declared_where_defined(
    tmp_path: Path,
):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "consts")
        inv = await _fda(c, ws, limit=500)
        migrate = await _sym(c, ws, "src.shop.script.migrate")
        init = await _sym(c, ws, "src.shop.db.init")
        rows = await _sym(c, ws, "src.shop.schema.widget_rows")
        labels = await _sym(c, ws, "src.shop.store.Shelf.labels")
        owned = await _sym(c, ws, "src.shop.store.Shelf.owned")
        formatted = await _sym(c, ws, "src.shop.store.Shelf.formatted")
        describe = await _sym(c, ws, "src.shop.store.Shelf.describe")
        version = await _sym(c, ws, "src.shop.schema.current_version")

    t = _inv(inv)
    # (a) a script gives one access per statement; each row sits on its own line
    assert {(r["kind"], r["table"]) for r in migrate["tables"]} == {
        ("ddl", "invoices"),
        ("ddl", "payments"),
        ("write", "invoices"),
        ("write", "payments"),
    }
    assert len({r["line"] for r in migrate["tables"]}) == 4
    # (b) the constant's SQL lands on the function that executes it, on the line of the use
    assert {(r["kind"], r["table"]) for r in init["tables"]} == {
        ("ddl", "widgets"),
        ("ddl", "gadgets"),
        ("write", "widgets"),
    }
    assert {r["line"] for r in init["tables"]} == {9}  # `conn.executescript(SCHEMA_SQL)`
    assert [(r["kind"], r["table"]) for r in rows["tables"]] == [("read", "widgets")]  # f-string
    assert {(r["kind"], r["table"]) for r in labels["tables"]} == {("read", "labels")}  # self.X
    assert {(r["kind"], r["table"]) for r in owned["tables"]} == {("read", "shelves")}  # a + b
    assert {(r["kind"], r["table"]) for r in formatted["tables"]} == {("read", "labels")}
    # a constant that only looks like SQL, passed around as text, is nothing
    assert describe["tables"] == [] and version["tables"] == []
    # (d) a table created in code is DECLARED, and the inventory says where from
    for name in ("widgets", "gadgets", "invoices", "payments"):
        assert t[name]["status"] == "ok" and t[name]["declared_source"] == "code", name
    assert t["widgets"]["declared_in"] == ["src/shop/schema.py:4"]
    assert t["gadgets"]["declared_in"] == ["src/shop/schema.py:5"]
    # DDL nobody executes still declares its table: declared, never accessed
    assert t["orphans"]["status"] == "unaccessed"
    assert t["orphans"]["declared_in"] == ["src/shop/schema.py:11"]
    assert inv["counts"]["declared_in_code"] >= 5
    assert "help_text" not in t and "list" not in t


@pytest.mark.asyncio
async def test_an_imported_constant_is_followed_one_hop_and_follows_its_definition(
    tmp_path: Path,
):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "consts")
        rel = await _sym(c, ws, "src.shop.db.init")
        absolute = await _sym(c, ws, "src.shop.other.go")
        shadowed = await _sym(c, ws, "src.shop.other.shadowed")
        before = await _fda(c, ws, table="gadgets")

        # edit ONLY the defining file: the importing files are not re-read, yet
        # their derived rows follow the new text
        schema = ws / "src" / "shop" / "schema.py"
        schema.write_text(
            schema.read_text(encoding="utf-8").replace(
                "CREATE TABLE IF NOT EXISTS gadgets",
                "CREATE TABLE IF NOT EXISTS sprockets",
            ),
            encoding="utf-8",
        )
        out = (await c.call_tool("index_project", {"workspace": str(ws)})).data
        assert out["files_changed"] == 1, out
        after = await _sym(c, ws, "src.shop.db.init")
        gone = await _fda(c, ws, table="gadgets")

    for payload in (rel, absolute):
        assert {(r["kind"], r["table"]) for r in payload["tables"]} == {
            ("ddl", "widgets"),
            ("ddl", "gadgets"),
            ("write", "widgets"),
        }
        assert all(r["constant"].endswith(":SCHEMA_SQL") for r in payload["tables"])
    assert shadowed["tables"] == []  # a parameter named like the import hides it
    assert before["counts"]["ddl"] >= 2
    assert {r["table"] for r in after["tables"]} == {"widgets", "sprockets"}
    assert gone.get("isError") is True  # no table, no model, nowhere


@pytest.mark.asyncio
async def test_local_string_variables_are_read_through_when_bound_once(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "consts")
        inv = await _fda(c, ws, limit=500)
        search = await _sym(c, ws, "src.shop.local.search")
        sub = await _sym(c, ws, "src.shop.local.with_subquery")
        cond = await _sym(c, ws, "src.shop.local.conditional")
        reassigned = await _sym(c, ws, "src.shop.local.reassigned")
        rebound = await _sym(c, ws, "src.shop.local.rebound")
        param = await _sym(c, ws, "src.shop.local.from_parameter")
        dyn = await _sym(c, ws, "src.shop.local.dynamic")

    assert {(r["kind"], r["table"]) for r in search["tables"]} == {("read", "books")}
    # a subquery held in a variable, interpolated into another
    assert {(r["kind"], r["table"]) for r in sub["tables"]} == {
        ("read", "authors"),
        ("read", "loans"),
    }
    # `+=` appends to the same statement
    assert {(r["kind"], r["table"]) for r in cond["tables"]} == {
        ("read", "tickets"),
        ("read", "statuses"),
    }
    # negatives: nothing is invented where the variable is not one known string
    assert reassigned["tables"] == []
    assert param["tables"] == []
    assert dyn["tables"] == []
    # a variable bound to two tables yields no row at the call that executes it:
    # only the two literals, where each is written
    assert {(r["kind"], r["table"]) for r in rebound["tables"]} == {
        ("read", "red_things"),
        ("read", "blue_things"),
    }
    assert {r["line"] for r in rebound["tables"]} == {31, 33}
    # what could not be seen is counted: `DELETE FROM {table}`, `"... FROM " + table`
    assert inv["counts"]["skipped"]["dynamic_table"] == 2


@pytest.mark.asyncio
async def test_catalogue_tables_are_marked_and_kept_out_of_the_inventory(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "consts")
        inv = await _fda(c, ws, limit=500)
        full = await _fda(c, ws, limit=500, include_system=True)
        tables = await _sym(c, ws, "src.shop.db.tables")
        cols = await _sym(c, ws, "src.shop.db.columns")
        one = await _fda(c, ws, table="sqlite_master")

    assert "sqlite_master" not in _inv(inv) and "columns" not in _inv(inv)
    assert inv["counts"]["system_tables_hidden"] == 2
    assert full["counts"]["system_tables_hidden"] == 0
    assert _inv(full)["sqlite_master"]["system"] is True
    assert full["counts"]["tables"] == inv["counts"]["tables"] + 2
    assert tables["tables"][0]["system"] is True and cols["tables"][0]["system"] is True
    assert cols["tables"][0]["schema"] == "information_schema"
    assert one["system"] is True


@pytest.mark.asyncio
async def test_typescript_and_java_constants_declare_their_tables(tmp_path: Path):
    requires_grammar("typescript", "java")
    async with Client(mcp) as c:
        ts = await _repo(c, tmp_path, "ts_decl")
        java = await _repo(c, tmp_path, "java_decl")
        ts_inv = _inv(await _fda(c, ts, limit=500))
        java_inv = _inv(await _fda(c, java, limit=500))
        init = await _sym(c, ts, "src.schema.init")

    assert ts_inv["gizmos"]["status"] == "ok" and ts_inv["gizmos"]["declared_in"] == [
        "src/schema.ts:2"
    ]
    assert ts_inv["gizmos"]["declared_source"] == "code"
    assert ts_inv["sprockets"]["declared_in"] == ["src/schema.ts:11"]
    # the constant's SQL is on the function that runs it, at the line of the use
    assert {(r["kind"], r["table"], r["line"]) for r in init["tables"]} == {
        ("ddl", "gizmos", 7),
        ("write", "gizmos", 7),
    }
    assert java_inv["cogs"]["status"] == "ok"
    assert java_inv["cogs"]["declared_in"] == ["src/main/java/shop/Schema.java:7"]


# --------------------------------------------------------------------------
# Recall: SQLAlchemy / Flask-SQLAlchemy
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flask_sqlalchemy_models_get_the_convention_name_only_for_db_model(
    tmp_path: Path,
):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "flask")
        inv = await _fda(c, ws, limit=500)

    t = _inv(inv)
    # CamelCase -> snake_case, marked as a convention, not as declared
    assert set(t) >= {"user", "user_role", "post", "followers", "staff"}
    assert t["user_role"]["mechanisms"] == ["sqlalchemy"]
    # single-table inheritance (no key of its own): the parent's table
    assert "admin" not in t and t["user"]["read"] >= 3
    unresolved = {u["model"] for u in inv["unresolved_models"]}
    # plain declarative has no default name; `store.Model` is not Flask-SQLAlchemy;
    # a computed name and an unknown base are not guessed either
    assert {"Account", "Widget", "Computed", "Orphan"} <= unresolved
    assert "Ledger" not in unresolved and "ledgers" in t
    assert not ({"account", "widget", "computed", "orphan"} & set(t))


@pytest.mark.asyncio
async def test_flask_rows_carry_the_convention_confidence(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "flask")
        one = await _sym(c, ws, "app.routes.one")
        role = await _sym(c, ws, "app.routes.role")
        admins = await _sym(c, ws, "app.routes.admins")
        staff = await _sym(c, ws, "app.routes.staff")
        ledger = await _sym(c, ws, "plain.models.entries")

    (r,) = one["tables"]
    assert (r["table"], r["kind"], r["model"], r["confidence"]) == (
        "user",
        "read",
        "User",
        "convention",
    )
    assert [(r["table"], r["kind"]) for r in role["tables"]] == [("user_role", "read")]
    assert [r["table"] for r in admins["tables"]] == ["user"]
    assert [r["table"] for r in staff["tables"]] == ["staff"]
    assert [(r["table"], r["confidence"]) for r in ledger["tables"]] == [("ledgers", "model")]


@pytest.mark.asyncio
async def test_sqlalchemy_2_calls_and_write_only_collections(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "flask")
        page = await _sym(c, ws, "app.routes.page")
        first = await _sym(c, ws, "app.routes.first")
        count = await _sym(c, ws, "app.routes.how_many")
        add_role = await _sym(c, ws, "app.routes.add_role")
        posts_of = await _sym(c, ws, "app.routes.posts_of")
        follow = await _sym(c, ws, "app.models.User.follow")
        recent = await _sym(c, ws, "app.models.User.recent")
        audit = await _sym(c, ws, "app.models.User.audit")
        untyped = await _sym(c, ws, "app.routes.untyped")
        followed = await _sym(c, ws, "app.service.followed_by")
        tagged = await _sym(c, ws, "app.service.tagged")

    # `x.following.select()` in a file that never imports SQLAlchemy: seen; `x.tags.add()`: not
    # (`following` is many-to-many: its association table is read too)
    assert {(r["table"], r["kind"]) for r in followed["tables"]} == {
        ("user", "read"),
        ("followers", "read"),
    }
    assert tagged["tables"] == []
    # a statement bound to a local, paginated later: the select(Model) is found
    assert [(r["table"], r["kind"]) for r in page["tables"]] == [("post", "read")]
    assert [(r["table"], r["kind"]) for r in first["tables"]] == [("user", "read")]  # first_or_404
    assert [(r["table"], r["kind"]) for r in count["tables"]] == [("post", "read")]  # select_from
    assert {(r["table"], r["kind"]) for r in add_role["tables"]} == {("user_role", "write")}
    # `user.posts.select()`: the attribute is a collection of exactly one class
    (r,) = posts_of["tables"]
    assert (r["table"], r["kind"], r["through"]) == ("post", "read", "User.posts:select")
    # `self.following.add(x)` of a many-to-many collection writes its association table
    (r,) = follow["tables"]
    assert (r["table"], r["kind"], r["model"], r["confidence"]) == (
        "followers",
        "write",
        "followers",
        "model",
    )
    assert [(r["table"], r["kind"]) for r in recent["tables"]] == [("post", "read")]
    # a collection whose target the class never states, an untyped `select(cls)`: nothing
    assert audit["tables"] == [] and untyped["tables"] == []


@pytest.mark.asyncio
async def test_unresolved_accesses_are_locatable_and_a_model_name_answers(tmp_path: Path):
    async with Client(mcp) as c:
        ws = await _repo(c, tmp_path, "flask")
        inv = await _fda(c, ws, limit=500)
        paged = await _fda(c, ws, limit=2)
        nxt = await _fda(c, ws, limit=2, unresolved_cursor=paged["unresolved_next_cursor"])
        by_model = await _fda(c, ws, table="account")
        resolved = await _fda(c, ws, table="Staff")
        nothing = await _fda(c, ws, table="nonesuch")

    acct = next(u for u in inv["unresolved_models"] if u["model"] == "Account")
    assert acct["accesses"] == [
        {
            "symbol": "plain.models.balance",
            "file": "plain/models.py:25",
            "kind": "read",
        }
    ]
    assert inv["counts"]["unresolved_models"] == len(inv["unresolved_models"]) >= 4
    assert len(paged["unresolved_models"]) == 2 and paged["unresolved_next_cursor"] == 2
    assert nxt["unresolved_models"][0] == inv["unresolved_models"][2]
    assert nxt["counts"] == paged["counts"] == inv["counts"]
    # `table=` that is no table but a model: answered, and it says which
    assert by_model["mode"] == "model" and by_model["matched"] == "model"
    assert by_model["tables"] == [] and "does not state" in by_model["note"]
    assert _rows(by_model)[0]["file"] == "plain/models.py:25"
    assert _rows(by_model)[0]["confidence"] == "unresolved"
    # a name that is a table wins over a model of the same name
    assert resolved["mode"] == "table" and resolved["table"] == "staff"
    assert nothing["isError"] is True


@pytest.mark.asyncio
async def test_an_ambiguous_collection_name_resolves_only_inside_its_own_class(
    tmp_path: Path,
):
    root = tmp_path / "amb"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text(
        "from flask_sqlalchemy import SQLAlchemy\n\ndb = SQLAlchemy()\n", encoding="utf-8"
    )
    (root / "app" / "models.py").write_text(
        """import sqlalchemy.orm as so

from app import db


class Folder(db.Model):
    id: so.Mapped[int] = so.mapped_column(primary_key=True)
    children: so.WriteOnlyMapped["File"] = so.relationship()

    def files(self):
        return db.session.scalars(self.children.select())


class Menu(db.Model):
    id: so.Mapped[int] = so.mapped_column(primary_key=True)
    children: so.WriteOnlyMapped["Dish"] = so.relationship()

    def dishes(self):
        return db.session.scalars(self.children.select())


def outside(node):
    import sqlalchemy as sa

    return db.session.scalars(node.children.select(), sa.text("x"))
""",
        encoding="utf-8",
    )
    async with Client(mcp) as c:
        out = (await c.call_tool("index_project", {"workspace": str(root)})).data
        assert "error" not in out, out
        files = await _sym(c, root, "app.models.Folder.files")
        dishes = await _sym(c, root, "app.models.Menu.dishes")
        outside = await _sym(c, root, "app.models.outside")

    # inside the declaring class the owner is certain (the target has no model: unresolved)
    assert [(r["model"], r["table"]) for r in files["tables"]] == [("File", None)]
    assert [(r["model"], r["table"]) for r in dishes["tables"]] == [("Dish", None)]
    # two classes declare `children`: from any other receiver it is not known which
    assert outside["tables"] == []


@pytest.mark.asyncio
async def test_reads_through_a_many_to_many_collection_reach_its_association_table(
    tmp_path: Path,
):
    """`self.following.select()`, a count over it and a join through it read the
    association table as well as the target; add / remove still only write it."""
    root = tmp_path / "m2m"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text(
        "from flask_sqlalchemy import SQLAlchemy\n\ndb = SQLAlchemy()\n", encoding="utf-8"
    )
    (root / "app" / "models.py").write_text(
        """import sqlalchemy as sa
import sqlalchemy.orm as so

from app import db

followers = sa.Table(
    "followers",
    db.metadata,
    sa.Column("follower_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
    sa.Column("followed_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True),
)


class User(db.Model):
    id: so.Mapped[int] = so.mapped_column(primary_key=True)
    following: so.WriteOnlyMapped["User"] = so.relationship(secondary=followers)
    followers: so.WriteOnlyMapped["User"] = so.relationship(secondary=followers)

    def follow(self, other):
        self.following.add(other)

    def is_following(self, other):
        return db.session.scalar(self.following.select().where(User.id == other.id))

    def following_count(self):
        return db.session.scalar(
            sa.select(sa.func.count()).select_from(self.following.select().subquery())
        )

    def feed(self):
        Author = so.aliased(User)
        return sa.select(User).join(Author.followers.of_type(Author), isouter=True)

    def raw(self):
        return db.session.execute(sa.select(followers.c.follower_id).select_from(followers))
""",
        encoding="utf-8",
    )
    async with Client(mcp) as c:
        out = (await c.call_tool("index_project", {"workspace": str(root)})).data
        assert "error" not in out, out
        follow = await _sym(c, root, "app.models.User.follow")
        is_following = await _sym(c, root, "app.models.User.is_following")
        count = await _sym(c, root, "app.models.User.following_count")
        feed = await _sym(c, root, "app.models.User.feed")
        raw = await _sym(c, root, "app.models.User.raw")

    def pairs(sym: dict[str, Any]) -> set[tuple[str, str]]:
        return {(r["table"], r["kind"]) for r in sym["tables"]}

    assert pairs(follow) == {("followers", "write")}
    assert pairs(is_following) == {("followers", "read"), ("user", "read")}
    assert pairs(count) == {("followers", "read"), ("user", "read")}
    assert ("followers", "read") in pairs(feed)
    assert pairs(raw) == {("followers", "read")}


# --------------------------------------------------------------------------
# Storage: migration 33
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_map_is_rebuilt_when_the_stored_shape_changes(tmp_path: Path):
    """v33 recreates the two derived tables with the wider shapes; a DB that
    already holds symbols is queued for re-extraction so the new rows fill."""
    from livespec_mcp.storage.db import clear_reextract_flag, connect, peek_reextract_flag

    db = tmp_path / "old.db"
    conn = connect(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(data_model)")}
    assert {"owner", "secondary", "detail"} <= cols
    clear_reextract_flag(conn)
    conn.execute("DROP TABLE data_access")
    conn.execute("DROP TABLE data_model")
    conn.execute("DELETE FROM schema_migrations WHERE version IN (31, 33)")
    conn.commit()
    conn.close()
    conn = connect(db)  # 31 then 33 again on an empty DB: no re-extract
    assert peek_reextract_flag(conn) is False
    conn.execute("INSERT INTO project(id, name, root) VALUES(1, 'p', '/p')")
    conn.execute(
        "INSERT INTO file(id, project_id, path, language, content_hash, line_count, mtime) "
        "VALUES(1, 1, 'm.py', 'python', 'h', 2, 0)"
    )
    conn.execute(
        "INSERT INTO symbol(id, file_id, name, qualified_name, kind, start_line, end_line) "
        "VALUES(1, 1, 'f', 'm.f', 'function', 1, 2)"
    )
    conn.commit()
    conn.execute("DELETE FROM schema_migrations WHERE version=33")
    conn.commit()
    conn.close()
    conn = connect(db)  # a v31 DB that already holds symbols
    assert peek_reextract_flag(conn) is True
    conn.execute(
        "INSERT INTO data_model(file_id, name, role, mechanism, table_name, table_source, line,"
        " owner, secondary, detail) VALUES(1, 'user', 'model', 'sqlalchemy', 'user',"
        " 'flask_candidate', 1, 'User', 'followers', 'write_only')"
    )
    conn.execute(
        "INSERT INTO data_access(symbol_id, access, mechanism, line, confidence, ref_kind,"
        " ref_name) VALUES(1, 'read', 'sqlalchemy', 1, 'model', 'relationship', 'User.posts:select')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO data_model(file_id, name, role, mechanism, line) "
            "VALUES(1, 'x', 'bogus', 'sql', 1)"
        )
    conn.close()
