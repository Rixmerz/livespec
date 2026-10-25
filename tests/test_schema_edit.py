"""Exact per-declaration edits for one field change (OpenSpec change
virtual-schema-first, phase 3: the coordinated schema change plan).

Pure text: each test edits a source string with `edit_declaration`, splices
the edits in with `apply_text_edits`, and reads the result back with the
indexer's own `extract()`. The edited source must still parse (tree-sitter
and the SQL reader both recover from errors, so a clean re-extraction alone
proves nothing), the field must change as asked, and every other field must
keep its shape.
"""

from __future__ import annotations

import ast
import itertools
import sqlite3
from pathlib import Path

import pytest

from livespec_mcp.domain.extractors import ExtractedField, extract
from livespec_mcp.domain.languages import get_parser
from livespec_mcp.domain.schema_drift import SchemaField, SchemaSide, SymbolRef, compatible_types
from livespec_mcp.domain.schema_edit import (
    PLAN_CHANGE_KINDS,
    RENDERABLE_TYPES,
    DeclarationEdit,
    PlannedChange,
    TextEdit,
    apply_text_edits,
    edit_declaration,
    name_style,
    plan_action,
    render_name,
    render_type,
)
from livespec_mcp.domain.schema_types import FLAVORS

from .conftest import requires_grammar

PYDANTIC = """from pydantic import BaseModel


class User(BaseModel):
    id: int
    email: str
    tags: list[str] = []
    created_at: str | None = None
"""

# Required fields only, `email` last: a dataclass rejects a required field
# after a defaulted one, so only the last field may gain a default.
DATACLASS = """from dataclasses import dataclass


@dataclass
class User:
    id: int
    created_at: str | None
    email: str
"""

TS_INTERFACE = """export interface User {
  id: number;
  email: string;
  createdAt?: string | null;
}
"""

# One line, ',' between members and none after the last.
TS_TYPE = """export type User = { id: number, email: string, createdAt?: string | null };
"""

ZOD = """import { z } from "zod";

export const User = z.object({
  id: z.number().int(),
  email: z.string().email(),
  createdAt: z.string().datetime().nullish(),
});
"""

# A table constraint after the last column: a new column goes before it.
# BIGINT, not INTEGER: SQLite fills an INTEGER PRIMARY KEY itself, so
# retyping one would rightly change its `optional` too.
SQL = """CREATE TABLE users (
  id BIGINT NOT NULL,
  email TEXT NOT NULL,
  created_at TIMESTAMP,
  PRIMARY KEY (id)
);
"""

DECLS: dict[str, tuple[str, str, str]] = {
    "pydantic": ("models.py", PYDANTIC, "User"),
    "dataclass": ("models.py", DATACLASS, "User"),
    "ts_interface": ("user.ts", TS_INTERFACE, "User"),
    "ts_type": ("user.ts", TS_TYPE, "User"),
    "zod": ("user.ts", ZOD, "User"),
    "sql_table": ("schema.sql", SQL, "users"),
}

CHANGES: dict[str, PlannedChange] = {
    "add": PlannedChange("add", "contact_email", new_type="string"),
    "remove": PlannedChange("remove", "email"),
    "rename": PlannedChange("rename", "email", new_name="contact_email"),
    "retype": PlannedChange("retype", "id", new_type="string"),
    "optionality": PlannedChange("optionality", "email", optional=True, nullable=True),
}

SPELLING = {
    "pydantic": "contact_email",
    "dataclass": "contact_email",
    "sql_table": "contact_email",
    "ts_interface": "contactEmail",
    "ts_type": "contactEmail",
    "zod": "contactEmail",
}


def _needs_grammar(file: str) -> None:
    if file.endswith((".ts", ".tsx")):
        requires_grammar("typescript")


