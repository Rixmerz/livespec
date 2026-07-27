"""Custom spec-ID prefix support (`BE-RF-102`, `FE-RF-119`, `DEVMCP-RF-007`, ...).

`@spec:` annotations were hardcoded to only accept `SPEC-NNN`-shaped tokens.
This derives the accepted prefix set from spec IDs actually present in the
store, so a project using its own ID scheme can use `@spec:` annotations
too — with zero config and zero false positives for prefixes the store
doesn't have.
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
    """Ground truth (unfixed default): `@spec:BE-RF-102` is invisible unless
    BE-RF is explicitly in the accepted prefix set."""
    assert parse_annotations("@spec:BE-RF-102") == []


def test_custom_prefix_accepted_when_derived():
    hits = parse_annotations("@spec:BE-RF-102", prefixes=("SPEC", "BE-RF"))
    assert len(hits) == 1
    assert hits[0].spec_id == "BE-RF-102"
    assert hits[0].confidence == 1.0


def test_no_false_positive_for_undeclared_prefix():
    """A BE-RF-shaped token must NOT link when BE-RF isn't a derived prefix
    (only SPEC is), same as the pre-fix ground truth above."""
    assert parse_annotations("@spec:BE-RF-102", prefixes=("SPEC",)) == []


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
    and ISO are (hypothetically) derived prefixes — the verb+token shape
    still gates it, and here there's no @spec:/implements verb at all."""
    text = "Per RFC-2119 and ISO-8601, this endpoint returns 204."
    assert parse_annotations(text, prefixes=("SPEC", "RFC", "ISO")) == []


def test_level2_verb_anchored_custom_prefix():
    text = "This handler implements BE-RF-102 by validating the tenant."
    hits = parse_annotations(text, prefixes=("SPEC", "BE-RF"))
    assert len(hits) == 1
    assert hits[0].spec_id == "BE-RF-102"
    assert hits[0].confidence == 0.7
    assert hits[0].relation == "implements"


def test_existing_spec_behavior_byte_identical():
    """Control: default (no prefixes arg) behaves exactly as before for
    plain SPEC-NNN annotations."""
    hits = parse_annotations("@spec:SPEC-241")
    assert len(hits) == 1
    assert hits[0].spec_id == "SPEC-241"
    assert hits[0].confidence == 1.0


def test_derive_spec_prefixes_always_includes_spec():
    assert derive_spec_prefixes([]) == ("SPEC",)
    assert derive_spec_prefixes(["BE-RF-102", "SPEC-001"]) == ("BE-RF", "SPEC")
    assert derive_spec_prefixes(["FE-RF-119", "DEVMCP-RF-007"]) == (
        "DEVMCP-RF",
        "FE-RF",
        "SPEC",
    )


@pytest.mark.asyncio
async def test_scan_spec_annotations_links_custom_prefix(workspace):
    """End-to-end: a spec registered as `BE-RF-102` links from
    `@spec:BE-RF-102` once it's derived from the store. `index_project`
    already runs the scanner at its tail (idempotent INSERT OR IGNORE), so
    assert the resulting link count via `list_specs` rather than assuming
    `scan_spec_annotations` itself reports the creation."""
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
    """A BE-RF-shaped annotation must NOT link when the store has no BE-RF
    spec at all (only SPEC, implicitly) — zero false positives."""
    async with Client(mcp) as c:
        (workspace / "pkg").mkdir()
        (workspace / "pkg" / "__init__.py").write_text("")
        (workspace / "pkg" / "code.py").write_text(
            'def suspend_tenant():\n    """@spec:BE-RF-102"""\n    return 1\n'
        )
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_spec_annotations", {})).data
        assert out["links_created"] == 0
