"""HTTP playground bridge: Explorer UI → in-process MCP tool calls.

Used by ``livespec explorer serve`` (always on) and optionally by
``mount_explorer`` when ``[explorer] playground = true``. Default mode is
``readonly`` (only tools with ``readOnlyHint=True``). Set
``[explorer] playground_mode = "all"`` or env ``LIVESPEC_EXPLORER_PLAYGROUND=all``
to allow mutations on a local preview.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from livespec_mcp.config import RepoConfig

PlaygroundMode = Literal["readonly", "all"]

_ENV_MODE = "LIVESPEC_EXPLORER_PLAYGROUND"


def playground_enabled(repo_cfg: RepoConfig, *, host_serve: bool) -> bool:
    """Host ``explorer serve`` always enables the bridge; mounts opt in."""
    if host_serve:
        return True
    return bool(repo_cfg.explorer_playground)


def playground_mode(repo_cfg: RepoConfig) -> PlaygroundMode:
    """Resolve mode: env wins, then ``.livespec.toml``, else ``readonly``."""
    env = (os.environ.get(_ENV_MODE) or "").strip().lower()
    if env in {"all", "readonly"}:
        return env  # type: ignore[return-value]
    if env in {"1", "true", "yes"}:
        return "all"
    cfg = (repo_cfg.explorer_playground_mode or "readonly").strip().lower()
    if cfg == "all":
        return "all"
    return "readonly"


def _tool_is_readonly(tool: Any) -> bool:
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return False
    hint = getattr(ann, "readOnlyHint", None)
    if hint is None and isinstance(ann, dict):
        hint = ann.get("readOnlyHint")
    return hint is True


def _serialize_result(raw: Any) -> Any:
    """Best-effort JSON-serializable payload from a FastMCP call result."""
    if raw is None:
        return None
    data = getattr(raw, "data", None)
    if data is not None:
        try:
            json.dumps(data)
            return data
        except (TypeError, ValueError):
            return {"repr": repr(data)}
    # Fallback: structured content / content blocks
    structured = getattr(raw, "structured_content", None)
    if structured is not None:
        try:
            json.dumps(structured)
            return structured
        except (TypeError, ValueError):
            pass
    content = getattr(raw, "content", None)
    if content is not None:
        try:
            return json.loads(json.dumps(content, default=str))
        except (TypeError, ValueError):
            return {"content": repr(content)}
    try:
        json.dumps(raw)
        return raw
    except (TypeError, ValueError):
        return {"repr": repr(raw)}


async def call_playground_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    workspace: Path | str,
    mode: PlaygroundMode,
) -> tuple[int, dict[str, Any]]:
    """Execute an MCP tool in-process.

    Returns ``(http_status, body)``. Injects ``workspace`` into arguments.
    """
    from fastmcp import Client

    from livespec_mcp.server import mcp

    tool_name = (name or "").strip()
    if not tool_name:
        return 400, {"error": "tool name is required", "isError": True}

    try:
        tool = await mcp.get_tool(tool_name)
    except Exception:
        tool = None
    if tool is None:
        # Fallback scan (some FastMCP versions raise vs return None).
        tools = await mcp._list_tools()
        tool = next((t for t in tools if getattr(t, "name", None) == tool_name), None)
    if tool is None:
        return 404, {
            "error": f"unknown tool: {tool_name}",
            "isError": True,
        }

    if mode == "readonly" and not _tool_is_readonly(tool):
        return 403, {
            "error": (
                f"tool {tool_name!r} is not read-only; "
                "set [explorer] playground_mode = \"all\" "
                f"(or {_ENV_MODE}=all) to allow mutations"
            ),
            "isError": True,
            "hint": "readonly playground only executes tools with readOnlyHint=True",
        }

    args = dict(arguments or {})
    args["workspace"] = str(Path(workspace).resolve())

    try:
        async with Client(mcp) as client:
            result = await client.call_tool(tool_name, args)
    except Exception as exc:
        return 500, {
            "error": f"{type(exc).__name__}: {exc}",
            "isError": True,
        }

    payload = _serialize_result(result)
    # Tools that return mcp_error keep HTTP 200 (MCP shape) but flag isError.
    if isinstance(payload, dict) and payload.get("isError"):
        return 200, payload
    return 200, {
        "ok": True,
        "tool": tool_name,
        "result": payload,
    }
