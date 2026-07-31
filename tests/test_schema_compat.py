"""SchemaCompat: Cursor-friendly tools/list parameter types + descriptions."""

from __future__ import annotations

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
    flat = flatten_tool_parameters(raw, tool_name="who_calls")
    ws = flat["properties"]["workspace"]
    assert ws.get("type") == "string"
    assert "anyOf" not in ws
    assert ws.get("description", "").startswith("REQUIRED")
    assert "default" not in ws
    assert "workspace" in flat["required"]


def test_flatten_fills_missing_param_descriptions() -> None:
    raw = {
        "type": "object",
        "properties": {
            "force": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 200},
        },
        "required": [],
    }
    flat = flatten_tool_parameters(raw, tool_name="index_project")
    assert "re-extract" in flat["properties"]["force"]["description"].lower()
    assert "page" in flat["properties"]["limit"]["description"].lower()


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


@pytest.mark.asyncio
async def test_all_listed_tool_params_have_descriptions(monkeypatch) -> None:
    """Every property on every visible tool carries a non-empty description."""
    monkeypatch.setenv("LIVESPEC_PLUGINS", "all")
    async with Client(mcp) as client:
        tools = await client.list_tools()
    missing: list[str] = []
    for tool in tools:
        props = (tool.inputSchema or {}).get("properties") or {}
        for name, prop in props.items():
            desc = prop.get("description") if isinstance(prop, dict) else None
            if not (isinstance(desc, str) and desc.strip()):
                missing.append(f"{tool.name}.{name}")
    assert missing == [], f"params without description: {missing}"
