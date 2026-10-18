"""Tracks split their Specs into ``changed`` and ``touched``.

The fixture reproduces a range measured on a real repository: 7 OpenSpec
requirement deltas, yet 37 Specs reached, because changed functions are
annotated with or linked to many Specs. Only the 7 delta Specs and the one Spec
a changed function names first in its own ``@spec:`` are ``changed``; the 29
others are ``touched``, and CHANGESPEC.md lists them on one marked line.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

N_SPECS = 37
DELTAS = [f"shop-r{i:02d}" for i in range(1, 8)]  # 7 requirement deltas
PRIMARY = "shop-r08"  # dispatch's first @spec:
CHANGED = [*DELTAS, PRIMARY]
ALL = [f"shop-r{i:02d}" for i in range(1, N_SPECS + 1)]
TOUCHED = [s for s in ALL if s not in CHANGED]


def _requirement(i: int, text: str) -> str:
    return (
        f"### Requirement: R{i:02d}\n\nThe system SHALL {text} {i}.\n\n"
        f"#### Scenario: R{i:02d} works\n\n- **WHEN** r{i}\n- **THEN** it works\n\n"
    )


SPEC_MD = "# Shop Specification\n\n## Purpose\n\nShop.\n\n## Requirements\n\n" + "".join(
    _requirement(i, "do") for i in range(1, N_SPECS + 1)
)
DELTA_MD = "## MODIFIED Requirements\n\n" + "".join(
    _requirement(i, "do better") for i in range(1, 8)
)

DISPATCH_IDS = ", ".join(f"shop-r{i:02d}" for i in range(8, 27))  # r08..r26


def _shop(version: int) -> str:
    legacy = (
        ""
        if version == 2
        else '''

def legacy():
    """@spec:shop-r01, shop-r29"""
    return 0
'''
    )
    return f'''def dispatch():
    """@spec:{DISPATCH_IDS}"""
    return {version}


def audit():
    """Audit log; implements shop-r27."""
    return {version}


def report():
    """@see:shop-r28"""
    return {version}


def linked():
    return {version}
{legacy}'''


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def shop_repo(workspace: Path) -> Path:
    ws = workspace
    (ws / "pkg").mkdir()
    (ws / "pkg" / "__init__.py").write_text("")
    (ws / "pkg" / "shop.py").write_text(_shop(1))
    (ws / "openspec" / "specs" / "shop").mkdir(parents=True)
    (ws / "openspec" / "specs" / "shop" / "spec.md").write_text(SPEC_MD)
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "base")
    _git(ws, "tag", "base")
    (ws / "pkg" / "shop.py").write_text(_shop(2))
    delta = ws / "openspec" / "changes" / "tune-shop" / "specs" / "shop"
    delta.mkdir(parents=True)
    (delta / "spec.md").write_text(DELTA_MD)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "head")
    return ws


async def _prepare(c: Client) -> None:
    await c.call_tool("index_project", {})
    await c.call_tool("sync_openspec", {})
    await c.call_tool("index_project", {"force": True})
    # `linked` has no annotation: the link table alone ties it to 8 Specs.
    r = (
        await c.call_tool(
            "bulk_link_spec_symbols",
            {
                "mappings": [
                    {"spec_id": f"shop-r{i:02d}", "symbol_qname": "pkg.shop.linked"}
                    for i in range(30, 38)
                ]
            },
        )
    ).data
    assert r["linked"] == 8, r


def _record(**extra) -> dict:
    return {"base_ref": "base", "head_ref": "HEAD", "title": "Tune shop", "why": "ship", **extra}


def _entry(ws: Path) -> str:
    return (ws / "CHANGESPEC.md").read_text()


@pytest.mark.asyncio
async def test_37_specs_7_deltas_only_delta_and_primary_specs_are_changed(shop_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        d = (await c.call_tool("propose_track", {"base_ref": "base", "head_ref": "HEAD"})).data
    assert d["counts"]["specs_touched"] == N_SPECS  # the total, as before tiers
    assert d["counts"]["spec_deltas"] == 7
    assert [s["spec_id"] for s in d["changed_specs"]] == CHANGED
    assert [s["spec_id"] for s in d["touched_specs"]] == TOUCHED
    assert (d["counts"]["specs_changed"], d["counts"]["specs_touched_only"]) == (8, 29)
    assert {s["tier"] for s in d["changed_specs"]} == {"changed"}
    assert {s["tier"] for s in d["touched_specs"]} == {"touched"}
    by = {s["spec_id"]: s for s in d["changed_specs"] + d["touched_specs"]}
    # dispatch reaches r08..r26 but names r08 first
    assert by[PRIMARY]["symbols"]["modified"][0]["name"] == "dispatch"
    assert by["shop-r09"]["symbols"]["modified"][0]["name"] == "dispatch"
    # removed legacy: base-side `@spec:shop-r01, shop-r29` — r01 changed, r29 not
    assert by["shop-r01"]["symbols"]["removed"][0]["name"] == "legacy"
    assert by["shop-r29"]["tier"] == "touched"
    # verb-anchored mention, @see: line, link table: never changed
    assert {by[s]["tier"] for s in ("shop-r27", "shop-r28", "shop-r30", "shop-r37")} == {"touched"}


@pytest.mark.asyncio
async def test_changespec_lists_touched_specs_on_one_marked_line(shop_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        r = (await c.call_tool("record_track", _record())).data
        assert r["changed_specs"] == CHANGED and r["touched_specs"] == TOUCHED
        assert r["next_cursor"] is None
        text = _entry(shop_repo)
        for sid in CHANGED:
            assert f"\n### {sid}\n" in text
        for sid in TOUCHED:
            assert f"### {sid}" not in text
        assert f"Also touched: {', '.join(TOUCHED)} <!-- livespec:touched -->\n" in text
        assert (
            "(touched only)" not in text and "~ audit (" not in text
        )  # code of touched Specs only
        assert "### shop-r08\n- Code: ~ dispatch (pkg/shop.py)\n" in text
        assert '### shop-r01\n- Spec: MODIFIED "R01"\n- Code: - legacy (pkg/shop.py)\n' in text

        touched = (await c.call_tool("get_spec_history", {"spec_id": "shop-r09"})).data
        assert touched["count"] == 1
        assert touched["tracks"][0]["tier"] == "touched"
        assert touched["tracks"][0]["symbols"] == {"added": [], "modified": [], "removed": []}
        changed = (await c.call_tool("get_spec_history", {"spec_id": PRIMARY})).data
        assert changed["tracks"][0]["tier"] == "changed"
        assert changed["tracks"][0]["symbols"]["modified"][0]["name"] == "dispatch"

    from livespec_mcp.state import get_state

    st = get_state(str(shop_repo))
    tiers = dict(st.conn.execute("SELECT tier, COUNT(*) FROM track_spec GROUP BY tier").fetchall())
    assert tiers == {"changed": 8, "touched": 29}


@pytest.mark.asyncio
async def test_rerecord_is_idempotent_and_keeps_a_touched_specs_reason(shop_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        first = (await c.call_tool("record_track", _record())).data
        once = _entry(shop_repo)
        again = (await c.call_tool("record_track", _record())).data
        assert again["created"] is False and _entry(shop_repo) == once
        assert again["entry_markdown"] == first["entry_markdown"]

        # A reason for a touched Spec gives it a (marked) section...
        await c.call_tool("record_track", _record(spec_reasons={"shop-r30": "linked moved"}))
        text = _entry(shop_repo)
        assert (
            "### shop-r30 <!-- livespec:touched -->\n- Code: ~ linked (pkg/shop.py)\n"
            "- Why: linked moved\n" in text
        )
        assert "shop-r30," not in text.split("<!-- livespec:touched -->")[0]
        # ...that a later call which does not restate it keeps.
        await c.call_tool("record_track", _record(why="ship, reworded"))
        text = _entry(shop_repo)
        assert text.count("livespec:track id=") == 1
        assert "Why: ship, reworded" in text
        assert "- Why: linked moved" in text
        h = (await c.call_tool("get_spec_history", {"spec_id": "shop-r30"})).data
        assert h["tracks"][0]["tier"] == "touched"
        assert h["tracks"][0]["spec_why"] == "linked moved"

        # The file is the truth: drop the rows, re-index, same history.
        from livespec_mcp.state import get_state

        before = (await c.call_tool("get_spec_history", {"spec_id": "shop-r09"})).data
        get_state(str(shop_repo)).conn.execute("DELETE FROM track")
        await c.call_tool("index_project", {})
        after = (await c.call_tool("get_spec_history", {"spec_id": "shop-r09"})).data
        assert after == before and after["tracks"][0]["tier"] == "touched"


@pytest.mark.asyncio
async def test_include_touched_sections_writes_a_section_per_touched_spec(shop_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        await c.call_tool("record_track", _record(include_touched_sections=True))
        text = _entry(shop_repo)
        assert "Also touched:" not in text
        for sid in TOUCHED:
            assert f"\n### {sid} <!-- livespec:touched -->\n" in text
        assert "### shop-r09 <!-- livespec:touched -->\n- Code: ~ dispatch (pkg/shop.py)\n" in text
        h = (await c.call_tool("get_spec_history", {"spec_id": "shop-r09"})).data
    assert h["tracks"][0]["tier"] == "touched"
    assert h["tracks"][0]["symbols"]["modified"][0]["name"] == "dispatch"

    from livespec_mcp.ui import data as ui_data

    detail = ui_data.spec_detail(ui_data.stable_id(str(shop_repo)), "shop-r09")
    assert [(t["title"], t["tier"]) for t in detail["tracks"]] == [("Tune shop", "touched")]


@pytest.mark.asyncio
async def test_tiers_paginate_with_exact_counts(shop_repo):
    async with Client(mcp) as c:
        await _prepare(c)
        p = (
            await c.call_tool("propose_track", {"base_ref": "base", "head_ref": "HEAD", "limit": 5})
        ).data
        assert len(p["changed_specs"]) == 5 and len(p["touched_specs"]) == 5
        assert p["next_cursor"] == 5 and p["counts"]["specs_touched_only"] == 29
        last = (
            await c.call_tool(
                "propose_track", {"base_ref": "base", "head_ref": "HEAD", "limit": 5, "cursor": 25}
            )
        ).data
        assert last["changed_specs"] == [] and len(last["touched_specs"]) == 4
        assert last["next_cursor"] is None
        s = (
            await c.call_tool(
                "propose_track", {"base_ref": "base", "head_ref": "HEAD", "summary_only": True}
            )
        ).data
        assert s["changed_spec_ids"] == CHANGED and s["touched_spec_ids"] == TOUCHED

        r = (await c.call_tool("record_track", _record(limit=5, cursor=5))).data
        assert r["changed_specs"] == CHANGED[5:] and r["touched_specs"] == TOUCHED[5:10]
        assert r["next_cursor"] == 10 and r["counts"]["specs_touched_only"] == 29
        forged = (
            await c.call_tool(
                "record_track",
                _record(why="ship\nAlso touched: shop-r01 <!-- livespec:touched -->"),
            )
        ).data
        assert forged["isError"] is True and set(forged) <= {"error", "isError", "hint"}
        quiet = (await c.call_tool("record_track", _record(summary_only=True))).data
        assert "touched_specs" not in quiet and "entry_markdown" not in quiet
        assert quiet["counts"]["specs_changed"] == 8


# --------------------------------------------------------------- the format

# An entry exactly as the format before tiers wrote it, with a Why whose
# continuation line starts with "Also touched:" (the Why is written unindented).
OLD_BLOCK = (
    "## 2026-10-01 · Old entry (aaaaaaa..bbbbbbb)\n"
    f"<!-- livespec:track id=aaaaaaa-bbbbbbb base={'a' * 40} head={'b' * 40} -->\n"
    "Why: first line\n"
    "Also touched: billing, the cache and the docs\n"
    "OpenSpec: c1\n"
    "\n"
    "### core-alpha\n"
    '- Spec: MODIFIED "Alpha"\n'
    "- Code: ~ change_me (pkg/core.py)\n"
    "- Why: alpha moved\n"
    "\n"
    "### core-beta\n"
    "- Code: + new_fn (pkg/core.py)\n"
    "\n"
    "### (no spec)\n"
    "- Code: + helper (pkg/util.py)\n"
)
OLD_FILE = "# CHANGESPEC\n\nPreamble.\n\n" + OLD_BLOCK


def _ingest(text: str, tmp_path: Path) -> sqlite3.Connection:
    from livespec_mcp.domain.changespec import ingest_changespec
    from livespec_mcp.storage.db import connect

    (tmp_path / "CHANGESPEC.md").write_text(text)
    conn = connect(tmp_path / "db.sqlite")
    conn.execute("INSERT INTO project(id, name, root) VALUES(1, 'p', ?)", (str(tmp_path),))
    out = ingest_changespec(conn, 1, tmp_path)
    assert "problems" not in out, out
    return conn


def test_old_format_entry_reads_back_byte_for_byte(tmp_path):
    from livespec_mcp.domain.changespec import parse_changespec, render_entry

    _, blocks = parse_changespec(OLD_FILE)
    e = blocks[0].entry
    assert e.why == "first line\nAlso touched: billing, the cache and the docs"
    assert e.touched == []
    assert [(s.spec_id, s.tier) for s in e.sections] == [
        ("core-alpha", "changed"),
        ("core-beta", "changed"),
        (None, "changed"),
    ]
    assert render_entry(e) == OLD_BLOCK

    conn = _ingest(OLD_FILE, tmp_path)
    rows = conn.execute("SELECT spec_id, tier FROM track_spec ORDER BY spec_id").fetchall()
    assert [tuple(r) for r in rows] == [("core-alpha", "changed"), ("core-beta", "changed")]
    assert conn.execute("SELECT why FROM track").fetchone()[0] == e.why


def test_why_with_an_also_touched_line_survives_the_new_format(tmp_path):
    from livespec_mcp.domain.changespec import (
        CodeItem,
        SpecSection,
        TrackEntry,
        parse_changespec,
        render_entry,
        upsert_entry,
    )

    e = TrackEntry(
        track_id="c" * 7 + "-" + "d" * 7,
        base_sha="c" * 40,
        head_sha="d" * 40,
        date="2026-10-18",
        title="New entry",
        why="first line\nAlso touched: billing, the cache\nlast line",
        sections=[
            SpecSection(spec_id="core-alpha", code=[CodeItem("modified", "f", "a.py")]),
            SpecSection(
                spec_id="core-gamma", code=[CodeItem("modified", "g", "a.py")], tier="touched"
            ),
        ],
        touched=["core-beta"],
    )
    # A newer entry above the old one: the old block stays byte-for-byte.
    text, created = upsert_entry(OLD_FILE, e)
    assert created and OLD_BLOCK in text
    _, blocks = parse_changespec(text)
    assert [b.entry for b in blocks] == [e, parse_changespec(OLD_FILE)[1][0].entry]
    assert render_entry(blocks[0].entry) == render_entry(e)
    assert "### core-gamma <!-- livespec:touched -->" in text

    conn = _ingest(text, tmp_path)
    rows = conn.execute(
        """SELECT t.title, ts.spec_id, ts.tier FROM track_spec ts JOIN track t ON t.id=ts.track_id
           ORDER BY t.ordinal, ts.spec_id"""
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("New entry", "core-alpha", "changed"),
        ("New entry", "core-beta", "touched"),
        ("New entry", "core-gamma", "touched"),
        ("Old entry", "core-alpha", "changed"),
        ("Old entry", "core-beta", "changed"),
    ]
    whys = [r[0] for r in conn.execute("SELECT why FROM track ORDER BY ordinal")]
    assert whys == [e.why, "first line\nAlso touched: billing, the cache and the docs"]


def test_primary_spec_is_the_first_explicit_implements_annotation():
    from livespec_mcp.domain.matcher import parse_annotations
    from livespec_mcp.domain.tracks import primary_spec

    known = {"a", "b", "c"}

    def primary(doc: str) -> str | None:
        return primary_spec(parse_annotations(doc, (), known_ids=known))

    assert primary("@spec:a, b") == "a"
    assert primary("@spec:b\n@spec:a") == "b"
    assert primary("@implements:c") == "c"
    assert primary("@spec:a:0.7") == "a"  # an override keeps it explicit
    assert primary("This implements a.") is None  # verb-anchored
    assert primary("@tests:a\n@see:b") is None
    assert primary("@spec:a, b\n@not_spec:a") == "b"
    assert primary("") is None
