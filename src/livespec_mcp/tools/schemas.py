"""Schema drift tool: do the declarations bound to one entity still agree?

A frontend Zod schema, a backend Pydantic model and a migration's
``CREATE TABLE`` that say ``@schema: user`` describe one entity. Inside a
shared ``[workspace] group_db`` they may live in different repositories.

The domain logic lives in ``domain/schema_drift.py``; this module scopes the
call to the workspace's projects, pages the findings and shapes errors.
``compute_schema_drift`` also backs the ``project://schemas`` resources.
"""

from __future__ import annotations

import difflib
from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.schema_drift import (
    FINDING_KINDS,
    BoundEntity,
    compare_entity,
    load_entities,
)
from livespec_mcp.domain.schema_types import name_key
from livespec_mcp.state import AppState, get_state
from livespec_mcp.tool_params import Cursor, Limit, SummaryOnly
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import _workspace_note
from livespec_mcp.workspace_param import Workspace

_ANNOTATE_HINT = "annotate a schema with @schema: <entity> and re-run index_project"


def _unknown_entity(entity: str, entities: list[BoundEntity]) -> dict[str, Any]:
    names = {e.entity_key: e.entity for e in entities}
    close = difflib.get_close_matches(name_key(entity), sorted(names), n=5, cutoff=0.5)
    return mcp_error(
        f"Entity '{entity}' is not bound to any schema in this workspace",
        did_you_mean=[names[k] for k in close] or None,
        hint=_ANNOTATE_HINT,
    )


def _single_sided_hint(single: list[str], grouped: bool) -> str:
    listed = ", ".join(single[:5]) + (", ..." if len(single) > 5 else "")
    if grouped:
        return (
            f"only one schema is bound to {listed}; annotate the other "
            "declarations with @schema: <entity> and re-run index_project"
        )
    return (
        f"only one schema is bound to {listed}. This workspace has no "
        "[workspace] group_db, so schemas in other repositories are not "
        "compared: set group_db in .livespec.toml on each repository and "
        "re-run index_project on each"
    )


def compute_schema_drift(
    st: AppState,
    entity: str | None = None,
    *,
    limit: int | None = 200,
    cursor: int = 0,
    summary_only: bool = False,
) -> dict[str, Any]:
    """The ``schema_drift`` payload. ``limit=None`` returns every finding."""
    pids = st.group_project_ids()
    entities = load_entities(st.conn, pids)
    if entity:
        key = name_key(entity)
        chosen = [e for e in entities if e.entity_key == key]
        if not chosen:
            return _unknown_entity(entity, entities)
        entities = chosen

    findings: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for e in entities:
        found, uncomparable = compare_entity(e.entity_key, e.schemas)
        findings.extend(found)
        counts = {k: sum(f["kind"] == k for f in found) for k in FINDING_KINDS}
        counts["uncomparable"] = uncomparable
        rows.append(
            {
                "entity": e.entity,
                "entity_key": e.entity_key,
                "schemas": [
                    {**s.ref.as_payload(), "flavor": s.flavor, "field_count": len(s.fields)}
                    for s in e.schemas
                ],
                "not_schemas": [r.as_payload() for r in e.not_schemas],
                "links": e.links,
                "counts": counts,
            }
        )

    names = {
        int(r["id"]): r["name"]
        for r in st.conn.execute(
            f"SELECT id, name FROM project WHERE id IN ({','.join('?' for _ in pids)})",
            tuple(pids),
        )
    }
    grouped = bool(st.settings.grouped)
    payload: dict[str, Any] = {
        "scope": {"grouped": grouped, "projects": [names[pid] for pid in pids]},
        "entities": rows,
    }
    total = len(findings)
    if not summary_only:
        page = findings[cursor:] if limit is None else findings[cursor : cursor + limit]
        payload["findings"] = page
    drifting = {f["entity_key"] for f in findings}
    payload["counts"] = {
        "entities": len(rows),
        "entities_with_drift": len(drifting),
        "findings": total,
        "by_kind": {k: sum(f["kind"] == k for f in findings) for k in FINDING_KINDS},
    }
    next_cursor = None
    if not summary_only and limit is not None and cursor + limit < total:
        next_cursor = cursor + limit
    payload.update({"limit": limit, "cursor": cursor, "next_cursor": next_cursor})

    if not rows:
        payload["hint"] = f"no schema is bound to an entity yet: {_ANNOTATE_HINT}"
    else:
        single = [r["entity"] for r in rows if len(r["schemas"]) < 2]
        if single:
            payload["hint"] = _single_sided_hint(single, grouped)
    return payload


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def schema_drift(
        entity: str | None = None,
        limit: Limit = 200,
        cursor: Cursor = 0,
        summary_only: SummaryOnly = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """@spec:schemas-field-level-schema-drift

        Do my frontend, backend and database schemas still agree? Compares,
        field by field, every schema bound to the same entity with a
        ``@schema: <entity>`` line (Python docstring, TS/Zod leading comment,
        ``--`` line above a SQL ``CREATE TABLE``): Pydantic models,
        dataclasses, TS interfaces and object type aliases, Zod
        ``z.object``, SQL tables. In a ``[workspace] group_db`` the
        comparison spans every repository of the group.

        Fields match by a convention-free key (``created_at`` = ``createdAt``).
        Findings, each naming every side with project, file and line:
        ``missing_field``, ``type_mismatch``, ``nullability_mismatch`` and
        ``optional_mismatch`` (judged among non-SQL schemas only: a column's
        default is not a payload's optional field). JSON-carried types are not
        mismatches (TS ``string`` vs ``UUID``/``datetime``, ``number`` vs
        ``int``; a SQL JSON column vs a nested model). A field whose type no
        rule maps is counted in ``uncomparable``, never judged.

        ``entity`` narrows to one entity (any spelling: ``user-profile`` =
        ``UserProfile``); unknown entities return ``did_you_mean``.
        ``limit`` / ``cursor`` page ``findings``; counts are exact.
        ``summary_only`` drops ``findings`` and keeps every count. Bound
        symbols that are not schemas are listed under ``not_schemas``."""
        st = get_state(workspace)
        return compute_schema_drift(
            st, entity, limit=limit, cursor=cursor, summary_only=summary_only
        )
