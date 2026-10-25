"""Schema tools: do the declarations bound to one entity still agree
(``schema_drift``), and what does changing one of their fields touch
(``schema_impact``)?

A frontend Zod schema, a backend Pydantic model and a migration's
``CREATE TABLE`` that say ``@schema: user`` describe one entity. Inside a
shared ``[workspace] group_db`` they may live in different repositories.

The domain logic lives in ``domain/schema_drift.py`` and
``domain/schema_impact.py``; this module scopes each call to the workspace's
projects, pages the results and shapes errors. ``compute_schema_drift`` also
backs the ``project://schemas`` resources.
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
from livespec_mcp.domain.schema_impact import (
    FieldChange,
    check_change,
    project_names,
    schema_change_impact,
)
from livespec_mcp.domain.schema_types import name_key
from livespec_mcp.state import AppState, get_state
from livespec_mcp.tool_params import Cursor, Limit, MaxDepth, SummaryOnly
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import _is_test_file_path, _workspace_note
from livespec_mcp.workspace_param import Workspace

_ANNOTATE_HINT = "annotate a schema with @schema: <entity> and re-run index_project"
_UNGROUPED_IMPACT_HINT = (
    "This workspace has no [workspace] group_db, so declarations and readers "
    "in other repositories are not listed: set group_db in .livespec.toml on "
    "each repository and re-run index_project on each"
)
_NO_GRAPH_READER_HINT = (
    "No symbol reaches these declarations through the call graph. TS "
    "interfaces and type aliases, Zod consts and SQL tables are used in type "
    "positions, which are not call edges: find those uses with "
    "grep_in_indexed_files on the declaration's name"
)


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


def compute_schema_impact(
    st: AppState,
    entity: str,
    change: str,
    field: str,
    new_name: str | None = None,
    new_type: str | None = None,
    *,
    max_depth: int = 3,
    limit: int = 200,
    cursor: int = 0,
    summary_only: bool = False,
) -> dict[str, Any]:
    """The ``schema_impact`` payload, or an ``mcp_error``."""
    pids = st.group_project_ids()
    entities = load_entities(st.conn, pids)
    chosen = next((e for e in entities if e.entity_key == name_key(entity)), None)
    if chosen is None:
        return _unknown_entity(entity, entities)
    request = FieldChange(change, field, new_name, new_type)
    problem = check_change(chosen, request)
    if problem is not None:
        return mcp_error(problem.error, did_you_mean=problem.did_you_mean)

    impact = schema_change_impact(
        st.conn, pids, chosen, request, max_depth=max_depth, is_test_path=_is_test_file_path
    )
    grouped = bool(st.settings.grouped)
    payload: dict[str, Any] = {
        "entity": chosen.entity,
        "entity_key": chosen.entity_key,
        "change": request.as_payload(),
        "scope": {"grouped": grouped, "projects": project_names(st.conn, pids)},
        "declarations": impact.declarations,
    }
    total = len(impact.readers)
    if not summary_only:
        payload["readers"] = impact.readers[cursor : cursor + limit]
    payload["by_project"] = impact.by_project
    payload["counts"] = impact.counts
    next_cursor = cursor + limit if not summary_only and cursor + limit < total else None
    payload.update({"limit": limit, "cursor": cursor, "next_cursor": next_cursor})

    hints = []
    if not grouped:
        hints.append(_UNGROUPED_IMPACT_HINT)
    if impact.graph_readers == 0:
        hints.append(_NO_GRAPH_READER_HINT)
    if hints:
        payload["hint"] = ". ".join(hints)
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

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def schema_impact(
        entity: str,
        change: str,
        field: str,
        new_name: str | None = None,
        new_type: str | None = None,
        max_depth: MaxDepth = 3,
        limit: Limit = 200,
        cursor: Cursor = 0,
        summary_only: SummaryOnly = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """@spec:schemas-cross-repo-schema-impact

        What breaks, here and in the other repositories, if I change this
        field? Call it BEFORE adding, removing, renaming or retyping a field
        of a schema bound with ``@schema: <entity>`` (Pydantic, dataclass,
        TS interface or type, Zod, SQL table). ``analyze_impact`` does not
        follow schema links; this is the tool for a shared field.

        ``change`` is ``add`` | ``remove`` | ``rename`` (needs ``new_name``) |
        ``retype`` (needs ``new_type``, a neutral type: string, int, float,
        decimal, bool, datetime, date, uuid, json, enum, list, ref, unknown).
        ``field`` matches by a convention-free key (``avatar_url`` =
        ``avatarUrl``), by attribute or wire alias.

        ``declarations``: every schema of the entity in scope (every project
        of a ``[workspace] group_db``, home first) with its file, line,
        matched field and ``action``: add | present, remove | absent,
        rename | both_present | renamed | absent, retype | compatible (the
        current type already carries the new one on that side's wire) |
        absent. ``readers``: symbols that read the declarations that must
        change, one row each with project, file, line, ``role`` (endpoint
        with ``http_method`` / ``http_path``, test, symbol), ``via`` (``graph``
        call cone up to ``max_depth`` across repos through HTTP routes,
        ``schema_binding``, ``spec:<id>``), ``depth`` and ``reads``. Readers
        are per declaration, not per field. Ordered by project, file, line.

        ``limit`` / ``cursor`` page ``readers``; ``by_project`` and ``counts``
        are exact. ``summary_only`` drops ``readers``. A ``hint`` says when
        other repositories are out of scope (no group_db) or when no call
        edge reaches the schema (type-position uses are not call edges)."""
        st = get_state(workspace)
        return compute_schema_impact(
            st,
            entity,
            change,
            field,
            new_name,
            new_type,
            max_depth=max_depth,
            limit=limit,
            cursor=cursor,
            summary_only=summary_only,
        )
