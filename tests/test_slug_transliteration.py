"""Accented requirement names get readable ids; the old ids keep working.

Before transliteration every non-ASCII letter of an OpenSpec heading became a
hyphen, so a Spanish requirement "Validación de contraseñas" answered to
`auth-validaci-n-de-contrase-as`. Those ids live on in existing databases and
in `@spec:` annotations, so the fix renames the row in place and keeps the old
id as an alias instead of orphaning either.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.md_specs import (
    _legacy_ospec_spec_id,
    _legacy_slugify,
    _ospec_spec_id,
    _slugify,
    parse_openspec_markdown,
)
from livespec_mcp.domain.spec_alias import record_alias, resolve_spec_id
from livespec_mcp.server import mcp
from livespec_mcp.state import get_state

AUTH_SPEC = """\
# Autenticación

## Purpose
Quién entra y cómo.

## Requirements

### Requirement: Validación de contraseñas
El sistema SHALL rechazar contraseñas de menos de 12 caracteres.

#### Scenario: Contraseña corta
- **WHEN** la contraseña tiene 8 caracteres
- **THEN** se rechaza con un error específico

### Requirement: Único DLT
El sistema SHALL tener siempre un DLT.

#### Scenario: Quitar el último
- **WHEN** se quita el último DLT
- **THEN** la operación falla

### Requirement: Plain ascii rule
The system SHALL keep ascii ids unchanged.

#### Scenario: Ascii
- **WHEN** a heading is ascii
- **THEN** its id is what it always was
"""

NEW_PWD = "auth-validacion-de-contrasenas"
OLD_PWD = "auth-validaci-n-de-contrase-as"
NEW_DLT = "auth-unico-dlt"
OLD_DLT = "auth-nico-dlt"
ASCII = "auth-plain-ascii-rule"


# ---------- the slug itself ----------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Spanish
        ("Validación de contraseñas", "validacion-de-contrasenas"),
        ("Único DLT", "unico-dlt"),
        ("Pingüino año", "pinguino-ano"),
        # Portuguese
        ("Ação de exclusão", "acao-de-exclusao"),
        ("Configuração", "configuracao"),
        # German
        ("Straße und Größe", "strasse-und-grosse"),
        ("Übermäßig", "ubermassig"),
        # French
        ("Garçon cœur élève", "garcon-coeur-eleve"),
        ("Æther Ørsted", "aether-orsted"),
        # Polish / Vietnamese letters NFKD does not decompose
        ("Łódź", "lodz"),
        ("Đường", "duong"),
        # ASCII is untouched by the change
        ("User login", "user-login"),
        ("  --Edge  case!!  ", "edge-case"),
        ("", "requirement"),
        ("日本語", "requirement"),
    ],
)
def test_slugify_transliterates(name: str, expected: str):
    assert _slugify(name) == expected
    assert _slugify(name) == _slugify(name)  # deterministic
    assert _slugify(expected) == expected  # idempotent on its own output


def test_legacy_slugify_is_the_old_algorithm():
    assert _legacy_slugify("Validación de contraseñas") == "validaci-n-de-contrase-as"
    assert _legacy_slugify("Único") == "nico"
    assert _legacy_slugify("User login") == "user-login"
    assert _legacy_ospec_spec_id("Único DLT", "auth") == OLD_DLT
    assert _ospec_spec_id("Único DLT", "auth") == NEW_DLT


def test_parser_emits_transliterated_ids():
    ids = [s.spec_id for s in parse_openspec_markdown(AUTH_SPEC, capability="auth")]
    assert ids == [NEW_PWD, NEW_DLT, ASCII]


# ---------- import / sync ----------


def _write_auth(ws: Path, folder: str = "auth") -> Path:
    tree = ws / "openspec" / "specs" / folder
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "spec.md").write_text(AUTH_SPEC, encoding="utf-8")
    return tree


async def _spec_ids(c: Client) -> set[str]:
    return {s["spec_id"] for s in (await c.call_tool("list_specs", {})).data["specs"]}


@pytest.mark.asyncio
async def test_sync_is_accent_free_and_idempotent(sample_repo):
    _write_auth(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        first = (await c.call_tool("sync_openspec", {})).data["specs"]
        assert first["created"] == 3, first
        assert await _spec_ids(c) == {NEW_PWD, NEW_DLT, ASCII}

        again = (await c.call_tool("sync_openspec", {})).data["specs"]
        assert again["created"] == 0 and again["updated"] == 3, again
        assert "renamed_legacy_ids" not in again and "retired" not in again, again
        assert await _spec_ids(c) == {NEW_PWD, NEW_DLT, ASCII}

        # The single-file importer is idempotent too, and agrees on the ids.
        imp = (
            await c.call_tool("import_specs_from_markdown", {"path": "openspec/specs/auth/spec.md"})
        ).data
        assert imp["parsed"] == 3, imp
        assert await _spec_ids(c) >= {NEW_PWD, NEW_DLT, ASCII}


def _rewind_to_legacy_ids(ws: Path) -> int:
    """Turn a synced DB into one written before transliteration: specs under
    their legacy slugs, no aliases. Returns the password spec's pk."""
    st = get_state(str(ws))
    for new, old in ((NEW_PWD, OLD_PWD), (NEW_DLT, OLD_DLT)):
        st.conn.execute(
            "UPDATE spec SET spec_id=? WHERE project_id=? AND spec_id=?",
            (old, st.project_id, new),
        )
    st.conn.execute("DELETE FROM spec_alias")
    st.conn.commit()
    return int(
        st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (st.project_id, OLD_PWD)
        ).fetchone()["id"]
    )


