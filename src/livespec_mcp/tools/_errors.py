"""Unified error payload helper for MCP tool returns (v0.6 P4).

Every tool error must use `mcp_error(...)` so the shape is consistent:
    {"error": str, "isError": True, "did_you_mean"?: list, "hint"?: str}

The `did_you_mean` field is for typo recovery (top-N substring/edit-distance
matches against the universe of indexed names). The `hint` field is for
short actionable guidance ("run `git init` first", "set LIVESPEC_WORKSPACE",
etc.). Both are optional — omit when not applicable.

Successful-but-empty responses are NOT errors and should not use this helper.
Return the normal payload shape with empty collections instead.
"""

from __future__ import annotations

from typing import Any


def mcp_error(
    message: str,
    *,
    did_you_mean: list[Any] | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"error": message, "isError": True}
    if did_you_mean is not None:
        out["did_you_mean"] = did_you_mean
    if hint is not None:
        out["hint"] = hint
    return out