def _schema_fields(file: str, source: str, name: str) -> list[ExtractedField]:
    result = extract(Path(file), source, Path("."))[1]
    found = [s for s in result.schemas if s.src_qname.rsplit(".", 1)[-1] == name]
    assert len(found) == 1, [s.src_qname for s in result.schemas]
    return found[0].fields


def _decl(file: str, source: str, name: str) -> tuple[SchemaSide, int, int]:
    result = extract(Path(file), source, Path("."))[1]
    schema = next(s for s in result.schemas if s.src_qname.rsplit(".", 1)[-1] == name)
    symbol = next(s for s in result.symbols if s.qualified_name == schema.src_qname)
    fields = tuple(
        SchemaField(
            f.name,
            f.field_key,
            f.type,
            f.item_type,
            f.ref_target,
            f.raw_type,
            f.optional,
            f.nullable,
            f.line,
        )
        for f in schema.fields
    )
    ref = SymbolRef(schema.src_qname, "repo", file, symbol.start_line)
    return SchemaSide(ref, schema.flavor, fields), symbol.start_line, symbol.end_line


def _plan(
    file: str, source: str, name: str, change: PlannedChange
) -> tuple[str, DeclarationEdit | None]:
    """The action and, for an action that edits, the declaration's edit."""
    side, start, end = _decl(file, source, name)
    action, field = plan_action(side, change)
    if action not in ("add", "remove", "rename", "retype", "set"):
        return action, None
    return action, edit_declaration(side.flavor, source, file, start, end, field, action, change)


def _applied(file: str, source: str, name: str, change: PlannedChange) -> str:
    action, edit = _plan(file, source, name, change)
    assert edit is not None, action
    assert edit.status == "edit", edit.reason
    # Contract: sorted, non-overlapping, no reason, a hint only for SQL.
    assert edit.edits and edit.reason is None
    assert all(a.end_line < b.start_line for a, b in itertools.pairwise(edit.edits))
    assert (edit.migration_hint is not None) == file.endswith(".sql")
    out = apply_text_edits(source, edit.edits)
    _assert_parses(file, out)
    return out


def _assert_parses(file: str, source: str) -> None:
    if file.endswith(".py"):
        ast.parse(source)
    elif file.endswith(".sql"):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(source)
        finally:
            conn.close()
    else:
        tree = get_parser("typescript").parse(source.encode("utf-8"))
        assert not tree.root_node.has_error, source


def _shape(f: ExtractedField) -> tuple:
    return (f.name, f.field_key, f.type, f.item_type, f.optional, f.nullable)


def _carries(flavor: str, f: ExtractedField, neutral: str) -> bool:
    return f.type == neutral or compatible_types(flavor, f.type, f.item_type, "", neutral, None)


# ---------- every flavor x every kind, read back by the indexer ----------


@pytest.mark.parametrize("kind", PLAN_CHANGE_KINDS)
@pytest.mark.parametrize("flavor", FLAVORS)
def test_every_change_round_trips_through_extract(flavor: str, kind: str):
    file, source, name = DECLS[flavor]
    _needs_grammar(file)
    change = CHANGES[kind]
    before = _schema_fields(file, source, name)
    after = _schema_fields(file, _applied(file, source, name, change), name)
    by_key = {f.field_key: f for f in after}

    touched = {"add": set(), "remove": {"email"}, "rename": {"email"}, "retype": {"id"}}.get(
        kind, {"email"}
    )
    untouched_before = [_shape(f) for f in before if f.field_key not in touched]
    untouched_after = [
        _shape(f) for f in after if f.field_key not in touched and f.field_key != "contactemail"
    ]
    assert untouched_after == untouched_before

    old = {f.field_key: f for f in before}
    if kind == "add":
        new = by_key["contactemail"]
        assert new.name == SPELLING[flavor]
        assert _carries(flavor, new, "string")
        assert (new.optional, new.nullable) == (False, False)
    elif kind == "remove":
        assert "email" not in by_key
        assert len(after) == len(before) - 1
    elif kind == "rename":
        assert "email" not in by_key
        new = by_key["contactemail"]
        assert new.name == SPELLING[flavor]
        assert (new.type, new.item_type, new.optional, new.nullable) == (
            old["email"].type,
            old["email"].item_type,
            old["email"].optional,
            old["email"].nullable,
        )
        assert [f.field_key for f in after].index("contactemail") == [
            f.field_key for f in before
        ].index("email")
    elif kind == "retype":
        assert by_key["id"].type != old["id"].type
        assert _carries(flavor, by_key["id"], "string")
        assert (by_key["id"].optional, by_key["id"].nullable) == (
            old["id"].optional,
            old["id"].nullable,
        )
    else:
        email = by_key["email"]
        assert email.type == old["email"].type
        assert email.nullable is True
        # A column's "optional" is the database's to fill in, never edited.
        expected_optional = old["email"].optional if flavor == "sql_table" else True
        assert email.optional is expected_optional