@pytest.mark.asyncio
async def test_legacy_row_is_renamed_with_everything_it_carries(sample_repo):
    _write_auth(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        pk = _rewind_to_legacy_ids(sample_repo)
        assert await _spec_ids(c) == {OLD_PWD, OLD_DLT, ASCII}

        # Traceability hung on the legacy ids, as an old DB would have it.
        await c.call_tool(
            "link_spec_symbol", {"spec_id": OLD_PWD, "symbol_qname": "pkg.auth.login"}
        )
        await c.call_tool(
            "link_scenario_symbol",
            {
                "spec_id": OLD_PWD,
                "scenario_name": "Contraseña corta",
                "symbol_qname": "pkg.auth.verify",
                "relation": "tests",
            },
        )
        await c.call_tool(
            "link_spec_dependency", {"parent_spec_id": OLD_PWD, "child_spec_id": OLD_DLT}
        )
        st = get_state(str(sample_repo))
        st.conn.execute(
            "INSERT INTO spec_coverage_snapshot(project_id, ts, spec_id, ratio) "
            "VALUES(?, '2026-01-01T00:00:00', ?, 0.5)",
            (st.project_id, OLD_PWD),
        )
        st.conn.commit()

        synced = (await c.call_tool("sync_openspec", {})).data["specs"]
        assert synced["created"] == 0, synced
        assert sorted(synced["renamed_legacy_ids"], key=lambda r: r["from"]) == [
            {"from": OLD_DLT, "to": NEW_DLT},
            {"from": OLD_PWD, "to": NEW_PWD},
        ]
        assert "retired" not in synced, synced
        assert await _spec_ids(c) == {NEW_PWD, NEW_DLT, ASCII}

        row = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (st.project_id, NEW_PWD)
        ).fetchone()
        assert int(row["id"]) == pk  # same row, not a copy

        impl = (await c.call_tool("get_spec_implementation", {"spec_id": NEW_PWD})).data
        assert [s["qualified_name"] for s in impl["symbols"]] == ["pkg.auth.login"]
        scen = {s["name"]: s for s in impl["scenarios"]}
        assert [x["qualified_name"] for x in scen["Contraseña corta"]["symbols"]] == [
            "pkg.auth.verify"
        ]
        assert "deprecated_spec_id" not in impl

        graph = (
            await c.call_tool(
                "get_spec_dependency_graph", {"spec_id": NEW_PWD, "direction": "forward"}
            )
        ).data
        assert graph["edges"] == [{"parent": NEW_PWD, "child": NEW_DLT, "kind": "requires"}]

        history = st.conn.execute(
            "SELECT spec_id FROM spec_coverage_snapshot WHERE project_id=? AND ts=?",
            (st.project_id, "2026-01-01T00:00:00"),
        ).fetchall()
        assert [r["spec_id"] for r in history] == [NEW_PWD]

        # Idempotent from here: the rename happened once.
        again = (await c.call_tool("sync_openspec", {})).data["specs"]
        assert "renamed_legacy_ids" not in again, again


