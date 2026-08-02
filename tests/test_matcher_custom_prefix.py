"""Custom spec-ID prefix support (`BE-RF-102`, `FE-RF-119`, `DEVMCP-RF-007`, ...).

``@spec:`` annotations resolve via store ``known_ids`` (OpenSpec slugs) and/or
PREFIX-NNN shape-match when the store still has rows of that shape — never
forced ``SPEC``.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.matcher import (
    _normalize_spec,
    derive_spec_prefixes,
    make_spec_token_re,
    parse_annotations,
)
from livespec_mcp.server import mcp


def test_custom_prefix_not_accepted_without_derivation():
    """Ground truth: `@spec:BE-RF-102` is invisible unless BE-RF is in prefixes."""
    assert parse_annotations("@spec:BE-RF-102") == []


def test_custom_prefix_accepted_when_derived():
    hits = parse_annotations("@spec:BE-RF-102", prefixes=("BE-RF",))
    assert len(hits) == 1
    assert hits[0].spec_id == "BE-RF-102"
    assert hits[0].confidence == 1.0


def test_no_false_positive_for_undeclared_prefix():
    """A BE-RF-shaped token must NOT link when BE-RF isn't a derived prefix."""
    assert parse_annotations("@spec:BE-RF-102", prefixes=()) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BE-RF-56", "BE-RF-056"),
        ("BE-RF-102", "BE-RF-102"),
        ("SPEC-1", "SPEC-001"),
        ("SPEC-901", "SPEC-901"),
    ],
)
def test_normalize_padding_table(raw, expected):
    token_re = make_spec_token_re(("SPEC", "BE-RF"))
    assert _normalize_spec(raw, token_re) == expected


def test_prose_lookalikes_do_not_match():
    """RFC-2119 / ISO-8601 in prose must produce zero hits even when RFC
    and ISO are (hypothetically) derived prefixes."""
    text = "Per RFC-2119 and ISO-8601, this endpoint returns 204."
    assert parse_annotations(text, prefixes=("RFC", "ISO")) == []


def test_level2_verb_anchored_custom_prefix():
    text = "This handler implements BE-RF-102 by validating the tenant."
    hits = parse_annotations(text, prefixes=("BE-RF",))
    assert len(hits) == 1
    assert hits[0].spec_id == "BE-RF-102"
    assert hits[0].confidence == 0.7
    assert hits[0].relation == "implements"


def test_slug_only_store_uses_known_ids():
    """Slug stores: no PREFIX-NNN match without known_ids."""
    assert parse_annotations("@spec:auth-user-login") == []
    hits = parse_annotations(
        "@spec:auth-user-login", known_ids=["auth-user-login"]
    )
    assert len(hits) == 1
    assert hits[0].spec_id == "auth-user-login"


def test_derive_spec_prefixes_from_store_ids_only():
    assert derive_spec_prefixes([]) == ()
    assert derive_spec_prefixes(["auth-user-login", "payments-charge"]) == ()
    assert derive_spec_prefixes(["BE-RF-102", "SPEC-001"]) == ("BE-RF", "SPEC")
    assert derive_spec_prefixes(["FE-RF-119", "DEVMCP-RF-007"]) == (
        "DEVMCP-RF",
        "FE-RF",
    )


@pytest.mark.asyncio
async def test_scan_spec_annotations_links_custom_prefix(workspace):
    """End-to-end: a spec registered as `BE-RF-102` links from
    `@spec:BE-RF-102` once it's derived from the store."""
    async with Client(mcp) as c:
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "__init__.py").write_text("")
        (workspace / "pkg" / "code.py").write_text(
            'def suspend_tenant():\n    """@spec:BE-RF-102"""\n    return 1\n'
        )
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"title": "Suspend tenant", "spec_id": "BE-RF-102"}
        )
        await c.call_tool("index_project", {"force": True})
        out = (await c.call_tool("list_specs", {})).data
        specs_by_id = {s["spec_id"]: s for s in out["specs"]}
        assert specs_by_id["BE-RF-102"]["link_count"] == 1


@pytest.mark.asyncio
async def test_scan_spec_annotations_no_false_positive_when_prefix_absent(workspace):
    """A BE-RF-shaped annotation must NOT link when the store has no BE-RF spec."""
    async with Client(mcp) as c:
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "__init__.py").write_text("")
        (workspace / "pkg" / "code.py").write_text(
            'def suspend_tenant():\n    """@spec:BE-RF-102"""\n    return 1\n'
        )
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_spec_annotations", {})).data
        assert out["links_created"] == 0


@pytest.mark.asyncio
async def test_scan_reports_ids_no_spec_answers_to(workspace):
    """A renamed OpenSpec requirement leaves the code pointing at a dead slug."""
    async with Client(mcp) as c:
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "__init__.py").write_text("")
        (workspace / "pkg" / "code.py").write_text(
            'def login():\n    """@spec:auth-user-login"""\n    return 1\n'
            '\n'
            'def signout():\n    """@spec:auth-user-signout"""\n    return 2\n'
            '\n'
            'def helper():\n    """@see the README for details"""\n    return 3\n'
        )
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"title": "User login", "spec_id": "auth-user-login"}
        )

        out = (await c.call_tool("scan_spec_annotations", {})).data
        assert out["links_created"] == 1
        assert out["unknown_annotation_ids"] == ["auth-user-signout"], out
        assert out["unknown_annotation_sample"][0]["qualified_name"].endswith("signout")
        assert out["hint"]


@pytest.mark.asyncio
async def test_scan_stays_quiet_when_every_annotation_resolves(workspace):
    """No `unknown_*` keys on a clean repo — the field is a signal, not noise."""
    async with Client(mcp) as c:
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "__init__.py").write_text("")
        (workspace / "pkg" / "code.py").write_text(
            'def login():\n    """@spec:auth-user-login"""\n    return 1\n'
        )
        await c.call_tool("index_project", {})
        await c.call_tool(
            "create_spec", {"title": "User login", "spec_id": "auth-user-login"}
        )
        out = (await c.call_tool("scan_spec_annotations", {})).data
        assert out["links_created"] == 1
        assert "unknown_annotation_ids" not in out, out
