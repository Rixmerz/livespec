"""SchemaCompat: Cursor-friendly tools/list parameter types."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from livespec_mcp.schema_compat import flatten_tool_parameters
from livespec_mcp.server import mcp


def test_flatten_unwraps_nested_workspace_anyof() -> None:
    raw = {
        "type": "object",
        "properties": {
            "qname": {"type": "string"},
            "workspace": {
                "anyOf": [
                    {
                        "anyOf": [
                            {"minLength": 1, "type": "string"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "description": "REQUIRED on every call.",
                        "examples": ["/tmp/repo"],
                    },
                    {"type": "null"},
                ],
                "default": None,
            },
        },
        "required": ["qname"],
    }
    flat = flatten_tool_parameters(raw)
    ws = flat["properties"]["workspace"]
    assert ws.get("type") == "string"
    assert "anyOf" not in ws
    assert ws.get("description", "").startswith("REQUIRED")
    assert "default" not in ws
    assert "workspace" in flat["required"]


@pytest.mark.asyncio
async def test_who_calls_list_schema_has_typed_params_with_descriptions() -> None:
    """tools/list after SchemaCompatMiddleware: no anyOf on workspace; descriptions present."""

    async with Client(mcp) as client:
        tools = await client.list_tools()
    by_name = {t.name: t for t in tools}
    assert "who_calls" in by_name
    schema = by_name["who_calls"].inputSchema
    props = schema["properties"]
    ws = props["workspace"]
    assert ws.get("type") == "string", ws
    assert "anyOf" not in ws
    assert "description" in ws and "REQUIRED" in ws["description"]
    assert "workspace" in schema.get("required", [])
    for name in ("qname", "max_depth", "limit", "cursor", "summary_only", "min_weight"):
        assert name in props
        assert "description" in props[name], name
        assert props[name].get("type") in {"string", "integer", "boolean", "number"}, props[name]