@pytest.mark.parametrize("neutral", RENDERABLE_TYPES)
@pytest.mark.parametrize("flavor", FLAVORS)
def test_every_renderable_type_reads_back_as_the_type_asked(flavor: str, neutral: str):
    """Contract invariant: re-extracting the edited source maps the field to
    a type that carries the requested one under the side's flavor."""
    file, source, name = DECLS[flavor]
    _needs_grammar(file)
    added = _schema_fields(
        file, _applied(file, source, name, PlannedChange("add", "extra", new_type=neutral)), name
    )
    assert _carries(flavor, {f.field_key: f for f in added}["extra"], neutral)

    action, edit = _plan(file, source, name, PlannedChange("retype", "email", new_type=neutral))
    if action == "compatible":
        return  # already carries it: nothing to edit
    retyped = _schema_fields(file, apply_text_edits(source, edit.edits), name)
    assert _carries(flavor, {f.field_key: f for f in retyped}["email"], neutral)


# ---------- names and aliases ----------


def test_rename_spelling_follows_each_language():
    spelled = {}
    for flavor, (file, source, name) in DECLS.items():
        _needs_grammar(file)
        out = _applied(file, source, name, CHANGES["rename"])
        spelled[flavor] = next(
            f.name for f in _schema_fields(file, out, name) if f.field_key == "contactemail"
        )
    assert spelled == SPELLING


def test_aliased_pydantic_rename_rewrites_only_the_alias_literal():
    # The non-ASCII description sits before the alias on the same line: ast
    # columns are UTF-8 bytes, and the edit must land on characters.
    source = (
        "from pydantic import BaseModel, Field\n"
        "\n"
        "\n"
        "class User(BaseModel):\n"
        "    id: int\n"
        "    email: str = Field(description='é-mail', alias='emailAddress')\n"
    )
    _, edit = _plan(
        "m.py", source, "User", PlannedChange("rename", "emailAddress", new_name="contact_email")
    )
    assert edit.status == "edit", edit.reason
    assert [e.as_payload() for e in edit.edits] == [
        {
            "file": "m.py",
            "start_line": 6,
            "end_line": 6,
            "old_text": "    email: str = Field(description='é-mail', alias='emailAddress')\n",
            "new_text": "    email: str = Field(description='é-mail', alias='contactEmail')\n",
        }
    ]
    fields = {
        f.name: f for f in _schema_fields("m.py", apply_text_edits(source, edit.edits), "User")
    }
    assert (fields["email"].field_key, fields["email"].wire_name) == (
        "contactemail",
        "contactEmail",
    )


def test_annotated_alias_is_renamed_where_the_extractor_reads_it():
    source = (
        "from typing import Annotated\n"
        "from pydantic import BaseModel, Field\n"
        "\n"
        "\n"
        "class User(BaseModel):\n"
        '    email: Annotated[str, Field(alias="mail")]\n'
        "    id: int\n"
    )
    out = _applied("m.py", source, "User", PlannedChange("rename", "mail", new_name="contact"))
    assert '    email: Annotated[str, Field(alias="contact")]\n' in out
    assert {f.name: f.field_key for f in _schema_fields("m.py", out, "User")}["email"] == "contact"