@pytest.mark.asyncio
async def test_tools_accept_the_legacy_id(sample_repo):
    _write_auth(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        note = {"id": OLD_PWD, "use": NEW_PWD}

        linked = (
            await c.call_tool(
                "link_spec_symbol", {"spec_id": OLD_PWD, "symbol_qname": "pkg.auth.login"}
            )
        ).data
        assert linked["linked"] is True and linked["spec_id"] == NEW_PWD
        assert linked["deprecated_spec_id"] == note

        impl = (await c.call_tool("get_spec_implementation", {"spec_id": OLD_PWD})).data
        assert impl["spec"]["spec_id"] == NEW_PWD
        assert impl["deprecated_spec_id"] == note
        assert [s["qualified_name"] for s in impl["symbols"]] == ["pkg.auth.login"]

        impact = (
            await c.call_tool("analyze_impact", {"target_type": "spec", "target": OLD_PWD})
        ).data
        assert impact["spec_id"] == NEW_PWD
        assert impact["deprecated_spec_id"] == note

        dep = (
            await c.call_tool(
                "link_spec_dependency", {"parent_spec_id": OLD_PWD, "child_spec_id": OLD_DLT}
            )
        ).data
        assert dep["linked"] is True
        assert (dep["parent"], dep["child"]) == (NEW_PWD, NEW_DLT)
        assert dep["deprecated_spec_ids"] == [note, {"id": OLD_DLT, "use": NEW_DLT}]

        bulk = (
            await c.call_tool(
                "bulk_link_spec_symbols",
                {"mappings": [{"spec_id": OLD_DLT, "symbol_qname": "pkg.auth.verify"}]},
            )
        ).data
        assert bulk["linked"] == 1, bulk
        assert bulk["results"][0]["spec_id"] == NEW_DLT
        assert bulk["results"][0]["deprecated_spec_id"] == {"id": OLD_DLT, "use": NEW_DLT}

        # A live id is never reported as deprecated.
        plain = (await c.call_tool("get_spec_implementation", {"spec_id": ASCII})).data
        assert "deprecated_spec_id" not in plain


def test_an_alias_never_shadows_a_live_id(sample_repo, workspace):
    st = get_state(str(workspace), create=True)
    pid = st.project_id
    st.conn.execute("INSERT INTO spec(project_id, spec_id, title) VALUES(?, 'a-live', 'A')", (pid,))
    st.conn.execute("INSERT INTO spec(project_id, spec_id, title) VALUES(?, 'b-new', 'B')", (pid,))
    b_pk = int(
        st.conn.execute(
            "SELECT id FROM spec WHERE spec_id='b-new' AND project_id=?", (pid,)
        ).fetchone()["id"]
    )
    assert record_alias(st.conn, pid, "a-live", b_pk) is False
    assert record_alias(st.conn, pid, "b-old", b_pk) is True
    assert resolve_spec_id(st.conn, pid, "a-live") == ("a-live", None)
    assert resolve_spec_id(st.conn, pid, "b-old") == ("b-new", "b-old")
    assert resolve_spec_id(st.conn, pid, "nope") == ("nope", None)


# ---------- annotations ----------


def _annotated_code(ws: Path) -> None:
    (ws / "pkg" / "passwords.py").write_text(
        "def check_length(pw):\n"
        f'    """Reject short passwords.\n\n    @spec:{OLD_PWD}\n    """\n'
        "    return len(pw) >= 12\n"
        "\n"
        "def keep_one_dlt(team):\n"
        f'    """@spec:{NEW_DLT}"""\n'
        "    return team\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_legacy_annotation_links_and_is_reported_deprecated(sample_repo):
    _annotated_code(sample_repo)
    _write_auth(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})  # fresh DB: no legacy row at all
        scan = (await c.call_tool("scan_spec_annotations", {})).data
        assert scan["deprecated_annotation_ids"] == [{"id": OLD_PWD, "use": NEW_PWD}], scan
        sample = scan["deprecated_annotation_sample"]
        assert sample[0]["qualified_name"] == "pkg.passwords.check_length"
        assert sample[0]["file_path"].endswith("passwords.py")
        assert OLD_PWD not in scan.get("unknown_annotation_ids", [])

        impl = (await c.call_tool("get_spec_implementation", {"spec_id": NEW_PWD})).data
        assert [s["qualified_name"] for s in impl["symbols"]] == ["pkg.passwords.check_length"]
        assert impl["symbols"][0]["source"] == "annotation"
        dlt = (await c.call_tool("get_spec_implementation", {"spec_id": NEW_DLT})).data
        assert [s["qualified_name"] for s in dlt["symbols"]] == ["pkg.passwords.keep_one_dlt"]

        # The indexer's own re-link (on a changed file) resolves the alias too.
        (sample_repo / "pkg" / "passwords.py").write_text(
            (sample_repo / "pkg" / "passwords.py").read_text() + "\n# touched\n"
        )
        await c.call_tool("index_project", {})
        impl = (await c.call_tool("get_spec_implementation", {"spec_id": NEW_PWD})).data
        assert [s["qualified_name"] for s in impl["symbols"]] == ["pkg.passwords.check_length"]


@pytest.mark.asyncio
async def test_annotation_on_a_renamed_legacy_row_keeps_linking(sample_repo):
    _annotated_code(sample_repo)
    _write_auth(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        _rewind_to_legacy_ids(sample_repo)
        await c.call_tool("scan_spec_annotations", {})  # links under the legacy id
        await c.call_tool("sync_openspec", {})  # renames it
        scan = (await c.call_tool("scan_spec_annotations", {})).data
        assert scan["deprecated_annotation_ids"] == [{"id": OLD_PWD, "use": NEW_PWD}], scan
        impl = (await c.call_tool("get_spec_implementation", {"spec_id": NEW_PWD})).data
        assert [s["qualified_name"] for s in impl["symbols"]] == ["pkg.passwords.check_length"]


# ---------- changes + export ----------


RENAME_DELTA = """\
## RENAMED Requirements
- FROM: `### Requirement: Único DLT`
- TO: `### Requirement: Al menos un DLT`
"""


@pytest.mark.asyncio
async def test_renamed_delta_finds_a_legacy_source(sample_repo):
    _write_auth(sample_repo)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        _rewind_to_legacy_ids(sample_repo)
        # Re-pointing every row would also rename OLD_DLT; keep it legacy by
        # ingesting the change only (no canonical re-sync in between).
        ch = sample_repo / "openspec" / "changes" / "rename-dlt" / "specs" / "auth"
        ch.mkdir(parents=True)
        (ch / "spec.md").write_text(RENAME_DELTA, encoding="utf-8")
        await c.call_tool(
            "link_spec_symbol", {"spec_id": OLD_DLT, "symbol_qname": "pkg.auth.login"}
        )
        st = get_state(str(sample_repo))
        from livespec_mcp.domain.openspec_changes import import_changes_tree

        import_changes_tree(st, sample_repo / "openspec")
        res = (await c.call_tool("apply_spec_change", {"name": "rename-dlt"})).data
        assert res["applied"]["renamed"] == 1, res
        assert res["warnings"] == [], res
        ids = await _spec_ids(c)
        assert "auth-al-menos-un-dlt" in ids and OLD_DLT not in ids
        impl = (
            await c.call_tool("get_spec_implementation", {"spec_id": "auth-al-menos-un-dlt"})
        ).data
        assert [s["qualified_name"] for s in impl["symbols"]] == ["pkg.auth.login"]


@pytest.mark.asyncio
async def test_export_writes_accent_free_folder_names(sample_repo):
    _write_auth(sample_repo, folder="autenticación")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("sync_openspec", {})
        ids = await _spec_ids(c)
        assert "autenticacion-validacion-de-contrasenas" in ids, ids
        out = (await c.call_tool("export_openspec", {"out_dir": "out"})).data
        assert "specs/autenticacion/spec.md" in out["files"], out
        for f in out["files"]:
            assert f.isascii(), f
        assert (sample_repo / "out" / "specs" / "autenticacion" / "spec.md").is_file()
