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

import difflib
import json
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from livespec_mcp import state as _state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.workspace_param import WorkspaceRequiredError

_WORKSPACE_HINT = "pass workspace='/absolute/path/to/repo' (the repository root)"

# Tools allowed to run against a workspace that has no index yet — they are
# the ones that BUILD the index. Every other tool must fail clean rather than
# let a downstream get_state() call materialize an empty .mcp-docs/docs.db in
# whatever directory a typo'd `workspace` happened to point at.
_INDEX_BOOTSTRAP_TOOLS = frozenset({"index_project"})

# Spellings agents use for "the symbol I mean"; tools differ on which one they take.
_SYMBOL_ID_NAMES = frozenset(
    {"qname", "qualified_name", "symbol", "symbol_name", "symbol_qname", "target", "name"}
)


async def _tool_schema(context: Any, tool_name: str) -> dict[str, Any] | None:
    """The input schema of the tool being called, or None when it cannot be read."""
    ctx = getattr(context, "fastmcp_context", None)
    server = getattr(ctx, "fastmcp", None)
    if server is None or not tool_name:
        return None
    try:
        tool = await server.get_tool(tool_name)
    except Exception:  # unknown tool: let the dispatcher say so
        return None
    params = getattr(tool, "parameters", None)
    return params if isinstance(params, dict) else None


def unknown_argument_error(
    tool_name: str, args: dict[str, Any], schema: dict[str, Any] | None
) -> dict[str, Any] | None:
    """An ``mcp_error`` naming the accepted parameters when ``args`` has keys the
    tool does not take; None when every key is accepted (or it cannot be told).

    pydantic's own message lists only the offender, and agents then guess the
    right name (``qname`` for ``symbol_qname``, ``path`` for ``trace_path``).
    """
    if not schema or schema.get("additionalProperties") is True:
        return None
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    accepted = [str(k) for k in props]
    unknown = [k for k in args if k not in props]
    if not unknown:
        return None
    required = [str(r) for r in schema.get("required", []) if r != "workspace"]
    close: list[str] = []
    for bad in unknown:
        near = difflib.get_close_matches(bad, accepted, n=3, cutoff=0.5)
        near += [a for a in accepted if bad in a or a in bad]
        if bad in _SYMBOL_ID_NAMES:  # a symbol id is `target` / `symbol_qname` / `qname`
            near += [a for a in accepted if a in _SYMBOL_ID_NAMES]
        for name in near:
            if name not in close and name not in args:
                close.append(name)
    hint = f"{tool_name} accepts: {', '.join(accepted)}."
    if required:
        hint += f" Required: {', '.join(required)}."
    return mcp_error(
        f"{tool_name}: unknown parameter(s): {', '.join(unknown)}.",
        did_you_mean=close[:5] or None,
        hint=hint,
    )


class WorkspaceErrorMiddleware(Middleware):
    """Return a shaped mcp_error when the workspace is missing / not a dir /
    not indexed yet (for tools other than the ones that build the index), and
    when a call names a parameter the tool does not take."""

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        args: dict[str, Any] = dict(getattr(context.message, "arguments", None) or {})
        bad_args = unknown_argument_error(
            getattr(context.message, "name", ""),
            args,
            await _tool_schema(context, getattr(context.message, "name", "")),
        )
        if bad_args is not None:
            return _as_tool_result(bad_args)
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
        tool_name = getattr(context.message, "name", "")
        if tool_name not in _INDEX_BOOTSTRAP_TOOLS and not _state.workspace_db_path(ws).is_file():
            return _as_tool_result(
                mcp_error(
                    "workspace not indexed",
                    hint=f"run index_project(workspace='{ws}') first",
                )
            )
        return await call_next(context)


def _as_tool_result(payload: dict[str, Any]) -> ToolResult:
    # A real TextContent block. A list of plain dicts is not content to
    # FastMCP: it serialises the whole list to JSON and wraps THAT in one text
    # block, so the caller read `[{"type":"text","text":"{...}"}]` instead of
    # the `{error, isError, hint}` object.
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, default=str))],
        structured_content=payload,
    )
