"""Per-workspace plugin tool visibility (v0.18).

Multi-tenant MCP servers register RF/docs mutation tools once at boot so
they remain callable when a workspace needs them. This middleware trims
``tools/list`` and blocks ``tools/call`` when the active workspace has not
opted into the plugin:

- No ``LIVESPEC_PLUGINS`` override and no session workspace hint → core
  surface only (~19 tools).
- After any tool call with ``workspace=``, the session caches that path
  and subsequent ``tools/list`` reflects ``detect_active_plugins`` for it.
- ``LIVESPEC_PLUGINS`` env overrides apply globally (all/none/rf/docs).

Plugin tools stay registered; they are hidden or rejected here — not
unregistered at startup.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import Tool, ToolResult

from livespec_mcp.state import get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.plugins import (
    DOCS_PLUGIN_TOOL_NAMES,
    RF_MUTATION_TOOL_NAMES,
    detect_active_plugins,
    plugin_name_for_tool,
)

_SESSION_WORKSPACES: OrderedDict[str, str] = OrderedDict()
_MAX_SESSION_WORKSPACES = 64


def _remember_session_workspace(session_id: str | None, workspace: str) -> None:
    if not session_id or not workspace.strip():
        return
    _SESSION_WORKSPACES[session_id] = workspace
    _SESSION_WORKSPACES.move_to_end(session_id)
    while len(_SESSION_WORKSPACES) > _MAX_SESSION_WORKSPACES:
        _SESSION_WORKSPACES.popitem(last=False)


def _session_id(context) -> str | None:
    if context.fastmcp_context is None:
        return None
    return getattr(context.fastmcp_context, "session_id", None)


def _active_plugins(workspace: str | None) -> set[str]:
    """Plugins visible for list/call gating."""
    if workspace:
        return detect_active_plugins(get_state(workspace))
    # Global env override without a workspace (all/none/rf/docs) still works:
    if os.environ.get("LIVESPEC_PLUGINS") is not None:
        return detect_active_plugins(get_state(workspace))
    return set()


def _visible_tool_names(active: set[str]) -> frozenset[str] | None:
    """Return None when every registered tool is visible."""
    if active == {"rf", "docs"}:
        return None
    hidden: set[str] = set()
    if "rf" not in active:
        hidden |= RF_MUTATION_TOOL_NAMES
    if "docs" not in active:
        hidden |= DOCS_PLUGIN_TOOL_NAMES
    return frozenset(hidden) if hidden else None


def _plugin_blocked_payload(tool_name: str, workspace: str | None) -> dict[str, Any]:
    plugin = plugin_name_for_tool(tool_name)
    ws_hint = f" for workspace {workspace!r}" if workspace else ""
    if plugin == "rf":
        return mcp_error(
            f"RF mutation tool {tool_name!r} is not active{ws_hint}.",
            hint=(
                "import_requirements_from_markdown is always visible (bootstrap). "
                "Other RF tools need rf rows or LIVESPEC_PLUGINS=rf (or =all)."
            ),
        )
    return mcp_error(
        f"Docs plugin tool {tool_name!r} is not active{ws_hint}.",
        hint=(
            "Generate a doc row first or set LIVESPEC_PLUGINS=docs (or =all) "
            "in MCP config."
        ),
    )


class PluginVisibilityMiddleware(Middleware):
    """Filter plugin tools per workspace on list; gate calls when inactive."""

    async def on_list_tools(self, context, call_next):  # type: ignore[override]
        tools: list[Tool] = list(await call_next(context))
        session_ws = _SESSION_WORKSPACES.get(_session_id(context) or "")
        active = _active_plugins(session_ws)
        hidden = _visible_tool_names(active)
        if hidden is None:
            return tools
        return [t for t in tools if t.name not in hidden]

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        msg = context.message
        tool_name = getattr(msg, "name", "")
        args: dict[str, Any] = dict(getattr(msg, "arguments", None) or {})
        ws_arg = args.get("workspace")
        workspace = ws_arg if isinstance(ws_arg, str) and ws_arg.strip() else None
        sid = _session_id(context)
        if workspace:
            _remember_session_workspace(sid, workspace)

        plugin = plugin_name_for_tool(tool_name)
        if plugin is None:
            return await call_next(context)

        active = _active_plugins(workspace or _SESSION_WORKSPACES.get(sid or ""))
        if plugin in active:
            return await call_next(context)

        payload = _plugin_blocked_payload(tool_name, workspace)
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(payload, default=str),
                }
            ],
            structured_content=payload,
        )