def test_an_alias_keyed_like_its_attribute_moves_with_it():
    # `user_id` and `userId` share one key, so the field is not "aliased"
    # away from its attribute, yet the alias still keys it.
    source = (
        "from pydantic import BaseModel, Field\n"
        "\n"
        "\n"
        "class User(BaseModel):\n"
        '    user_id: int = Field(alias="userId")\n'
        "    name: str\n"
    )
    out = _applied(
        "m.py", source, "User", PlannedChange("rename", "user_id", new_name="account_id")
    )
    assert '    account_id: int = Field(alias="accountId")\n' in out
    keys = [f.field_key for f in _schema_fields("m.py", out, "User")]
    assert keys == ["accountid", "name"]


def test_name_style_and_render_name():
    assert [name_style(n) for n in ("created_at", "x-request-id", "createdAt", "CreatedAt")] == [
        "snake",
        "kebab",
        "camel",
        "pascal",
    ]
    assert name_style("email") is None
    assert name_style("ID") is None
    assert [render_name("contactEmail", s) for s in ("snake", "kebab", "camel", "pascal")] == [
        "contact_email",
        "contact-email",
        "contactEmail",
        "ContactEmail",
    ]
    assert render_name("contact email", "camel") == "contactEmail"
    assert render_name("contact_email", None) == "contact_email"


def test_render_type_covers_the_renderable_types_only():
    assert [render_type("sql_table", t) for t in RENDERABLE_TYPES] == [
        "TEXT",
        "INTEGER",
        "DOUBLE PRECISION",
        "NUMERIC",
        "BOOLEAN",
        "TIMESTAMP",
        "DATE",
        "UUID",
        "JSON",
    ]
    assert render_type("zod", "uuid") == "z.string().uuid()"
    assert render_type("dataclass", "json") == "dict[str, Any]"
    with pytest.raises(ValueError):
        render_type("pydantic", "list")


# ---------- Python imports and nullability ----------


def test_python_import_is_added_once_after_the_last_import():
    out = _applied("models.py", PYDANTIC, "User", PlannedChange("add", "price", new_type="decimal"))
    lines = out.splitlines()
    assert lines.count("from decimal import Decimal") == 1
    assert (
        lines.index("from decimal import Decimal")
        == lines.index("from pydantic import BaseModel") + 1
    )
    assert "    price: Decimal" in lines


def test_python_import_is_not_added_when_the_name_is_bound():
    source = "from decimal import Decimal\n" + PYDANTIC
    _, edit = _plan("models.py", source, "User", PlannedChange("retype", "id", new_type="decimal"))
    assert edit.status == "edit", edit.reason
    assert [(e.old_text, e.new_text) for e in edit.edits] == [
        ("    id: int\n", "    id: Decimal\n")
    ]


def test_python_nullable_follows_the_files_optional_style():
    source = (
        "from typing import Optional\n"
        "from pydantic import BaseModel\n"
        "\n"
        "\n"
        "class User(BaseModel):\n"
        "    nick: Optional[str] = None\n"
        "    email: str\n"
    )
    out = _applied("m.py", source, "User", PlannedChange("optionality", "email", nullable=True))
    assert "    email: Optional[str]\n" in out
    email = {f.name: f for f in _schema_fields("m.py", out, "User")}["email"]
    assert (email.optional, email.nullable) == (False, True)


def test_python_field_made_required_and_non_null():
    out = _applied(
        "models.py",
        PYDANTIC,
        "User",
        PlannedChange("optionality", "created_at", optional=False, nullable=False),
    )
    assert "    created_at: str\n" in out
    field = {f.name: f for f in _schema_fields("models.py", out, "User")}["created_at"]
    assert (field.optional, field.nullable) == (False, False)


