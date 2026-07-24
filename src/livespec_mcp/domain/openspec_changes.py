"""OpenSpec change-proposal lifecycle: parse, ingest, apply, archive (v0.22).

An OpenSpec change lives on disk as ``openspec/changes/<name>/`` holding
``proposal.md`` / ``design.md`` / ``tasks.md`` plus delta spec files under
``specs/<capability>/spec.md`` whose requirements sit under
``## ADDED|MODIFIED|REMOVED|RENAMED Requirements`` headers. This module:

* ``parse_change_dir`` — read one change package off disk.
* ``ingest_change`` — upsert it into ``spec_change`` + ``spec_change_delta``.
* ``apply_change`` — fold the deltas into the canonical ``spec`` set (ADDED/
  MODIFIED upsert & activate, REMOVED deprecate, RENAMED upsert).
* ``archive_change`` — mark a change archived.

Idempotent throughout: re-ingesting the same folder replaces its deltas;
re-applying an applied change is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from livespec_mcp.domain.md_specs import ParsedSpec, parse_openspec_markdown
from livespec_mcp.domain.specs_sync import _sync_spec_scenarios

_PROSE_FILES = {"proposal": "proposal.md", "design": "design.md", "tasks": "tasks.md"}
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules"}


@dataclass
class ParsedChange:
    name: str
    proposal: str | None = None
    design: str | None = None
    tasks: str | None = None
    deltas: list[ParsedSpec] = field(default_factory=list)


def parse_change_dir(path: Path) -> ParsedChange:
    """Read one ``changes/<name>/`` (or ``archive/<name>/``) package."""
    path = Path(path)
    change = ParsedChange(name=path.name)
    for attr, fname in _PROSE_FILES.items():
        fp = path / fname
        if fp.is_file():
            setattr(change, attr, fp.read_text(encoding="utf-8", errors="replace"))
    specs_dir = path / "specs"
    if specs_dir.is_dir():
        for md in sorted(specs_dir.rglob("*.md")):
            if any(part in _SKIP_DIRS for part in md.parts):
                continue
            parent = md.parent.name
            capability = None if parent in {"specs"} else parent
            text = md.read_text(encoding="utf-8", errors="replace")
            for pspec in parse_openspec_markdown(text, capability=capability):
                # Only requirements under a delta header carry an operation;
                # a bare requirement with none defaults to 'added'.
                if pspec.operation is None:
                    pspec.operation = "added"
                change.deltas.append(pspec)
    return change


def ingest_change(
    st: Any, parsed: ParsedChange, *, status: str = "proposed"
) -> dict[str, Any]:
    """Upsert a parsed change (+ its deltas) into the DB. Idempotent."""
    pid = st.project_id
    existing = st.conn.execute(
        "SELECT id FROM spec_change WHERE project_id=? AND name=?", (pid, parsed.name)
    ).fetchone()
    if existing:
        change_id = int(existing["id"])
        st.conn.execute(
            """UPDATE spec_change SET status=?, proposal=?, design=?, tasks=?,
               updated_at=datetime('now') WHERE id=?""",
            (status, parsed.proposal, parsed.design, parsed.tasks, change_id),
        )
    else:
        cur = st.conn.execute(
            """INSERT INTO spec_change(project_id, name, status, proposal, design, tasks)
               VALUES(?,?,?,?,?,?)""",
            (pid, parsed.name, status, parsed.proposal, parsed.design, parsed.tasks),
        )
        change_id = int(cur.lastrowid)
    # Replace deltas wholesale so a re-ingest mirrors the folder exactly.
    st.conn.execute("DELETE FROM spec_change_delta WHERE change_id=?", (change_id,))
    for ordinal, d in enumerate(parsed.deltas):
        st.conn.execute(
            """INSERT OR IGNORE INTO spec_change_delta
               (change_id, operation, capability, spec_id, title, description, ordinal)
               VALUES(?,?,?,?,?,?,?)""",
            (change_id, d.operation, d.module, d.spec_id, d.title, d.description, ordinal),
        )
    st.conn.commit()
    return {
        "change": parsed.name,
        "status": status,
        "deltas": len(parsed.deltas),
        "change_id": change_id,
    }


def _module_id(st: Any, name: str | None) -> int | None:
    if not name:
        return None
    pid = st.project_id
    row = st.conn.execute(
        "SELECT id FROM module WHERE project_id=? AND name=?", (pid, name)
    ).fetchone()
    if row:
        return int(row["id"])
    cur = st.conn.execute(
        "INSERT INTO module(project_id, name) VALUES(?,?)", (pid, name)
    )
    return int(cur.lastrowid)


def _upsert_spec(st: Any, spec_id: str, title: str, description: str | None,
                 capability: str | None, scenarios: list[tuple[str, str]]) -> None:
    pid = st.project_id
    module_id = _module_id(st, capability)
    existing = st.conn.execute(
        "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (pid, spec_id)
    ).fetchone()
    if existing:
        spec_pk = int(existing["id"])
        st.conn.execute(
            """UPDATE spec SET title=?, description=?, status='active', module_id=?,
               updated_at=datetime('now') WHERE id=?""",
            (title, description, module_id, spec_pk),
        )
    else:
        cur = st.conn.execute(
            """INSERT INTO spec(project_id, spec_id, title, description, module_id,
               status, priority, kind)
               VALUES(?,?,?,?,?, 'active','medium','functional_requirement')""",
            (pid, spec_id, title, description, module_id),
        )
        spec_pk = int(cur.lastrowid)
    _sync_spec_scenarios(st.conn, spec_pk, scenarios)


def apply_change(st: Any, name: str) -> dict[str, Any]:
    """Fold a change's deltas into the canonical spec set."""
    pid = st.project_id
    change = st.conn.execute(
        "SELECT id, status FROM spec_change WHERE project_id=? AND name=?", (pid, name)
    ).fetchone()
    if change is None:
        return {"error": f"change {name!r} not found", "isError": True}
    change_id = int(change["id"])
    deltas = st.conn.execute(
        """SELECT operation, capability, spec_id, title, description
           FROM spec_change_delta WHERE change_id=? ORDER BY ordinal, id""",
        (change_id,),
    ).fetchall()

    from livespec_mcp.domain.md_specs import extract_scenarios

    counts = {"added": 0, "modified": 0, "removed": 0, "renamed": 0}
    for d in deltas:
        op = d["operation"]
        if op in ("added", "modified", "renamed"):
            _upsert_spec(
                st, d["spec_id"], d["title"], d["description"], d["capability"],
                extract_scenarios(d["description"] or ""),
            )
            counts[op] = counts.get(op, 0) + 1
        elif op == "removed":
            st.conn.execute(
                "UPDATE spec SET status='deprecated', updated_at=datetime('now') "
                "WHERE project_id=? AND spec_id=?",
                (pid, d["spec_id"]),
            )
            counts["removed"] += 1
    st.conn.execute(
        "UPDATE spec_change SET status='applied', updated_at=datetime('now') WHERE id=?",
        (change_id,),
    )
    st.conn.commit()
    return {"change": name, "status": "applied", "applied": counts}


def archive_change(st: Any, name: str) -> dict[str, Any]:
    """Mark a change archived (idempotent)."""
    pid = st.project_id
    cur = st.conn.execute(
        "UPDATE spec_change SET status='archived', updated_at=datetime('now') "
        "WHERE project_id=? AND name=?",
        (pid, name),
    )
    st.conn.commit()
    if cur.rowcount == 0:
        return {"error": f"change {name!r} not found", "isError": True}
    return {"change": name, "status": "archived"}


def import_changes_tree(st: Any, root: Path) -> dict[str, Any]:
    """Ingest every change under ``<root>/changes/`` and ``<root>/archive/``."""
    root = Path(root)
    results: list[dict[str, Any]] = []
    for base, status in (("changes", "proposed"), ("archive", "archived")):
        base_dir = root / base
        if not base_dir.is_dir():
            continue
        for child in sorted(base_dir.iterdir()):
            if not child.is_dir() or child.name in _SKIP_DIRS:
                continue
            parsed = parse_change_dir(child)
            results.append(ingest_change(st, parsed, status=status))
    return {"changes": results, "count": len(results)}
