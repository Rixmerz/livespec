"""Plan, and on request apply, one field-level change to every declaration of
an entity across the projects in scope (every repository of a ``group_db``).

``plan_schema_change`` (tools/schemas.py) calls ``plan_change`` for the plan
and ``apply_plan`` for its apply mode. Each declaration that must change is
re-read from disk and re-parsed by ``schema_edit``; the file must still hash
to what the index recorded, or its declaration is ``stale`` and gets no edits.
Readers are ``schema_impact``'s, so both tools list the same rows.

``apply_plan`` is one of the two writes livespec makes into a project (the
other is ``CHANGESPEC.md`` via ``record_track``): it rewrites only declaration
files inside the roots it is given, only when every one of them still hashes
as indexed, never creates a file and never writes the SQL migration hint.

Nothing here shapes an MCP error: refusals come back as ``PlanProblem`` and
the test-path rule is injected, so ``domain/`` never imports ``tools/``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from livespec_mcp.domain.indexer import _hash_bytes
from livespec_mcp.domain.schema_drift import BoundEntity, SchemaField, SchemaSide
from livespec_mcp.domain.schema_edit import (
    PlannedChange,
    TextEdit,
    apply_text_edits,
    edit_declaration,
    plan_action,
)
from livespec_mcp.domain.schema_impact import (
    TestPath,
    _field_payload,
    _role_counts,
    find_readers,
    project_names,
)

# The actions that ask the declaration itself to change: each one gets edits,
# or is manual or stale.
EDIT_ACTIONS: frozenset[str] = frozenset({"add", "remove", "rename", "retype", "set"})
# As in schema_impact: a declaration holding both names of a rename still has
# a field to drop, so its readers are as exposed as those of one that changes.
_READER_ACTIONS = EDIT_ACTIONS | {"both_present"}
_BOTH_PRESENT = "holds both names; drop one by hand"
_NOT_UTF8 = "the file is not UTF-8, so it is edited by hand"
OUTSIDE_APPLY = "outside apply_workspaces"
CHANGED = "changed since indexing"
MISSING = "missing"
OUTSIDE_ROOT = "resolves outside its project root"


@dataclass(frozen=True)
class PlanProblem:
    """A refusal the caller turns into ``mcp_error``."""

    error: str
    hint: str | None = None


@dataclass(frozen=True)
class _Target:
    root: str  # project.root as stored
    path: str  # file.path, project-relative POSIX
    content_hash: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class PlannedDeclaration:
    """One declaration of the entity and what the change asks of it."""

    side: SchemaSide
    root: str
    content_hash: str
    action: str
    field: SchemaField | None
    status: str  # 'edit' | 'none' | 'manual' | 'stale'
    reason: str | None
    edits: tuple[TextEdit, ...]
    migration_hint: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            **self.side.ref.as_payload(),
            "flavor": self.side.flavor,
            "action": self.action,
            "field": _field_payload(self.field),
            "status": self.status,
            "reason": self.reason,
            "edits": [e.as_payload() for e in self.edits],
            "migration_hint": self.migration_hint,
        }


@dataclass(frozen=True)
class SchemaPlan:
    declarations: tuple[PlannedDeclaration, ...]
    readers: list[dict[str, Any]]
    by_project: list[dict[str, Any]]
    counts: dict[str, int]
    graph_readers: int
    # Roots of the projects holding a stale declaration, in scope order.
    stale_roots: list[str]


@dataclass(frozen=True)
class AppliedPlan:
    written: list[dict[str, Any]]
    not_applied: list[dict[str, Any]]
    reindex: list[str]


def scope_roots(conn: sqlite3.Connection, project_ids: Sequence[int]) -> list[str]:
    """The stored root of each of ``project_ids``, in that order."""
    roots = {
        int(r["id"]): r["root"]
        for r in conn.execute(
            "SELECT id, root FROM project WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(project_ids)),),
        )
    }
    return [roots[pid] for pid in project_ids if pid in roots]


def _targets(conn: sqlite3.Connection, sides: Sequence[SchemaSide]) -> dict[int, _Target]:
    ids = [s.ref.symbol_id for s in sides if s.ref.symbol_id is not None]
    return {
        int(r["id"]): _Target(
            root=r["root"],
            path=r["path"],
            content_hash=r["content_hash"],
            start_line=int(r["start_line"]),
            end_line=int(r["end_line"]),
        )
        for r in conn.execute(
            """SELECT s.id, s.start_line, s.end_line, f.path, f.content_hash, p.root
               FROM symbol s
               JOIN file f ON f.id = s.file_id
               JOIN project p ON p.id = f.project_id
               WHERE s.id IN (SELECT value FROM json_each(?))""",
            (json.dumps(ids),),
        )
    }


def _resolve_inside(root: str, rel: str) -> Path | None:
    """``root / rel`` resolved, or None when it lands outside ``root`` (a
    symlink or ``..`` pointing elsewhere; CWE-22)."""
    base = Path(root).resolve()
    target = (Path(root) / rel).resolve()
    return target if target.is_relative_to(base) else None


def _read_fresh(root: str, rel: str, content_hash: str) -> tuple[bytes | None, str | None]:
    """``(bytes, None)`` when the file still hashes as indexed, else
    ``(None, reason)``."""
    target = _resolve_inside(root, rel)
    if target is None:
        return None, OUTSIDE_ROOT
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None, MISSING
    except OSError as e:
        return None, f"unreadable: {e.strerror or e}"
    if _hash_bytes(raw) != content_hash:
        return None, CHANGED
    return raw, None


def _plan_declaration(
    side: SchemaSide,
    target: _Target | None,
    action: str,
    matched: SchemaField | None,
    change: PlannedChange,
) -> PlannedDeclaration:
    def planned(
        status: str,
        reason: str | None = None,
        edits: tuple[TextEdit, ...] = (),
        hint: str | None = None,
    ) -> PlannedDeclaration:
        return PlannedDeclaration(
            side=side,
            root=target.root if target else "",
            content_hash=target.content_hash if target else "",
            action=action,
            field=matched,
            status=status,
            reason=reason,
            edits=edits,
            migration_hint=hint,
        )

    if action == "both_present":
        return planned("manual", _BOTH_PRESENT)
    if action not in EDIT_ACTIONS:
        return planned("none")
    if target is None:
        # The symbol left the index between load_entities and this read.
        return planned("stale", MISSING)
    raw, why = _read_fresh(target.root, target.path, target.content_hash)
    if raw is None:
        return planned("stale", why)
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return planned("manual", _NOT_UTF8)
    result = edit_declaration(
        side.flavor,
        source,
        target.path,
        target.start_line,
        target.end_line,
        matched,
        action,
        change,
    )
    return planned(result.status, result.reason, result.edits, result.migration_hint)


def plan_change(
    conn: sqlite3.Connection,
    project_ids: Sequence[int],
    entity: BoundEntity,
    change: PlannedChange,
    *,
    max_depth: int,
    is_test_path: TestPath,
) -> SchemaPlan:
    """@spec:schemas-coordinated-schema-change-plan

    Every declaration of ``entity`` in scope (``project_ids``, home first, in
    ``load_entities`` order) with the action ``change`` asks of it and its
    status: 'edit' with the exact line edits, 'manual' with the reason no
    exact edit exists, 'stale' when its file no longer hashes as indexed (no
    edits computed), 'none' when nothing is asked. Readers are
    ``schema_impact``'s, over the declarations that must change or hold both
    names of a rename. ``change`` must already be validated. Never writes."""
    targets = _targets(conn, entity.schemas)
    planned: list[PlannedDeclaration] = []
    sources: list[SchemaSide] = []
    for side in entity.schemas:
        action, matched = plan_action(side, change)
        if action in _READER_ACTIONS:
            sources.append(side)
        target = targets.get(side.ref.symbol_id) if side.ref.symbol_id is not None else None
        planned.append(_plan_declaration(side, target, action, matched, change))
    readers = find_readers(
        conn, project_ids, entity, sources, max_depth=max_depth, is_test_path=is_test_path
    )

    def tally(decls: Sequence[PlannedDeclaration]) -> dict[str, int]:
        return {
            "declarations": len(decls),
            "to_change": sum(d.action in EDIT_ACTIONS for d in decls),
            "edits": sum(len(d.edits) for d in decls),
            "manual": sum(d.status == "manual" for d in decls),
            "stale": sum(d.status == "stale" for d in decls),
        }

    by_project: list[dict[str, Any]] = []
    for name in dict.fromkeys(project_names(conn, project_ids)):
        mine = [d for d in planned if d.side.ref.project == name]
        read = [r for r in readers if r["project"] == name]
        if mine or read:
            by_project.append({"project": name, **tally(mine), **_role_counts(read)})
    files = {(d.side.ref.project, e.file) for d in planned for e in d.edits}
    counts = {**tally(planned), "files": len(files), **_role_counts(readers)}
    stale = {d.root for d in planned if d.status == "stale" and d.root}
    return SchemaPlan(
        declarations=tuple(planned),
        readers=readers,
        by_project=by_project,
        counts=counts,
        graph_readers=sum("graph" in r["via"] for r in readers),
        stale_roots=[r for r in scope_roots(conn, project_ids) if r in stale],
    )


@dataclass
class _FileWrite:
    project: str
    root: str
    file: str
    content_hash: str
    edits: list[TextEdit]


def _not_applied(d: PlannedDeclaration, reason: str) -> dict[str, Any]:
    ref = d.side.ref
    return {
        "project": ref.project,
        "file": ref.file,
        "qualified_name": ref.qualified_name,
        "reason": reason,
    }


def _write_bytes(target: Path, data: bytes) -> None:
    """Replace ``target`` with ``data`` through a temp file in its directory,
    keeping its mode, so a crash leaves the old file or the new one."""
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".livespec")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        # Gone after a successful replace; left behind by any failure.
        Path(tmp).unlink(missing_ok=True)


def apply_plan(
    plan: SchemaPlan, apply_roots: Collection[Path]
) -> tuple[AppliedPlan | None, PlanProblem | None]:
    """@spec:schemas-applying-a-schema-change-plan

    Write the plan's edits into the declaration files of the projects whose
    resolved root is in ``apply_roots``. Edits elsewhere, and manual
    declarations, go to ``not_applied`` with a reason. Every check runs
    before the first byte is written: a stale declaration in those roots, or
    a file that no longer hashes as indexed, resolves outside its root or no
    longer reads as an edit expects, refuses the whole apply. Each file is
    hashed again right before it is replaced; a failure past the first write
    names the files already written. Never re-indexes: ``reindex`` lists the
    roots to index again."""
    roots = {Path(r).resolve() for r in apply_roots}
    not_applied: list[dict[str, Any]] = []
    stale: list[PlannedDeclaration] = []
    files: dict[tuple[str, str], _FileWrite] = {}
    for d in plan.declarations:
        if d.status == "none":
            continue
        if not d.root or Path(d.root).resolve() not in roots:
            not_applied.append(_not_applied(d, OUTSIDE_APPLY))
        elif d.status == "manual":
            not_applied.append(_not_applied(d, d.reason or "edited by hand"))
        elif d.status == "stale":
            stale.append(d)
        else:
            fw = files.setdefault(
                (d.root, d.side.ref.file),
                _FileWrite(d.side.ref.project, d.root, d.side.ref.file, d.content_hash, []),
            )
            # Two declarations of one file may ask for the same import line.
            fw.edits.extend(e for e in d.edits if e not in fw.edits)
    if stale:
        listed = ", ".join(f"{d.side.ref.project}:{d.side.ref.file} ({d.reason})" for d in stale)
        return None, PlanProblem(
            f"Nothing was written: {listed}",
            hint=f"re-run index_project on {', '.join(dict.fromkeys(d.root for d in stale))} "
            "and plan again",
        )

    rewritten: list[tuple[_FileWrite, Path, bytes]] = []
    for fw in files.values():
        raw, why = _read_fresh(fw.root, fw.file, fw.content_hash)
        if raw is None:
            return None, PlanProblem(
                f"Nothing was written: {fw.project}:{fw.file} ({why})",
                hint=f"re-run index_project on {fw.root} and plan again",
            )
        try:
            new = apply_text_edits(raw.decode("utf-8"), fw.edits)
        except (UnicodeDecodeError, ValueError) as e:
            return None, PlanProblem(
                f"Nothing was written: {fw.project}:{fw.file} cannot take the edits: {e}",
                hint=f"re-run index_project on {fw.root} and plan again",
            )
        target = _resolve_inside(fw.root, fw.file)
        assert target is not None  # _read_fresh just checked it
        rewritten.append((fw, target, new.encode("utf-8")))

    written: list[dict[str, Any]] = []
    for fw, target, data in rewritten:
        done = ", ".join(f"{w['project']}:{w['file']}" for w in written) or "none"
        _, why = _read_fresh(fw.root, fw.file, fw.content_hash)
        if why is not None:
            return None, PlanProblem(
                f"{fw.project}:{fw.file} ({why}) after the plan was checked; "
                f"files already written: {done}",
                hint="re-run index_project on every project listed and plan again",
            )
        try:
            _write_bytes(target, data)
        except OSError as e:
            return None, PlanProblem(
                f"Writing {fw.project}:{fw.file} failed ({e.strerror or e}); "
                f"files already written: {done}",
                hint="re-run index_project on every project listed and plan again",
            )
        written.append({"project": fw.project, "file": fw.file, "edits": len(fw.edits)})
    reindex = list(dict.fromkeys(fw.root for fw, _, _ in rewritten))
    return AppliedPlan(written=written, not_applied=not_applied, reindex=reindex), None