# ---------- manual, never guessed ----------


def test_inherited_field_is_manual():
    source = (
        "from pydantic import BaseModel\n"
        "\n"
        "\n"
        "class Base(BaseModel):\n"
        "    id: int\n"
        "\n"
        "\n"
        "class User(Base):\n"
        "    email: str\n"
    )
    _, edit = _plan("m.py", source, "User", PlannedChange("remove", "id"))
    assert (edit.status, edit.edits, edit.migration_hint) == ("manual", (), None)
    assert "inherited" in edit.reason


def test_inherited_ts_field_is_manual():
    requires_grammar("typescript")
    source = (
        "interface Base {\n  id: number;\n}\ninterface User extends Base {\n  email: string;\n}\n"
    )
    _, edit = _plan("u.ts", source, "User", PlannedChange("retype", "id", new_type="string"))
    assert edit.status == "manual"
    assert "inherited" in edit.reason


@pytest.mark.parametrize(
    "change",
    [
        PlannedChange("add", "nick", new_type="string", optional=True),
        PlannedChange("optionality", "email", optional=True),
    ],
    ids=["add", "optionality"],
)
def test_optional_non_nullable_python_field_is_manual(change: PlannedChange):
    _, edit = _plan("models.py", PYDANTIC, "User", change)
    assert (edit.status, edit.edits, edit.reason) == ("manual", (), "needs a default value")


def test_dataclass_required_field_after_a_default_is_manual():
    source = DATACLASS.replace("    email: str\n", "    email: str = ''\n")
    _, edit = _plan("models.py", source, "User", CHANGES["add"])
    assert edit.status == "manual"
    assert "cannot follow one with a default" in edit.reason


def test_field_not_at_its_indexed_line_is_manual():
    side, start, end = _decl("models.py", PYDANTIC, "User")
    email = next(f for f in side.fields if f.name == "email")
    moved = SchemaField(**{**email.__dict__, "line": email.line + 1})
    edit = edit_declaration(
        "pydantic", PYDANTIC, "models.py", start, end, moved, "remove", CHANGES["remove"]
    )
    assert edit.status == "manual"
    assert "not at its indexed line" in edit.reason


# ---------- SQL migration hints ----------


@pytest.mark.parametrize(
    ("change", "hint"),
    [
        (
            PlannedChange("add", "nick", new_type="string"),
            "ALTER TABLE users ADD COLUMN nick TEXT NOT NULL;"
            " -- NOT NULL needs a DEFAULT or a backfill on a table with rows",
        ),
        (
            PlannedChange("add", "nick", new_type="string", nullable=True),
            "ALTER TABLE users ADD COLUMN nick TEXT;",
        ),
        (PlannedChange("remove", "email"), "ALTER TABLE users DROP COLUMN email;"),
        (
            PlannedChange("rename", "email", new_name="contactEmail"),
            "ALTER TABLE users RENAME COLUMN email TO contact_email;",
        ),
        (
            PlannedChange("retype", "id", new_type="uuid"),
            "ALTER TABLE users ALTER COLUMN id TYPE UUID;",
        ),
        (
            PlannedChange("optionality", "email", nullable=True),
            "ALTER TABLE users ALTER COLUMN email DROP NOT NULL;",
        ),
        (
            PlannedChange("optionality", "created_at", nullable=False),
            "ALTER TABLE users ALTER COLUMN created_at SET NOT NULL;",
        ),
    ],
    ids=["add", "add-nullable", "remove", "rename", "retype", "drop-not-null", "set-not-null"],
)
def test_sql_migration_hint(change: PlannedChange, hint: str):
    _, edit = _plan("schema.sql", SQL, "users", change)
    assert edit.status == "edit", edit.reason
    assert edit.migration_hint == hint
    _assert_parses("schema.sql", apply_text_edits(SQL, edit.edits))


