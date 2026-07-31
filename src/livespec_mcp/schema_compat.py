"""Flatten nullable JSON-Schema unions so MCP hosts (esp. Cursor) map types.

Cursor's tool UI often shows ``Type: any`` / empty Description when a property
uses nested ``anyOf`` (``str | None`` wrapped again as ``Workspace | None``) or
even a single ``anyOf[T, null]``. Runtime still accepts omitted ``workspace``
(tests + middleware); the *advertised* schema is a plain ``string`` with
description so hosts and agents fill it correctly.
"""

from __future__ import annotations

import copy
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import Tool


def _unwrap_null_union(prop: dict[str, Any]) -> dict[str, Any]:
    """Collapse ``anyOf[T, null]`` (possibly nested) into a plain ``T`` schema."""
    current = dict(prop)
    for _ in range(4):  # nested anyOf safety bound
        any_of = current.get("anyOf")
        if not isinstance(any_of, list) or len(any_of) != 2:
            break
        non_null = [
            x for x in any_of if not (isinstance(x, dict) and x.get("type") == "null")
        ]
        nulls = [x for x in any_of if isinstance(x, dict) and x.get("type") == "null"]
        if len(non_null) != 1 or len(nulls) != 1:
            break
        inner = dict(non_null[0])
        for key in ("description", "examples", "title", "default"):
            if key in current and key not in inner:
                inner[key] = current[key]
        # Prefer outer description when the branch is a bare type.
        if "description" in current:
            inner["description"] = current["description"]
        current = inner
    # Hosts choke on ``type: string`` + ``default: null``.
    if current.get("type") == "string" and current.get("default", object()) is None:
        current.pop("default", None)
    return current


def flatten_tool_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an inputSchema with nullable unions flattened."""
    out = copy.deepcopy(schema)
    props = out.get("properties")
    if not isinstance(props, dict):
        return out
    required = list(out.get("required") or [])
    for name, prop in list(props.items()):
        if not isinstance(prop, dict):
            continue
        flat = _unwrap_null_union(prop)
        props[name] = flat
        if name == "workspace" and "workspace" not in required:
            required.append("workspace")
    out["properties"] = props
    out["required"] = required
    return out


class SchemaCompatMiddleware(Middleware):
    """Rewrite ``tools/list`` parameter schemas for Cursor-friendly types."""

    async def on_list_tools(self, context, call_next):  # type: ignore[override]
        tools: list[Tool] = list(await call_next(context))
        fixed: list[Tool] = []
        for tool in tools:
            params = getattr(tool, "parameters", None)
            if not isinstance(params, dict):
                fixed.append(tool)
                continue
            flat = flatten_tool_parameters(params)
            if flat == params:
                fixed.append(tool)
                continue
            fixed.append(tool.model_copy(update={"parameters": flat}))
        return fixed
