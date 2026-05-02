"""v0.6 P4: every tool error returns {error, isError, did_you_mean?, hint?}.

The helper is `livespec_mcp.tools._errors.mcp_error`. Tests crawl the tools
and trigger known error paths, asserting the shape is consistent — no
ad-hoc {error: ..., warning: ..., extra_field: ...} variations.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.tools._errors import mcp_error


def test_helper_shape():
    """Direct unit: mcp_error always emits the canonical shape."""
    e = mcp_error("oops")
    assert e == {"error": "oops", "isError": True}

    e = mcp_error("oops", did_you_mean=[{"qualified_name": "x"}])
    assert e == {
        "error": "oops",
        "isError": True,
        "did_you_mean": [{"qualified_name": "x"}],
    }

    e = mcp_error("oops", hint="run `index_project`")
    assert e == {"error": "oops", "isError": True, "hint": "run `index_project`"}

    e = mcp_error("oops", did_you_mean=[], hint="hi")
    assert e["error"] == "oops"
    assert e["isError"] is True
    assert e["did_you_mean"] == []
    assert e["hint"] == "hi"


def _assert_canonical_error(payload: dict, must_have_hint: bool = False) -> None:
    """The error shape contract."""
    assert payload.get("isError") is True, f"missing isError=True: {payload}"
    assert isinstance(payload.get("error"), str) and payload["error"], (
        f"error must be a non-empty string: {payload}"
    )
    # error must be single-line, not a stderr dump
    assert "\n" not in payload["error"], f"error must be single line: {payload}"
    # only the canonical keys allowed
    allowed = {"error", "isError", "did_you_mean", "hint"}
    extras = set(payload.keys()) - allowed
    assert not extras, f"unexpected keys in error payload: {extras}"
    if must_have_hint:
        assert isinstance(payload.get("hint"), str), f"hint missing: {payload}"


@pytest.mark.asyncio
async def test_unknown_rf_error_shape(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "get_requirement_implementation", {"rf_id": "RF-DOES-NOT-EXIST"}
            )
        ).data
        _assert_canonical_error(out, must_have_hint=True)


@pytest.mark.asyncio
async def test_unknown_symbol_error_shape(sample_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("quick_orient", {"qname": "zzzz"})).data
        _assert_canonical_error(out)
        # Should also have did_you_mean for typo recovery
        assert "did_you_mean" in out
        assert isinstance(out["did_you_mean"], list)


@pytest.mark.asyncio
async def test_self_link_rf_dependency_error_shape(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_requirement", {"rf_id": "RF-A", "title": "A"})
        out = (
            await c.call_tool(
                "link_rf_dependency",
                {"parent_rf_id": "RF-A", "child_rf_id": "RF-A"},
            )
        ).data
        _assert_canonical_error(out)


@pytest.mark.asyncio
async def test_cycle_error_shape_includes_hint(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        for r in ("RF-1", "RF-2"):
            await c.call_tool("create_requirement", {"rf_id": r, "title": r})
        await c.call_tool(
            "link_rf_dependency", {"parent_rf_id": "RF-1", "child_rf_id": "RF-2"}
        )
        out = (
            await c.call_tool(
                "link_rf_dependency",
                {"parent_rf_id": "RF-2", "child_rf_id": "RF-1"},
            )
        ).data
        _assert_canonical_error(out, must_have_hint=True)


@pytest.mark.asyncio
async def test_git_diff_not_a_repo_error_shape(sample_repo):
    """Already covered by test_git_diff but verifies P4 contract too."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("git_diff_impact", {})).data
        _assert_canonical_error(out)