def test_only_sql_edits_carry_a_migration_hint():
    _, edit = _plan("models.py", PYDANTIC, "User", CHANGES["remove"])
    assert edit.status == "edit"
    assert edit.migration_hint is None


def test_sql_removing_the_last_column_drops_the_comma_before_it():
    source = "CREATE TABLE t (a INTEGER, b TEXT);\n"
    out = _applied("t.sql", source, "t", PlannedChange("remove", "b"))
    assert out == "CREATE TABLE t (a INTEGER);\n"
    multi = "CREATE TABLE t (\n  a INTEGER,\n  b TEXT\n);\n"
    assert _applied("t.sql", multi, "t", PlannedChange("remove", "b")) == (
        "CREATE TABLE t (\n  a INTEGER\n);\n"
    )


def test_sql_column_named_by_a_table_constraint_is_manual():
    _, edit = _plan("schema.sql", SQL, "users", PlannedChange("rename", "id", new_name="key"))
    assert edit.status == "manual"
    assert "names 'id'" in edit.reason


# ---------- the file's own style ----------


def test_crlf_line_endings_are_kept():
    for file, source, name in (
        ("models.py", PYDANTIC, "User"),
        ("schema.sql", SQL, "users"),
        ("user.ts", ZOD, "User"),
    ):
        _needs_grammar(file)
        crlf = source.replace("\n", "\r\n")
        for change in (
            CHANGES["add"],
            CHANGES["remove"],
            PlannedChange("add", "p", new_type="json"),
        ):
            out = _applied(file, crlf, name, change)
            assert out.count("\n") == out.count("\r\n"), (file, change.kind, out)


def test_python_field_added_under_a_class_docstring():
    source = 'from pydantic import BaseModel\n\n\nclass User(BaseModel):\n    """A user."""\n'
    out = _applied("m.py", source, "User", PlannedChange("add", "nick", new_type="string"))
    assert out.endswith('    """A user."""\n    nick: str\n')
    assert ast.get_docstring(ast.parse(out).body[1]) == "A user."


def test_multi_line_python_field_is_removed_whole():
    source = (
        "from pydantic import BaseModel, Field\n"
        "\n"
        "\n"
        "class User(BaseModel):\n"
        "    id: int\n"
        "    email: str = Field(\n"
        '        alias="mail",\n'
        "    )\n"
        "    nick: str\n"
    )
    out = _applied("m.py", source, "User", PlannedChange("remove", "mail"))
    assert out.endswith("class User(BaseModel):\n    id: int\n    nick: str\n")


def test_removing_a_ts_member_takes_its_doc_comment():
    requires_grammar("typescript")
    source = (
        "interface User {\n"
        "  id: number; // the id\n"
        "  /** Where we write to. */\n"
        "  email: string;\n"
        "  nick: string;\n"
        "}\n"
    )
    out = _applied("user.tsx", source, "User", PlannedChange("remove", "email"))
    assert out == "interface User {\n  id: number; // the id\n  nick: string;\n}\n"


def test_quoted_names_keep_their_quotes():
    requires_grammar("typescript")
    sql = 'CREATE TABLE "t" ("a" INTEGER, "b" TEXT NULL);\n'
    assert _applied("t.sql", sql, "t", PlannedChange("rename", "b", new_name="bee")) == (
        'CREATE TABLE "t" ("a" INTEGER, "bee" TEXT NULL);\n'
    )
    assert _applied("t.sql", sql, "t", PlannedChange("add", "c", new_type="bool")) == (
        'CREATE TABLE "t" ("a" INTEGER, "b" TEXT NULL, "c" BOOLEAN NOT NULL);\n'
    )
    ts = "interface User {\n  'created-at': string;\n  id: number;\n}\n"
    out = _applied("u.ts", ts, "User", PlannedChange("rename", "created-at", new_name="updated_at"))
    assert "  'updated-at': string;\n" in out


