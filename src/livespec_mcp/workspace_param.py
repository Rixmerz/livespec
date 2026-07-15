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


# Annotated alias: MCP clients expose Field.description + examples on the parameter.
Workspace = Annotated[
    str | None,
    Field(
        default=None,
        description=WORKSPACE_DESCRIPTION,
        min_length=1,
        examples=[
            "/home/user/projects/my-app",
            "/Users/dev/work/api-server",
        ],
    ),
]
