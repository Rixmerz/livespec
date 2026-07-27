"""Audit bug #10: annotations using an unrecognized verb (`@rf:BE-RF-080`)
are invisible to `parse_annotations` / `scan_spec_annotations` — no error,
no link, no count. `scan_annotation_verbs` is the check that surfaces them.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.domain.matcher import parse_annotations
from livespec_mcp.server import mcp


def test_rf_verb_is_invisible_to_the_real_matcher():
    """Ground truth: confirms the bug this tool exists to catch actually
    exists in the matcher — @rf: produces zero hits, @spec: produces one."""
    assert parse_annotations("@rf:BE-RF-080") == []
    hits = parse_annotations("@spec:SPEC-080")
    assert len(hits) == 1
    assert hits[0].spec_id == "SPEC-080"


@pytest.mark.asyncio
async def test_scan_annotation_verbs_flags_unrecognized_verb(workspace):
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text(
        'def suspend_tenant():\n    """@rf:BE-RF-080"""\n    return 1\n'
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_annotation_verbs", {})).data
        assert out["total_findings"] == 1
        group = out["verb_groups"][0]
        assert group["verb"] == "@rf"
        assert group["count"] == 1
        assert group["reason"] == "unrecognized_verb"
        assert group["did_you_mean"].startswith("@")
        sample = group["sample"][0]
        assert sample["file_path"] == "pkg/code.py"
        assert sample["qualified_name"] == "pkg.code.suspend_tenant"
        assert "BE-RF-080" in sample["token_candidate"]


@pytest.mark.asyncio
async def test_scan_annotation_verbs_flags_token_shape_mismatch(workspace):
    """Verb IS recognized (@spec) but the payload isn't SPEC-NNN shaped —
    a second, independent way an annotation goes unconsumed."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text(
        '"""\n@spec:BE-RF-080\n"""\n'
        "def handler():\n    return 1\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_annotation_verbs", {})).data
        assert out["total_findings"] == 1
        group = out["verb_groups"][0]
        assert group["verb"] == "@spec"
        assert group["reason"] == "token_shape"
        assert group["did_you_mean"] is None


@pytest.mark.asyncio
async def test_scan_annotation_verbs_skips_consumable_annotations(workspace):
    """A correct @spec:SPEC-NNN annotation must NOT be flagged — it's
    consumed by the real matcher, so this tool has nothing to report."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text(
        '"""\n@spec:SPEC-001\n"""\n'
        "def handler():\n    return 1\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_annotation_verbs", {})).data
        assert out["total_findings"] == 0
        assert out["verb_groups"] == []


@pytest.mark.asyncio
async def test_scan_annotation_verbs_finds_annotations_the_extractor_drops(workspace):
    """`@rf:` above a bare Hono route-registration expression must still be
    found even though NO function/handler symbol exists on that line — this
    tool reads FILES, not just extracted symbol docstrings, so a gap in
    what tree-sitter extracts as a linkable node can't hide a finding.
    (`get_spec_implementation`-style docstring scans would see nothing here
    since there's no symbol whose docstring this comment could attach to.)
    """
    (workspace / "src").mkdir()
    (workspace / "src" / "routes.ts").write_text(
        "import { Hono } from 'hono';\n"
        "const app = new Hono();\n"
        "/** @rf:BE-RF-200 */\n"
        "app.get('/x', (c) => c.json({}));\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_annotation_verbs", {})).data
        assert out["total_findings"] == 1
        assert out["verb_groups"][0]["sample"][0]["file_path"] == "src/routes.ts"
        assert "BE-RF-200" in out["verb_groups"][0]["sample"][0]["token_candidate"]


@pytest.mark.asyncio
async def test_scan_annotation_verbs_groups_bound_sample(workspace):
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    body = ['"""']
    for i in range(15):
        body.append(f"@rf:BE-RF-{i:03d}")
    body.append('"""')
    body.append("def many():\n    return 1\n")
    (workspace / "pkg" / "code.py").write_text("\n".join(body))
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("scan_annotation_verbs", {"sample_per_group": 5})).data
        group = out["verb_groups"][0]
        assert group["count"] == 15
        assert len(group["sample"]) == 5
