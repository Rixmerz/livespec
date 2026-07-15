"""Agent dispatch logging middleware (v0.8 P1, v0.18 agent config).

Writes one JSONL line per `tools/call` to `<workspace>/.mcp-docs/agent_log.jsonl`
when enabled. Opt-in per repo via ``.livespec.toml``::

    [agent]
    log_calls = true

Default is **off** (no surprise writes). Global overrides:

- ``LIVESPEC_AGENT_LOG=1`` — force logging for every workspace
- ``LIVESPEC_AGENT_LOG=0`` — force off (wins over config)

Output schema (per line):
    {
        "timestamp":     ISO8601 UTC (alias ``ts`` kept for compat),
        "tool_name":     str,
        "args_redacted": dict,   # paths stripped; secret-like keys redacted
        "latency_ms":    int,
        "result_chars":  int,
        "error":         str | None,
        "session_id":    str | None,
        "workspace":     str,
    }

Failures writing the log file are swallowed — instrumentation must never
break dispatch.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware

from livespec_mcp.config import load_repo_config
from livespec_mcp.state import _resolve_workspace
from livespec_mcp.workspace_param import WorkspaceRequiredError

_LOG_FILENAME = "agent_log.jsonl"
_SECRET_KEY_RE = re.compile(
    r"(password|secret|token|api[_-]?key|authorization|credential)",
    re.IGNORECASE,
)


def _redact(value: Any, ws_root: str) -> Any:
    """Recursively replace ``ws_root`` in strings and redact secret-like keys."""
    if isinstance(value, str):
        if ws_root and ws_root in value:
            return value.replace(ws_root, "<workspace>")
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _SECRET_KEY_RE.search(k):
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v, ws_root)
        return out
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


def _logging_enabled(workspace_path: Path) -> bool:
    env = os.environ.get("LIVESPEC_AGENT_LOG")
    if env == "0":
        return False
    if env == "1":
        return True
    return load_repo_config(workspace_path).agent_log_calls


class AgentLogMiddleware(Middleware):
    """FastMCP middleware that appends one JSONL line per tool dispatch."""

    def __init__(self, log_filename: str = _LOG_FILENAME) -> None:
        self._log_filename = log_filename

    def _workspace_root(self, workspace_arg: Any) -> Path | None:
        """Resolve workspace root for config + log path; ``None`` when absent.

        Never touches the filesystem: a missing/invalid workspace must surface
        the tool's own actionable error, not a logging side effect (a mkdir
        here used to mask "workspace is required" with Permission denied on
        read-only cwds and litter ``.mcp-docs-agent-log-fallback`` dirs).
        """
        arg = workspace_arg if isinstance(workspace_arg, str) and workspace_arg.strip() else None
        try:
            return _resolve_workspace(arg)
        except (WorkspaceRequiredError, FileNotFoundError, OSError):
            return None

    def _log_path(self, ws_root: Path) -> Path:
        return ws_root / ".mcp-docs" / self._log_filename

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        msg = context.message
        tool_name = getattr(msg, "name", "<unknown>")
        args: dict[str, Any] = dict(getattr(msg, "arguments", None) or {})
        ws_arg = args.get("workspace")
        ws_root = self._workspace_root(ws_arg)
        if ws_root is None:
            # No workspace to attach a log to — dispatch untouched so the tool
            # reports the missing-workspace error itself.
            return await call_next(context)

        try:
            enabled = _logging_enabled(ws_root)
        except Exception:
            # A malformed .livespec.toml must fail index_project with its own
            # actionable error, not every tool call via this middleware.
            enabled = False
        if not enabled:
            return await call_next(context)

        log_path = self._log_path(ws_root)
        ws_root_str = str(ws_root)
        args_red = _redact(args, ws_root_str)

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
                "timestamp": ts,
                "ts": ts,
                "tool_name": tool_name,
                "args_redacted": args_red,
                "latency_ms": latency_ms,
                "result_chars": _result_size(result),
                "error": error,
                "session_id": session_id,
                "workspace": ws_root_str,
            }
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError:
                pass
