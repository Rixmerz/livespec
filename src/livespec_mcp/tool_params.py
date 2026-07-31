"""Shared Annotated tool parameters with MCP-visible descriptions.

Cursor (and similar hosts) show ``Type: any`` / empty Description when the
schema uses nested ``anyOf`` or when properties lack ``description``. Keep
types plain (``str``/``int``/``bool``/``float`` + defaults) and put copy on
``Field(description=...)``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

QName = Annotated[
    str,
    Field(
        description=(
            "Fully-qualified symbol name. Separators ``::``, ``.``, and ``#`` "
            "are accepted interchangeably."
        ),
        min_length=1,
        examples=[
            "pkg.mod.Class.method",
            "src.livespec_mcp.state.get_state",
        ],
    ),
]

SymbolQuery = Annotated[
    str,
    Field(
        description=(
            "Substring or qualified name to search. Separators ``::``, ``.``, "
            "and ``#`` are normalized."
        ),
        min_length=1,
    ),
]

MaxDepth = Annotated[
    int,
    Field(
        description="How many call-graph hops to walk (1 = direct callers/callees only).",
        ge=0,
        le=50,
    ),
]

Limit = Annotated[
    int,
    Field(
        description="Max items to return in this page (pagination; count stays exact).",
        ge=1,
        le=10_000,
    ),
]

Cursor = Annotated[
    int,
    Field(
        description="Offset into the full result list; pass prior ``next_cursor`` to continue.",
        ge=0,
    ),
]

SummaryOnly = Annotated[
    bool,
    Field(
        description="If true, return counts/meta only (no item arrays) — use on huge repos.",
    ),
]

MinWeight = Annotated[
    float,
    Field(
        description=(
            "Drop call edges below this resolver weight. Default 0.6 skips ambiguous "
            "fan-out (weight 0.5). Pass 0.0 for the unfiltered cone."
        ),
        ge=0.0,
        le=1.0,
    ),
]
