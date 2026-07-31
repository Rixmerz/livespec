"""Shared ``workspace`` tool parameter for multi-repo MCP sessions."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

# Shown on EVERY tool in the MCP schema (parameter description). Agents read this
# when filling in arguments — keep it actionable and tied to "which repo now".
WORKSPACE_DESCRIPTION = (
    "REQUIRED on every call. Absolute path to the single repository root for the "
    "project you are analyzing in this turn — pick the repo the user is editing "
    "(same folder as the open editor workspace when one repo is open), not a "
    "parent directory that holds several unrelated projects. "
    "Change only this argument to switch repos; no MCP restart needed. "
    "Example: /home/user/projects/my-app."
)

# Optional extra paragraph for high-traffic tool docstrings (complements the tool
# description; the parameter schema still carries WORKSPACE_DESCRIPTION).
WORKSPACE_DOCSTRING_NOTE = (
    "\n\n**workspace (required):** Pass the absolute repo root for the project "
    "the user is working on in this turn (same folder as the open editor workspace "
    "when they have a single repo open), e.g. "
    '`workspace="/home/user/projects/my-app"`. '
    "Never call this tool without `workspace`."
)


class WorkspaceRequiredError(ValueError):
    """Raised when a tool is invoked without ``workspace``."""


class WorkspaceNotIndexedError(ValueError):
    """Raised when a tool needs an existing index but the workspace has none.

    ``workspace`` resolves to a real directory (otherwise ``WorkspaceRequiredError``
    / ``FileNotFoundError`` would fire first) but no ``.mcp-docs/docs.db`` exists
    yet. Only ``index_project`` (and the ``livespec-mcp index`` CLI command) may
    create that file — every other entry point must raise this instead.
    """


# Plain ``str`` (not ``str | None``): Cursor and other MCP hosts mishandle
# nested ``anyOf`` / nullable unions and often render every param as ``any``
# with an empty description. Optional-in-signature ``Workspace | None = None``
# used to expand to anyOf[anyOf[str,null], null]. Make the parameter required
# in the JSON Schema instead — runtime already rejects a missing workspace.
Workspace = Annotated[
    str,
    Field(
        description=WORKSPACE_DESCRIPTION,
        min_length=1,
        examples=[
            "/home/user/projects/my-app",
            "/Users/dev/work/api-server",
        ],
    ),
]
