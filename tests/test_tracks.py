"""Tracks: a named git range, why it exists, and per Spec what code changed.

The fixture is a two-commit repo whose second commit exercises every case the
symbol diff has to get right: an added function, a modified one, a
reformat-only one (must NOT count), a removed one, a method changed inside a
class (the method counts, the class does not), a file renamed with one of its
functions changed, a brand-new file with no Spec, and an OpenSpec change that
ADDs one requirement and MODIFIEs another.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

SPEC_MD = """# Core Specification

## Purpose

Core behaviour.

## Requirements

### Requirement: Alpha

The system SHALL alpha.

#### Scenario: Alpha works

- **WHEN** alpha
- **THEN** it works

### Requirement: Beta

The system SHALL beta.

#### Scenario: Beta works

- **WHEN** beta
- **THEN** it works
"""

CORE_V1 = '''def keep():
    return 1


def change_me(x):
    """@spec:core-alpha"""
    return x + 1


def reformat_only(a, b):
    return a+b


def going_away():
    """@spec:core-alpha"""
    return 0


class Box:
    def open(self):
        """@spec:core-beta"""
        return 1

    def close(self):
        return 2
'''

CORE_V2 = '''def keep():
    return 1


def change_me(x):
    """@spec:core-alpha"""
    return x + 2


def reformat_only(a,   b):
    # only whitespace and a comment changed
    return a  +  b


def new_fn():
    """@spec:core-beta"""
    return 3


class Box:
    def open(self):
        """@spec:core-beta"""
        return 10

    def close(self):
        return 2
'''

OLD_NAME = """def moved_same():
    return 5


def moved_changed():
    return 6
"""

DELTA_MD = """## ADDED Requirements

### Requirement: Gamma

The system SHALL gamma.

#### Scenario: Gamma works

- **WHEN** gamma
- **THEN** it works

## MODIFIED Requirements

### Requirement: Beta

The system SHALL beta, faster.

#### Scenario: Beta works