def test_non_ascii_before_a_tree_sitter_edit():
    # tree-sitter positions are UTF-8 bytes; the edit must land on characters.
    requires_grammar("typescript")
    source = 'type User = { "naïve": string, email: string, id: number };\n'
    out = _applied("u.ts", source, "User", CHANGES["rename"])
    assert out == 'type User = { "naïve": string, contactEmail: string, id: number };\n'


def test_missing_grammar_is_manual(monkeypatch):
    from livespec_mcp.domain import extractors
    from livespec_mcp.domain.languages import GrammarUnavailableError

    def _boom(language: str):
        raise GrammarUnavailableError(language, RuntimeError("no network"))

    monkeypatch.setattr(extractors, "get_parser", _boom)
    edit = edit_declaration(
        "ts_interface", TS_INTERFACE, "user.ts", 1, 5, None, "add", CHANGES["add"]
    )
    assert edit.status == "manual"
    assert "grammar is unavailable" in edit.reason


def test_ts_optional_and_null_are_dropped_together():
    requires_grammar("typescript")
    out = _applied(
        "user.ts",
        TS_INTERFACE,
        "User",
        PlannedChange("optionality", "createdAt", optional=False, nullable=False),
    )
    assert "  createdAt: string;\n" in out


def test_zod_object_chained_with_a_callback_is_found():
    requires_grammar("typescript")
    source = ZOD.replace("});\n", "}).refine((u) => u.id > 0);\n")
    out = _applied("user.ts", source, "User", CHANGES["add"])
    assert "  contactEmail: z.string(),\n" in out


# ---------- plan_action and apply_text_edits ----------


def test_plan_action_for_optionality():
    side, _, _ = _decl("models.py", PYDANTIC, "User")
    assert plan_action(side, PlannedChange("optionality", "email", nullable=True))[0] == "set"
    assert plan_action(side, PlannedChange("optionality", "email", optional=False))[0] == (
        "unchanged"
    )
    assert plan_action(side, PlannedChange("optionality", "nope", nullable=True)) == (
        "absent",
        None,
    )


def test_plan_action_judges_sql_by_nullable_only():
    side, _, _ = _decl("schema.sql", SQL, "users")
    action, field = plan_action(side, PlannedChange("optionality", "email", optional=True))
    assert (action, field.name) == ("unchanged", "email")
    assert plan_action(side, PlannedChange("optionality", "email", nullable=True))[0] == "set"


def test_plan_action_delegates_the_shared_kinds():
    side, _, _ = _decl("models.py", PYDANTIC, "User")
    assert plan_action(side, CHANGES["add"])[0] == "add"
    assert plan_action(side, PlannedChange("add", "email", new_type="string"))[0] == "present"
    assert plan_action(side, PlannedChange("retype", "email", new_type="string"))[0] == "compatible"
    assert plan_action(side, CHANGES["rename"])[0] == "rename"


def test_apply_text_edits_refuses_a_stale_old_text():
    edit = TextEdit("m.py", 2, 2, "b = 2\n", "b = 3\n")
    assert apply_text_edits("a = 1\nb = 2\n", [edit]) == "a = 1\nb = 3\n"
    with pytest.raises(ValueError, match="changed since the edit was planned"):
        apply_text_edits("a = 1\nb = 9\n", [edit])


def test_payload_shapes():
    change = PlannedChange("rename", "email", new_name="contactEmail")
    assert change.as_payload() == {
        "kind": "rename",
        "field": "email",
        "field_key": "email",
        "new_name": "contactEmail",
        "new_key": "contactemail",
        "new_type": None,
        "optional": None,
        "nullable": None,
    }
    assert change.to_field_change().as_payload()["new_key"] == "contactemail"
    with pytest.raises(ValueError):
        PlannedChange("optionality", "email", nullable=True).to_field_change()
