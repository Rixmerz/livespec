"""Shape workspace-resolution errors into the canonical mcp_error payload.

The single most common agent mistake is calling a tool without ``workspace``
(or with a path that does not exist). ``get_state`` raises
``WorkspaceRequiredError`` / ``FileNotFoundError`` for those, and the client
then sees a raw protocol error instead of the ``{error, isError, hint}`` shape
every other tool error uses.

We PRE-VALIDATE the workspace before dispatching (the same way
``PluginVisibilityMiddleware`` returns a shaped result without invoking the
tool). Returning a shaped ToolResult from a middleware that has *caught* an
in-flight exception surfaces as a protocol error, not as tool data — so we
resolve the workspace ourselves up front and short-circuit on failure.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult

from livespec_mcp import state as _state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.workspace_param import WorkspaceRequiredError

_WORKSPACE_HINT = "pass workspace='/absolute/path/to/repo' (the repository root)"


class WorkspaceErrorMiddleware(Middleware):
    """Return a shaped mcp_error when the workspace is missing / not a dir."""

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        args: dict[str, Any] = dict(getattr(context.message, "arguments", None) or {})
        ws_arg = args.get("workspace")
        ws_str = ws_arg if isinstance(ws_arg, str) and ws_arg.strip() else None
        # _resolve_workspace is monkeypatched in tests to fall back to the test
        # workspace, so look it up dynamically rather than binding at import.
        try:
            ws = _state._resolve_workspace(ws_str)
        except WorkspaceRequiredError as e:
            return _as_tool_result(mcp_error(str(e), hint=_WORKSPACE_HINT))
        if not ws.is_dir():
            return _as_tool_result(
                mcp_error(
                    f"Workspace directory not found: {ws}.",
                    hint=_WORKSPACE_HINT,
                )
            )
        return await call_next(context)


def _as_tool_result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(
        content=[{"type": "text", "text": json.dumps(payload, default=str)}],
        structured_content=payload,
    )