- **WHEN** beta
- **THEN** it works fast
"""


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def track_repo(workspace: Path) -> Path:
    ws = workspace
    (ws / "pkg").mkdir()
    (ws / "pkg" / "__init__.py").write_text("")
    (ws / "pkg" / "core.py").write_text(CORE_V1)
    (ws / "pkg" / "old_name.py").write_text(OLD_NAME)
    (ws / "openspec" / "specs" / "core").mkdir(parents=True)
    (ws / "openspec" / "specs" / "core" / "spec.md").write_text(SPEC_MD)
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "base")
    _git(ws, "tag", "base")
    (ws / "pkg" / "core.py").write_text(CORE_V2)
    _git(ws, "mv", "pkg/old_name.py", "pkg/new_name.py")
    (ws / "pkg" / "new_name.py").write_text(OLD_NAME.replace("return 6", "return 7"))
    (ws / "pkg" / "util.py").write_text("def helper():\n    return 'h'\n")
    delta = ws / "openspec" / "changes" / "add-gamma" / "specs" / "core"
    delta.mkdir(parents=True)
    (delta / "spec.md").write_text(DELTA_MD)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "head")
    return ws


async def _prepare(c: Client) -> None:
    await c.call_tool("index_project", {})
    await c.call_tool("sync_openspec", {})
    await c.call_tool("index_project", {"force": True})


def _names(items: list[dict]) -> set[str]:
    return {i["name"] for i in items}


@pytest.mark.asyncio
async def test_propose_track_symbol_level_draft(track_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        d = (await c.call_tool("propose_track", {"base_ref": "base", "head_ref": "HEAD"})).data
    assert d["base_sha"] == _git(track_repo, "rev-parse", "base")
    assert d["already_recorded"] is False
    specs = {s["spec_id"]: s for s in d["changed_specs"] + d["touched_specs"]}
    assert set(specs) == {"core-alpha", "core-beta", "core-gamma"}
    # each one has a delta or is the sole @spec: of code the range changed
    assert d["touched_specs"] == [] and {s["tier"] for s in specs.values()} == {"changed"}

    alpha = specs["core-alpha"]["symbols"]
    assert _names(alpha["modified"]) == {"change_me"}
    # removed: mapped through its base-side annotation
    assert _names(alpha["removed"]) == {"going_away"}

    beta = specs["core-beta"]["symbols"]
    assert _names(beta["added"]) == {"new_fn"}
    # the method changed; the class around it is not reported again
    assert _names(beta["modified"]) == {"Box.open"}

    # deltas: ADDED Gamma (unknown to the store yet), MODIFIED Beta
    assert [x["operation"] for x in specs["core-beta"]["deltas"]] == ["MODIFIED"]
    assert specs["core-gamma"]["deltas"][0]["operation"] == "ADDED"
    assert specs["core-gamma"]["known"] is False
    assert d["openspec_changes"] == ["add-gamma"]

    unlinked = d["unlinked"]
    every = _names(unlinked["added"]) | _names(unlinked["modified"]) | _names(unlinked["removed"])
    assert "helper" in _names(unlinked["added"])
    # rename: paired by local name, only the changed body counts
    moved = [m for m in unlinked["modified"] if m["name"] == "moved_changed"]
    assert moved and moved[0]["path"] == "pkg/new_name.py"
    assert moved[0]["old_path"] == "pkg/old_name.py"
    # reformat-only, untouched and pure-move symbols never appear
    assert {"reformat_only", "keep", "moved_same", "Box", "Box.close"}.isdisjoint(every)
    assert d["counts"]["symbols_added"] == 2  # new_fn, helper


@pytest.mark.asyncio
async def test_propose_track_summary_and_pagination(track_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        s = (
            await c.call_tool(
                "propose_track", {"base_ref": "base", "head_ref": "HEAD", "summary_only": True}
            )
        ).data
        assert "changed_specs" not in s
        assert s["changed_spec_ids"] == ["core-alpha", "core-beta", "core-gamma"]
        assert s["touched_spec_ids"] == []
        p = (
            await c.call_tool("propose_track", {"base_ref": "base", "head_ref": "HEAD", "limit": 1})
        ).data
        assert len(p["changed_specs"]) == 1 and p["next_cursor"] == 1
        assert p["counts"]["specs_touched"] == p["counts"]["specs_changed"] == 3


@pytest.mark.asyncio
async def test_propose_track_unknown_ref_is_shaped_error(track_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        r = (await c.call_tool("propose_track", {"base_ref": "bsae", "head_ref": "HEAD"})).data
    assert r["isError"] is True
    assert set(r) <= {"error", "isError", "did_you_mean", "hint"}
    assert "base" in r["did_you_mean"]


@pytest.mark.asyncio
async def test_record_track_writes_changespec_idempotently(track_repo):
    reasons = {"core-alpha": "alpha returns more", "core-beta": "beta grows new_fn"}
    async with Client(mcp) as c:
        await _prepare(c)
        r = (
            await c.call_tool(
                "record_track",
                {
                    "base_ref": "base",
                    "head_ref": "HEAD",
                    "title": "Gamma lands",
                    "why": "ship gamma",
                    "spec_reasons": reasons,
                },
            )
        ).data
        assert r["created"] is True and r["file"] == "CHANGESPEC.md"
        text = (track_repo / "CHANGESPEC.md").read_text()
        base7 = r["base_sha"][:7]
        head7 = r["head_sha"][:7]
        assert text.startswith("# CHANGESPEC\n")
        assert f"· Gamma lands ({base7}..{head7})" in text
        assert f"<!-- livespec:track id={r['track_id']} base={r['base_sha']} " in text
        assert "Why: ship gamma" in text
        assert "### core-alpha" in text
        assert "- Code: ~ change_me (pkg/core.py), - going_away (pkg/core.py)" in text
        assert "- Why: alpha returns more" in text
        assert '- Spec: MODIFIED "Beta"' in text
        assert '- Spec: ADDED "Gamma"' in text
        assert "~ moved_changed (pkg/new_name.py ← pkg/old_name.py)" in text
        assert "### (no spec)" in text
        assert "OpenSpec: add-gamma" in text

        # same range again: updated, not duplicated
        r2 = (
            await c.call_tool(
                "record_track",
                {
                    "base_ref": r["base_sha"],
                    "head_ref": r["head_sha"],
                    "title": "Gamma lands",
                    "why": "ship gamma, reworded",
                },
            )
        ).data
        assert r2["created"] is False and r2["updated"] is True
        text2 = (track_repo / "CHANGESPEC.md").read_text()
        assert text2.count("livespec:track id=") == 1
        assert "Why: ship gamma, reworded" in text2
        assert "- Why: alpha returns more" in text2  # not restated, so kept

        prop = (await c.call_tool("propose_track", {"base_ref": "base", "head_ref": "HEAD"})).data
        assert prop["already_recorded"] is True


@pytest.mark.asyncio
async def test_record_track_errors(track_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        bad_spec = (
            await c.call_tool(
                "record_track",
                {
                    "base_ref": "base",
                    "head_ref": "HEAD",
                    "title": "t",
                    "why": "w",
                    "spec_reasons": {"core-alpah": "typo"},
                },
            )
        ).data
        assert bad_spec["isError"] is True
        assert "core-alpha" in bad_spec["did_you_mean"]
        bad_ref = (
            await c.call_tool(
                "record_track",
                {"base_ref": "nope", "head_ref": "HEAD", "title": "t", "why": "w"},
            )
        ).data
        assert bad_ref["isError"] is True
        no_why = (
            await c.call_tool(
                "record_track", {"base_ref": "base", "head_ref": "HEAD", "title": "t", "why": " "}
            )
        ).data
        assert no_why["isError"] is True
    assert not (track_repo / "CHANGESPEC.md").exists()


@pytest.mark.asyncio
async def test_index_rebuilds_tracks_from_changespec(track_repo):
    """The file is the source of truth: drop the rows, re-index, same history."""
    from livespec_mcp.state import get_state

    async with Client(mcp) as c:
        await _prepare(c)
        await c.call_tool(
            "record_track",
            {"base_ref": "base", "head_ref": "HEAD", "title": "Gamma lands", "why": "ship"},
        )
        before = (await c.call_tool("get_spec_history", {"spec_id": "core-alpha"})).data
        st = get_state(str(track_repo))
        st.conn.execute("DELETE FROM track")
        assert st.conn.execute("SELECT COUNT(*) FROM track_symbol").fetchone()[0] == 0
        idx = (await c.call_tool("index_project", {})).data
        assert idx["tracks"]["present"] is True and idx["tracks"]["tracks"] == 1
        after = (await c.call_tool("get_spec_history", {"spec_id": "core-alpha"})).data
    assert after == before
    assert after["tracks"][0]["symbols"]["removed"][0]["qualified_name"] == "pkg.core.going_away"


@pytest.mark.asyncio
async def test_index_tolerates_missing_and_malformed_changespec(track_repo):
    async with Client(mcp) as c:
        idx = (await c.call_tool("index_project", {})).data
        assert idx["tracks"] == {"present": False, "tracks": 0}
        (track_repo / "CHANGESPEC.md").write_text(
            "# CHANGESPEC\n\n## not a track heading\nsome prose\n\n"
            "## 2026-01-01 · No marker (abcdef1..1234567)\nWhy: x\n"
        )
        idx = (await c.call_tool("index_project", {})).data
    assert idx["tracks"]["tracks"] == 0
    assert len(idx["tracks"]["problems"]) == 2
    assert "symbols_total" in idx  # the index run itself succeeded


@pytest.mark.asyncio
async def test_spec_and_symbol_history_paginate(track_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        first = _git(track_repo, "rev-list", "--max-parents=0", "HEAD")
        await c.call_tool(
            "record_track",
            {"base_ref": "base", "head_ref": "HEAD", "title": "One", "why": "first"},
        )
        # a second track on top: change new_fn again
        core = track_repo / "pkg" / "core.py"
        core.write_text(core.read_text().replace("return 3", "return 33"))
        _git(track_repo, "commit", "-q", "-am", "third")
        await c.call_tool("index_project", {})
        await c.call_tool(
            "record_track",
            {
                "base_ref": "HEAD~1",
                "head_ref": "HEAD",
                "title": "Two",
                "why": "second",
                "spec_reasons": {"core-beta": "tune new_fn"},
            },
        )
        h = (await c.call_tool("get_spec_history", {"spec_id": "core-beta"})).data
        assert h["count"] == 2
        assert [t["title"] for t in h["tracks"]] == ["Two", "One"]  # newest first
        assert h["tracks"][0]["spec_why"] == "tune new_fn"
        assert h["tracks"][1]["deltas"] == [{"operation": "MODIFIED", "requirement": "Beta"}]
        page = (await c.call_tool("get_spec_history", {"spec_id": "core-beta", "limit": 1})).data
        assert len(page["tracks"]) == 1 and page["next_cursor"] == 1 and page["count"] == 2
        summ = (
            await c.call_tool("get_spec_history", {"spec_id": "core-beta", "summary_only": True})
        ).data
        assert summ["tracks"][0] == {
            "track_id": h["tracks"][0]["track_id"],
            "date": h["tracks"][0]["date"],
            "tier": "changed",
        }

        sym = (await c.call_tool("get_symbol_history", {"symbol": "new_fn"})).data
        assert sym["count"] == 2 and sym["matched"] == ["pkg.core.new_fn"]
        assert sym["tracks"][0]["changes"][0]["change"] == "modified"
        assert sym["tracks"][1]["changes"][0]["change"] == "added"
        assert sym["tracks"][0]["specs"] == [{"spec_id": "core-beta", "why": "tune new_fn"}]
        same = (await c.call_tool("get_symbol_history", {"symbol": "pkg::core::new_fn"})).data
        assert same["count"] == 2
        gone = (await c.call_tool("get_symbol_history", {"symbol": "going_away"})).data
        assert gone["count"] == 1 and gone["tracks"][0]["changes"][0]["change"] == "removed"
        quiet = (await c.call_tool("get_symbol_history", {"symbol": "keep"})).data
        assert quiet["count"] == 0 and quiet["tracks"] == []
        missing = (await c.call_tool("get_symbol_history", {"symbol": "new_fm"})).data
        assert missing["isError"] is True and "pkg.core.new_fn" in missing["did_you_mean"]
        unknown = (await c.call_tool("get_spec_history", {"spec_id": "core-bta"})).data
        assert unknown["isError"] is True and "core-beta" in unknown["did_you_mean"]
    text = (track_repo / "CHANGESPEC.md").read_text()
    assert text.index("· Two (") < text.index("· One (")
    assert first  # the fixture really has a root commit


@pytest.mark.asyncio
async def test_spec_history_accepts_alias(track_repo):
    from livespec_mcp.domain.spec_alias import record_alias
    from livespec_mcp.state import get_state

    async with Client(mcp) as c:
        await _prepare(c)
        await c.call_tool(
            "record_track",
            {"base_ref": "base", "head_ref": "HEAD", "title": "One", "why": "first"},
        )
        st = get_state(str(track_repo))
        pk = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id='core-alpha'", (st.project_id,)
        ).fetchone()[0]
        record_alias(st.conn, st.project_id, "core-alfa", int(pk))
        h = (await c.call_tool("get_spec_history", {"spec_id": "core-alfa"})).data
    assert h["spec_id"] == "core-alpha" and h["count"] == 1
    assert h["deprecated_spec_ids"] == [{"id": "core-alfa", "use": "core-alpha"}]


@pytest.mark.asyncio
async def test_git_diff_impact_untouched_function_is_not_changed(track_repo):
    """git_diff_impact used to mark every symbol of a changed file as changed."""
    async with Client(mcp) as c:
        await _prepare(c)
        r = (await c.call_tool("git_diff_impact", {"base_ref": "base", "head_ref": "HEAD"})).data
    names = {s["qualified_name"] for s in r["changed_symbols"]}
    assert "pkg.core.change_me" in names and "pkg.core.new_fn" in names
    assert "pkg.core.keep" not in names
    assert "pkg.core.reformat_only" not in names
    assert "pkg.core.Box.close" not in names
    assert r["counts"]["changed_symbols"] == len(r["changed_symbols"])


def test_changespec_round_trip_parse_render():
    from livespec_mcp.domain.changespec import (
        CodeItem,
        SpecSection,
        TrackEntry,
        parse_changespec,
        render_entry,
        upsert_entry,
    )

    e = TrackEntry(
        track_id="a" * 7 + "-" + "b" * 7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        date="2026-10-18",
        title='Route groups (and "quotes")',
        why="line one\nline two",
        changes=["c1"],
        sections=[
            SpecSection(
                spec_id="cap-x",
                deltas=[
                    {"operation": "MODIFIED", "requirement": 'A "quoted", name'},
                    {"operation": "RENAMED", "requirement": "New", "rename_from": "Old"},
                ],
                code=[
                    CodeItem("added", "page", "app/(auth)/login/page.tsx"),
                    CodeItem("modified", "Cls.m", "b.py", "a.py"),
                    CodeItem("removed", "gone", "c.py"),
                ],
                why="because",
            ),
            SpecSection(spec_id=None, code=[CodeItem("added", "h", "u.py")]),
        ],
    )
    text, created = upsert_entry(None, e)
    assert created
    _, blocks = parse_changespec(text)
    assert len(blocks) == 1 and blocks[0].entry == e
    assert render_entry(blocks[0].entry) == render_entry(e)


@pytest.mark.asyncio
async def test_links_to_specs_born_after_head_are_not_credited(track_repo):
    """The link table is today's; a past range must not touch a Spec that did
    not exist at its head (OpenSpec repos say which existed, commit by commit)."""
    spec = track_repo / "openspec" / "specs" / "core" / "spec.md"
    spec.write_text(
        spec.read_text()
        + "\n### Requirement: Later\n\nThe system SHALL later.\n\n"
        + "#### Scenario: Later works\n\n- **WHEN** later\n- **THEN** it works\n"
    )
    core = track_repo / "pkg" / "core.py"
    core.write_text(
        core.read_text().replace(
            '"""@spec:core-alpha"""\n    return x + 2',
            '"""@spec:core-alpha, core-later"""\n    return x + 2',
        )
    )
    async with Client(mcp) as c:
        await _prepare(c)
        later = (await c.call_tool("get_spec_implementation", {"spec_id": "core-later"})).data
        assert later.get("isError") is not True  # linked in today's index
        d = (await c.call_tool("propose_track", {"base_ref": "base", "head_ref": "HEAD"})).data
    reached = d["changed_specs"] + d["touched_specs"]
    assert "core-later" not in {s["spec_id"] for s in reached}
    alpha = {s["spec_id"]: s for s in reached}["core-alpha"]
    assert "change_me" in _names(alpha["symbols"]["modified"])


@pytest.mark.asyncio
async def test_ui_spec_detail_shows_tracks_read_only(track_repo):
    from livespec_mcp.ui import data as ui_data

    async with Client(mcp) as c:
        await _prepare(c)
        await c.call_tool(
            "record_track",
            {
                "base_ref": "base",
                "head_ref": "HEAD",
                "title": "Gamma lands",
                "why": "ship",
                "spec_reasons": {"core-beta": "grow beta"},
            },
        )
    before = (track_repo / "CHANGESPEC.md").read_text()
    detail = ui_data.spec_detail(ui_data.stable_id(str(track_repo)), "core-beta")
    assert [t["title"] for t in detail["tracks"]] == ["Gamma lands"]
    t = detail["tracks"][0]
    assert t["tier"] == "changed"
    assert t["spec_why"] == "grow beta"
    assert {"change": "added", "name": "new_fn", "path": "pkg/core.py"} in t["code"]
    assert t["deltas"] == [{"operation": "MODIFIED", "requirement": "Beta"}]
    assert (track_repo / "CHANGESPEC.md").read_text() == before
