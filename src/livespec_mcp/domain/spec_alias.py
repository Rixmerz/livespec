"""Spec id aliases: old ids that still answer for a spec (migration 24).

An OpenSpec requirement has no id of its own — livespec derives one from its
heading. Until transliteration landed, every non-ASCII letter in a heading
became a hyphen (``Validación`` → ``validaci-n``, ``Único`` → ``nico``), and
those ids live on in two places nobody should have to rewrite by hand: rows in
existing databases and ``@spec:`` annotations in code.

So when a spec now derives a different id, the row that carried the legacy id
is renamed in place (same primary key: links, scenarios, dependencies and
history stay) and the legacy id is kept here, pointing at the spec. Annotation
scanning and every tool that takes a spec id resolve an alias to the spec it
names, and report it as deprecated so an agent can rewrite it. A live spec
whose id equals an alias always wins over the alias.

Domain only: returns data, never shapes MCP errors.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from livespec_mcp.domain.md_specs import _legacy_ospec_spec_id, _ospec_spec_id


def record_alias(conn: sqlite3.Connection, project_id: int, alias: str, spec_pk: int) -> bool:
    """Point ``alias`` at spec row ``spec_pk``. Skipped (``False``) when a live
    spec already answers to ``alias`` — an alias never shadows a real id."""
    live = conn.execute(
        "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (project_id, alias)
    ).fetchone()
    if live is not None:
        return False
    conn.execute(
        """INSERT INTO spec_alias(project_id, alias, spec_id) VALUES(?,?,?)
           ON CONFLICT(project_id, alias) DO UPDATE SET spec_id=excluded.spec_id""",
        (project_id, alias, spec_pk),
    )
    return True


def rename_spec_row(
    conn: sqlite3.Connection, project_id: int, spec_pk: int, old_id: str, new_id: str
) -> None:
    """Give row ``spec_pk`` the id ``new_id`` and keep ``old_id`` as its alias.

    The primary key does not change, so everything keyed on it (spec_symbol,
    spec_scenario → scenario_symbol, spec_dependency) follows for free. The
    coverage history is keyed on the id *string*, so it is carried over too.
    """
    if old_id == new_id:
        return
    conn.execute("UPDATE spec SET spec_id=? WHERE id=?", (new_id, spec_pk))
    conn.execute(
        "UPDATE spec_coverage_snapshot SET spec_id=? WHERE project_id=? AND spec_id=?",
        (new_id, project_id, old_id),
    )
    record_alias(conn, project_id, old_id, spec_pk)


def legacy_slug_for(spec_id: str, title: str, capability: str | None) -> str | None:
    """The legacy id ``(title, capability)`` used to derive, when it differs.

    ``None`` when ``spec_id`` is not the derived slug (a ``livespec:id``
    marker or a hand-picked id: nothing was derived, so nothing is legacy) or
    when both algorithms agree (plain ASCII headings)."""
    if spec_id != _ospec_spec_id(title, capability):
        return None
    legacy = _legacy_ospec_spec_id(title, capability)
    return legacy if legacy != spec_id else None


def adopt_legacy_spec(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    spec_id: str,
    title: str,
    capability: str | None,
    module_id: int | None,
    taken: set[str] | frozenset[str] = frozenset(),
) -> sqlite3.Row | None:
    """Rename the row stored under the legacy slug of this requirement.

    Matches only the same requirement: legacy id AND same heading AND same
    capability. ``taken`` holds ids the caller is about to write, so a legacy
    id that is also some other requirement's current id is never stolen.
    Returns the renamed row (``id``, ``spec_id`` = the OLD id) or ``None``.
    """
    legacy = legacy_slug_for(spec_id, title, capability)
    if legacy is None or legacy in taken:
        return None
    row = conn.execute(
        """SELECT id, spec_id FROM spec
           WHERE project_id=? AND spec_id=? AND title=? AND module_id IS ?""",
        (project_id, legacy, title, module_id),
    ).fetchone()
    if row is None:
        return None
    rename_spec_row(conn, project_id, int(row["id"]), legacy, spec_id)
    return row


def alias_legacy_slug(
    conn: sqlite3.Connection,
    project_id: int,
    spec_pk: int,
    *,
    spec_id: str,
    title: str,
    capability: str | None,
    taken: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """Keep the legacy slug of a spec as its alias, even on a fresh database.

    The DB may be new while the code is not: ``@spec:`` annotations written
    against the old ids must still link. Returns the alias recorded, if any."""
    legacy = legacy_slug_for(spec_id, title, capability)
    if legacy is None or legacy in taken:
        return None
    return legacy if record_alias(conn, project_id, legacy, spec_pk) else None


def alias_map(conn: sqlite3.Connection, project_id: int) -> dict[str, tuple[int, str]]:
    """``{alias: (spec_pk, current_spec_id)}`` for aliases no live id shadows."""
    rows = conn.execute(
        """SELECT a.alias, sp.id AS pk, sp.spec_id
           FROM spec_alias a JOIN spec sp ON sp.id = a.spec_id
           WHERE a.project_id=?
             AND NOT EXISTS (
                 SELECT 1 FROM spec live
                 WHERE live.project_id = a.project_id AND live.spec_id = a.alias)""",
        (project_id,),
    ).fetchall()
    return {r["alias"]: (int(r["pk"]), r["spec_id"]) for r in rows}


def resolve_spec_id(
    conn: sqlite3.Connection, project_id: int, spec_id: str
) -> tuple[str, str | None]:
    """``(id to use, alias it was reached through or None)``.

    A live id is returned as is. An alias resolves to the current id of the
    spec it points at. Anything else comes back unchanged so the caller's own
    not-found path runs."""
    live = conn.execute(
        "SELECT 1 FROM spec WHERE project_id=? AND spec_id=?", (project_id, spec_id)
    ).fetchone()
    if live is not None:
        return spec_id, None
    row = conn.execute(
        """SELECT sp.spec_id FROM spec_alias a JOIN spec sp ON sp.id = a.spec_id
           WHERE a.project_id=? AND a.alias=?""",
        (project_id, spec_id),
    ).fetchone()
    if row is None:
        return spec_id, None
    return row["spec_id"], spec_id


def deprecation_note(alias: str | None, spec_id: str) -> dict[str, Any] | None:
    """Payload fragment telling an agent which id to write instead."""
    if alias is None:
        return None
    return {"id": alias, "use": spec_id}
