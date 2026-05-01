"""Agent dispatch logging middleware (v0.8 P1).

Writes one JSONL line per `tools/call` to `<workspace>/.mcp-docs/agent_log.jsonl`.
The schema is intentionally agent-shaped — `args_redacted` strips absolute
paths so logs are shareable, `result_chars` measures payload size for
the v0.8 P3 curation pass, `latency_ms` flags slow tools.

Output schema (per line):
    {
        "ts":            ISO8601 UTC,
        "tool_name":     str,
        "args_redacted": dict,   # absolute paths stripped to <workspace>/...
        "latency_ms":    int,
        "result_chars":  int,    # len(json.dumps(result)) — exact payload size
        "error":         str | None,  # ExceptionType: short-message
        "session_id":    str | None,  # FastMCP session id when available
        "workspace":     str,    # absolute path
    }

Disable globally with `LIVESPEC_AGENT_LOG=0` in the env. Failures writing
the log file are swallowed — instrumentation must never break dispatch.

The `result_cited_in_final_answer` field mentioned in the v0.8 plan is
NOT filled here; it's a post-session annotation done by hand or by a
heuristic over the agent's text output.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware

from livespec_mcp.state import _resolve_workspace


_LOG_FILENAME = "agent_log.jsonl"


def _redact(value: Any, ws_root: str) -> Any:
    """Recursively replace `ws_root` prefix in any string with `<workspace>`.

    Keeps tool args useful for analysis while making the log shareable
    without leaking the user's home directory layout.
    """
    if isinstance(value, str):
        if ws_root and ws_root in value:
            return value.replace(ws_root, "<workspace>")
        return value
    if isinstance(value, dict):
        return {k: _redact(v, ws_root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, ws_root) for v in value]
    return value


def _result_size(result: Any) -> int:
    """Best-effort serialized size of a tool result. Returns 0 on failure."""
    if result is None:
        return 0
    try:
        return len(json.dumps(result, default=str))
    except (TypeError, ValueError):
        try:
            return len(str(result))
        except Exception:
            return 0


class AgentLogMiddleware(Middleware):
    """FastMCP middleware that appends one JSONL line per tool dispatch."""

    def __init__(self, log_filename: str = _LOG_FILENAME) -> None:
        self._log_filename = log_filename

    def _log_path(self, workspace_arg: Any) -> Path:
        """Resolve the log file path for a tool call.

        Falls back to env LIVESPEC_WORKSPACE / cwd if the call didn't pass
        an explicit `workspace` arg — same resolution policy as `state.py`.
        """
        ws = _resolve_workspace(
            workspace_arg if isinstance(workspace_arg, str) else None
        )
        return ws / ".mcp-docs" / self._log_filename

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        if os.environ.get("LIVESPEC_AGENT_LOG", "1") == "0":
            return await call_next(context)

        msg = context.message
        tool_name = getattr(msg, "name", "<unknown>")
        args: dict[str, Any] = dict(getattr(msg, "arguments", None) or {})
        ws_arg = args.get("workspace")
        log_path = self._log_path(ws_arg)
        ws_root = str(log_path.parent.parent)
        args_red = _redact(args, ws_root)

        ts = datetime.now(timezone.utc).isoformat()
        start = time.monotonic()
        result: Any = None
        error: str | None = None
        try:
            result = await call_next(context)
            return result
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:200]}"
            raise
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            session_id = None
            if context.fastmcp_context is not None:
                session_id = getattr(
                    context.fastmcp_context, "session_id", None
                )
            entry = {
                "ts": ts,
                "tool_name": tool_name,
                "args_redacted": args_red,
                "latency_ms": latency_ms,
                "result_chars": _result_size(result),
                "error": error,
                "session_id": session_id,
                "workspace": ws_root,
            }
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError:
                # Never fail dispatch on a log-write failure
                pass
