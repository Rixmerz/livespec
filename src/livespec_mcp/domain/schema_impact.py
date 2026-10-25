"""Cross-repo impact of a schema change: which declarations of an entity a
field change touches, and which symbols, endpoints and tests read them.

Two callers share this module. ``schema_impact`` (tools/schemas.py) answers a
proposed change (add, remove, rename or retype one field). ``git_diff_impact``
(tools/analysis.py) answers a git range that changed a declaration bound with
``@schema:``, reading the fields at base and head from git.

Readers come from three channels, each tagged in ``via``: the backward call
graph of the declarations (``graph``), symbols bound with ``@schema: <entity>``
that are not schemas (``schema_binding``) and symbols sharing a Spec with a
declaration (``spec:<id>``). Readers are per declaration, never per field:
livespec records no field-level reads.

Nothing here shapes an MCP error. A bad request comes back as
``ChangeProblem`` and the test-path rule is injected, so ``domain/`` never
imports ``tools/``.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import networkx as nx

from livespec_mcp.domain.extractors import extract
from livespec_mcp.domain.git_range import DiffEntry, read_blobs, resolve_commit
from livespec_mcp.domain.graph import load_graph
from livespec_mcp.domain.schema_drift import (
    BoundEntity,
    SchemaField,
    SchemaSide,
    compatible_types,
    load_entities,
)
from livespec_mcp.domain.schema_types import NEUTRAL_TYPES, name_key
from livespec_mcp.domain.tracks import local_name, qualify

CHANGE_KINDS: tuple[str, ...] = ("add", "remove", "rename", "retype")
# The actions that ask the declaration itself to change; counted as to_change.
TO_CHANGE: frozenset[str] = frozenset(CHANGE_KINDS)
# A declaration holding both names of a rename still has a field to drop, so
# its readers are as exposed as those of one that must change.
_CONE_ACTIONS: frozenset[str] = TO_CHANGE | {"both_present"}

# The git_diff_impact section reads at this depth whatever the range's own
# max_depth is, so an entry means the same thing in every diff.
DIFF_READER_DEPTH = 3
_DIFF_SAMPLE = 5
# Field change kind -> the proposed change it asks of the other declarations.
_DIFF_CHANGE = {"added": "add", "removed": "remove", "retyped": "retype"}
_DIFF_KIND_ORDER = ("removed", "added", "retyped", "modified")

TestPath = Callable[[str], bool]


@dataclass(frozen=True)
class FieldChange:
    """One proposed change to one field of an entity."""

    kind: str  # one of CHANGE_KINDS
    field: str
    new_name: str | None = None
    new_type: str | None = None

    @property
    def field_key(self) -> str:
        return name_key(self.field)

    @property
    def new_key(self) -> str | None:
        return name_key(self.new_name) if self.new_name else None

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "field": self.field,
            "field_key": self.field_key,
            "new_name": self.new_name,
            "new_key": self.new_key,
            "new_type": self.new_type,
        }


@dataclass(frozen=True)
class ChangeProblem:
    """A request the caller turns into ``mcp_error``."""

    error: str
    did_you_mean: list[str] | None = None


class _FieldLike(Protocol):
    name: str
    field_key: str
    type: str
    item_type: str | None
    optional: bool
    nullable: bool
    wire_name: str | None


def matched_field(side: SchemaSide, key: str) -> SchemaField | None:
    """The first field, in source order, whose key or name as written keys to
    ``key``: a Python attribute and its wire alias both find the field."""
    return next((f for f in side.fields if key in (f.field_key, name_key(f.name))), None)


def check_change(entity: BoundEntity, change: FieldChange) -> ChangeProblem | None:
    """Why ``change`` cannot be answered for ``entity``, or None."""
    if change.kind not in CHANGE_KINDS:
        return ChangeProblem(
            f"Unknown change '{change.kind}': expected add, remove, rename or retype",
            did_you_mean=list(CHANGE_KINDS),
        )
    if not change.field.strip():
        return ChangeProblem("field is required: the name of the field to change")
    if change.kind == "rename" and not change.new_name:
        return ChangeProblem("rename needs new_name: the field's name after the change")
    if change.kind == "retype" and not change.new_type:
        return ChangeProblem(
            f"retype needs new_type: one of the neutral types ({', '.join(NEUTRAL_TYPES)})"
        )
    if change.new_type is not None and change.new_type not in NEUTRAL_TYPES:
        close = difflib.get_close_matches(change.new_type, NEUTRAL_TYPES, n=5, cutoff=0.5)
        return ChangeProblem(
            f"new_type '{change.new_type}' is not a neutral type: expected one of "
            f"{', '.join(NEUTRAL_TYPES)}",
            did_you_mean=close or None,
        )
    if change.kind != "add" and not any(matched_field(s, change.field_key) for s in entity.schemas):
        names: dict[str, set[str]] = {}
        for s in entity.schemas:
            for f in s.fields:
                names.setdefault(f.field_key, set()).add(f.name)
                names.setdefault(name_key(f.name), set()).add(f.name)
        close = difflib.get_close_matches(change.field_key, sorted(names), n=5, cutoff=0.5)
        return ChangeProblem(
            f"No declaration of entity '{entity.entity}' holds field '{change.field}'",
            did_you_mean=sorted({n for k in close for n in names[k]}) or None,
        )
    return None


def _carries(flavor: str, f: SchemaField, new_type: str | None) -> bool:
    """Whether ``f`` already carries ``new_type`` under the drift comparator's
    rules for the declaration's own flavor. The proposed type has no flavor,
    so only this side's loose rules apply: a TS ``string`` carries a ``uuid``,
    while a Zod ``uuid`` widened to ``string`` is a change."""
    if new_type is None:
        return False
    if f.type == new_type:
        return True
    return compatible_types(flavor, f.type, f.item_type, "", new_type, None)


def declaration_action(side: SchemaSide, change: FieldChange) -> tuple[str, SchemaField | None]:
    """The action ``change`` asks of one declaration, with the field it matched.

    add: add | present. remove: remove | absent. rename: rename (holds the old
    name, lacks the new) | both_present | renamed (holds the new, lacks the
    old; the field returned is the new one) | absent. retype: retype |
    compatible | absent."""
    held = matched_field(side, change.field_key)
    match change.kind:
        case "add":
            return ("present" if held else "add"), held
        case "remove":
            return ("remove" if held else "absent"), held
        case "rename":
            new = matched_field(side, change.new_key) if change.new_key else None
            if held and new:
                return "both_present", held
            if held:
                return "rename", held
            if new:
                return "renamed", new
            return "absent", None
        case "retype":
            if held is None:
                return "absent", None
            return (
                "compatible" if _carries(side.flavor, held, change.new_type) else "retype"
            ), held
    raise ValueError(f"unknown change kind: {change.kind!r}")


def _field_payload(f: SchemaField | None) -> dict[str, Any] | None:
    if f is None:
        return None
    return {
        "name": f.name,
        "field_key": f.field_key,
        "type": f.type,
        "item_type": f.item_type,
        "ref_target": f.ref_target,
        "raw_type": f.raw_type,
        "optional": f.optional,
        "nullable": f.nullable,
        "line": f.line,
    }


# ---------------------------------------------------------------- readers


@dataclass
class _Hit:
    via: set[str] = field(default_factory=set)
    depth: int | None = None
    reads: set[str] = field(default_factory=set)


def _backward_cone(graphs: Sequence[nx.DiGraph], source: int, max_depth: int) -> dict[int, int]:
    """Every symbol reaching ``source`` within ``max_depth`` hops, over every
    edge type and weight, with its fewest hops.

    The per-project graphs are walked as one union without composing a copy:
    each holds the edges leaving its own project, so a frontend's
    ``invokes_route`` edge into a backend handler sits in the frontend's
    graph, and asking every graph for a node's predecessors crosses it."""
    depth = {source: 0}
    frontier = deque([source])
    while frontier:
        node = frontier.popleft()
        d = depth[node]
        if d >= max_depth:
            continue
        for g in graphs:
            if node not in g:
                continue
            for pred in g.predecessors(node):
                if pred not in depth:
                    depth[pred] = d + 1
                    frontier.append(pred)
    del depth[source]
    return depth


def _ids(values: Iterable[int]) -> str:
    # One JSON parameter read with json_each: no host-parameter ceiling on a
    # wide cone, and no chunking helper to keep in step with tools/.
    return json.dumps(sorted(set(values)))


def _spec_hits(
    conn: sqlite3.Connection, project_ids: Sequence[int], decls: dict[int, str]
) -> list[tuple[int, str, set[str]]]:
    """(symbol id, spec id, declarations sharing it) for every symbol linked
    to a Spec whose id matches one linked to a declaration in ``decls``."""
    if not decls:
        return []
    pids = _ids(project_ids)
    shared: dict[str, set[str]] = {}
    for r in conn.execute(
        """SELECT DISTINCT ss.symbol_id, sp.spec_id
           FROM spec_symbol ss JOIN spec sp ON sp.id = ss.spec_id
           WHERE ss.symbol_id IN (SELECT value FROM json_each(?))
             AND sp.project_id IN (SELECT value FROM json_each(?))""",
        (_ids(decls), pids),
    ):
        shared.setdefault(r["spec_id"], set()).add(decls[int(r["symbol_id"])])
    if not shared:
        return []
    # Matched by the spec_id string, so a Spec mirrored as xrepo-* in each
    # repository joins symbols across them.
    return [
        (int(r["symbol_id"]), r["spec_id"], shared[r["spec_id"]])
        for r in conn.execute(
            """SELECT DISTINCT ss.symbol_id, sp.spec_id
               FROM spec_symbol ss
               JOIN spec sp ON sp.id = ss.spec_id
               JOIN symbol s ON s.id = ss.symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE sp.spec_id IN (SELECT value FROM json_each(?))
                 AND sp.project_id IN (SELECT value FROM json_each(?))
                 AND f.project_id IN (SELECT value FROM json_each(?))""",
            (json.dumps(sorted(shared)), pids, pids),
        )
    ]


def find_readers(
    conn: sqlite3.Connection,
    project_ids: Sequence[int],
    entity: BoundEntity,
    sources: Sequence[SchemaSide],
    *,
    max_depth: int,
    is_test_path: TestPath,
) -> list[dict[str, Any]]:
    """Readers of ``sources`` (declarations of ``entity``) in scope, one row
    per symbol, ordered by project rank, file, line and qualified name. None
    of the entity's own declarations is ever a reader."""
    hits: dict[int, _Hit] = {}
    graphs = [load_graph(conn, pid).g for pid in project_ids]
    decls: dict[int, str] = {}
    for side in sources:
        sid = side.ref.symbol_id
        if sid is None:
            continue
        decls[sid] = side.ref.qualified_name
        for node, d in _backward_cone(graphs, sid, max_depth).items():
            hit = hits.setdefault(node, _Hit())
            hit.via.add("graph")
            hit.depth = d if hit.depth is None else min(hit.depth, d)
            hit.reads.add(side.ref.qualified_name)
    for ref in entity.not_schemas:
        if ref.symbol_id is not None:
            hits.setdefault(ref.symbol_id, _Hit()).via.add("schema_binding")
    for sid, spec_id, shared in _spec_hits(conn, project_ids, decls):
        hit = hits.setdefault(sid, _Hit())
        hit.via.add(f"spec:{spec_id}")
        hit.reads |= shared
    for side in entity.schemas:
        hits.pop(side.ref.symbol_id or -1, None)
    if not hits:
        return []

    rank = {pid: i for i, pid in enumerate(project_ids)}
    ids = _ids(hits)
    meta = {
        int(r["id"]): r
        for r in conn.execute(
            """SELECT s.id, s.qualified_name, s.kind, s.start_line, s.end_line,
                      f.path, f.project_id, p.name AS project
               FROM symbol s
               JOIN file f ON f.id = s.file_id
               JOIN project p ON p.id = f.project_id
               WHERE s.id IN (SELECT value FROM json_each(?))""",
            (ids,),
        )
    }
    routes: dict[int, sqlite3.Row] = {}
    for r in conn.execute(
        """SELECT symbol_id, method, path FROM route_ref
           WHERE role = 'server' AND symbol_id IN (SELECT value FROM json_each(?))
           ORDER BY line, id""",
        (ids,),
    ):
        routes.setdefault(int(r["symbol_id"]), r)

    keyed: list[tuple[tuple[int, str, int, str], dict[str, Any]]] = []
    for sid, hit in hits.items():
        m = meta.get(sid)
        if m is None or int(m["project_id"]) not in rank:
            continue
        route = routes.get(sid)
        if route is not None:
            role = "endpoint"
        elif is_test_path(m["path"]):
            role = "test"
        else:
            role = "symbol"
        line = int(m["start_line"])
        keyed.append(
            (
                (rank[int(m["project_id"])], m["path"], line, m["qualified_name"]),
                {
                    "qualified_name": m["qualified_name"],
                    "kind": m["kind"],
                    "project": m["project"],
                    "file": m["path"],
                    "line": line,
                    "end_line": m["end_line"],
                    "role": role,
                    "via": sorted(hit.via),
                    "depth": hit.depth,
                    "reads": sorted(hit.reads),
                    "http_method": route["method"] if route is not None else None,
                    "http_path": route["path"] if route is not None else None,
                },
            )
        )
    keyed.sort(key=lambda kv: kv[0])
    return [row for _, row in keyed]


def _role_counts(readers: Sequence[dict[str, Any]]) -> dict[str, int]:
    return {
        "readers": len(readers),
        "endpoints": sum(r["role"] == "endpoint" for r in readers),
        "tests": sum(r["role"] == "test" for r in readers),
        "symbols": sum(r["role"] == "symbol" for r in readers),
    }


def project_names(conn: sqlite3.Connection, project_ids: Sequence[int]) -> list[str]:
    """Names of ``project_ids``, in that order."""
    names = {
        int(r["id"]): r["name"]
        for r in conn.execute(
            "SELECT id, name FROM project WHERE id IN (SELECT value FROM json_each(?))",
            (_ids(project_ids),),
        )
    }
    return [names[pid] for pid in project_ids if pid in names]


@dataclass(frozen=True)
class SchemaImpact:
    declarations: list[dict[str, Any]]
    readers: list[dict[str, Any]]
    by_project: list[dict[str, Any]]
    counts: dict[str, int]
    graph_readers: int


def schema_change_impact(
    conn: sqlite3.Connection,
    project_ids: Sequence[int],
    entity: BoundEntity,
    change: FieldChange,
    *,
    max_depth: int,
    is_test_path: TestPath,
) -> SchemaImpact:
    """@spec:schemas-cross-repo-schema-impact

    Every declaration of ``entity`` in scope with the action ``change`` asks
    of it, and every reader of the declarations that must change (or hold
    both names of a rename). ``change`` must have passed ``check_change``.
    Scope is ``project_ids``, home project first; each project's rank is its
    position there."""
    declarations: list[dict[str, Any]] = []
    sources: list[SchemaSide] = []
    for side in entity.schemas:
        action, matched = declaration_action(side, change)
        if action in _CONE_ACTIONS:
            sources.append(side)
        declarations.append(
            {
                **side.ref.as_payload(),
                "flavor": side.flavor,
                "action": action,
                "field": _field_payload(matched),
            }
        )
    readers = find_readers(
        conn, project_ids, entity, sources, max_depth=max_depth, is_test_path=is_test_path
    )

    by_project: list[dict[str, Any]] = []
    for name in dict.fromkeys(project_names(conn, project_ids)):
        decl = [d for d in declarations if d["project"] == name]
        read = [r for r in readers if r["project"] == name]
        if decl or read:
            roles = _role_counts(read)
            by_project.append(
                {
                    "project": name,
                    "declarations": len(decl),
                    "to_change": sum(d["action"] in TO_CHANGE for d in decl),
                    **roles,
                }
            )
    counts = {
        "declarations": len(declarations),
        "to_change": sum(d["action"] in TO_CHANGE for d in declarations),
        "both_present": sum(d["action"] == "both_present" for d in declarations),
        **_role_counts(readers),
    }
    return SchemaImpact(
        declarations=declarations,
        readers=readers,
        by_project=by_project,
        counts=counts,
        graph_readers=sum("graph" in r["via"] for r in readers),
    )


# ---------------------------------------------------------------- git ranges


def _shape(f: _FieldLike | None) -> dict[str, Any] | None:
    if f is None:
        return None
    return {
        "type": f.type,
        "item_type": f.item_type,
        "optional": f.optional,
        "nullable": f.nullable,
    }


def field_changes(
    before: Sequence[_FieldLike], after: Sequence[_FieldLike]
) -> list[dict[str, Any]]:
    """Fields of one declaration at base against head, matched by field key.

    ``removed`` / ``added``; ``retyped`` when the type or item type differ;
    ``modified`` when only optional or nullable differ. A rename is a removed
    plus an added field. Grouped by kind (removed, added, retyped, modified):
    removed fields in base order, the others in head order."""
    old: dict[str, _FieldLike] = {}
    for f in before:
        old.setdefault(f.field_key, f)
    new: dict[str, _FieldLike] = {}
    for f in after:
        new.setdefault(f.field_key, f)

    def entry(kind: str, f: _FieldLike, b: _FieldLike | None, a: _FieldLike | None):
        return {
            "kind": kind,
            "field": f.name,
            "wire_name": f.wire_name or f.name,
            "field_key": f.field_key,
            "before": _shape(b),
            "after": _shape(a),
        }

    out = [entry("removed", f, f, None) for k, f in old.items() if k not in new]
    for k, f in new.items():
        prev = old.get(k)
        if prev is None:
            out.append(entry("added", f, None, f))
        elif (prev.type, prev.item_type) != (f.type, f.item_type):
            out.append(entry("retyped", f, prev, f))
        elif (prev.optional, prev.nullable) != (f.optional, f.nullable):
            out.append(entry("modified", f, prev, f))
    out.sort(key=lambda e: _DIFF_KIND_ORDER.index(e["kind"]))
    return out


def _declared_fields(
    path: str, qname: str, source: str | None, root: Path
) -> tuple[list[Any] | None, str | None]:
    """Fields of the declaration ``qname`` in one side of ``path``.

    ``(fields, None)`` when declared, ``(None, None)`` when this side does not
    declare it, ``(None, reason)`` when the side cannot be read."""
    if source is None:
        return None, "blob unreadable"
    _, result = extract(Path(path), source, root)
    if result.grammar_missing:
        return None, f"grammar unavailable: {result.grammar_missing}"
    if result.parse_error:
        return None, "parse error"
    for s in result.schemas:
        if s.src_qname == qname:
            return list(s.fields), None
    return None, None


def _range_field_changes(
    side: SchemaSide,
    old_path: str | None,
    shas: tuple[str, str] | None,
    blobs: dict[tuple[str, str], str | None],
    root: Path,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``(field_changes, None)`` for one changed declaration, ``(None, reason)``
    when a side cannot be read, ``(None, None)`` when head does not declare it.
    ``old_path`` None means the range added the file."""
    if shas is None:
        return None, "git ref unresolvable"
    base_sha, head_sha = shas
    path, qname = side.ref.file, side.ref.qualified_name
    head, why = _declared_fields(path, qname, blobs.get((head_sha, path)), root)
    if head is None:
        return None, why
    base: list[Any] = []
    if old_path:
        old_qname = qualify(local_name(qname, path), old_path)
        found, why = _declared_fields(old_path, old_qname, blobs.get((base_sha, old_path)), root)
        if why is not None:
            return None, why
        # Not declared at base: the range added the declaration.
        base = found or []
    return field_changes(base, head), None


def diff_schema_impact(
    conn: sqlite3.Connection,
    ws_root: str,
    base_ref: str,
    head_ref: str,
    entries: Sequence[DiffEntry],
    changed_ids: Iterable[int],
    project_ids: Sequence[int],
    *,
    is_test_path: TestPath,
    summary_only: bool = False,
) -> list[dict[str, Any]]:
    """@spec:schemas-schema-impact-of-a-diff

    One entry per entity, sorted by entity key, for every changed symbol
    (``changed_ids``, home project) that is a schema declaration bound with
    ``@schema:``. Empty when the range changed none, so the caller leaves its
    payload as it was.

    Each changed declaration's fields are read from git at base and head and
    compared by field key; ``added`` / ``removed`` / ``retyped`` fields give
    the entity's other declarations ``add`` / ``remove`` / ``retype`` actions.
    A declaration the range removed entirely is left out. Readers are the
    ``schema_impact`` readers of every declaration of the entity at depth
    ``DIFF_READER_DEPTH``: counts per project and the first readers of each
    project as a sample, which ``summary_only`` drops."""
    bound: dict[str, set[int]] = {}
    for r in conn.execute(
        """SELECT DISTINCT b.entity_key, b.symbol_id
           FROM schema_binding b JOIN schema_decl d ON d.symbol_id = b.symbol_id
           WHERE b.symbol_id IN (SELECT value FROM json_each(?))""",
        (_ids(changed_ids),),
    ):
        bound.setdefault(r["entity_key"], set()).add(int(r["symbol_id"]))
    if not bound:
        return []

    base_sha, base_problem = resolve_commit(ws_root, base_ref)
    head_sha, head_problem = resolve_commit(ws_root, head_ref)
    shas = (base_sha, head_sha) if base_sha and head_sha else None
    git_problem = base_problem or head_problem
    entities = [e for e in load_entities(conn, project_ids) if e.entity_key in bound]

    # Base-side path of every head-side path the diff names: a move keeps its
    # old path, an added file has none.
    base_path: dict[str, str | None] = {e.new_path: e.old_path for e in entries if e.new_path}
    blobs: dict[tuple[str, str], str | None] = {}
    if shas is not None:
        want: list[tuple[str, str]] = []
        for e in entities:
            for s in e.schemas:
                if s.ref.symbol_id in bound[e.entity_key]:
                    want.append((shas[1], s.ref.file))
                    old = base_path.get(s.ref.file, s.ref.file)
                    if old:
                        want.append((shas[0], old))
        blobs = read_blobs(ws_root, want)

    root = Path(ws_root)
    out: list[dict[str, Any]] = []
    for e in entities:
        changed: list[dict[str, Any]] = []
        asked: dict[tuple[str, str], FieldChange] = {}
        follow_up: list[str] = []
        not_other: set[int | None] = set()
        for side in e.schemas:
            if side.ref.symbol_id not in bound[e.entity_key]:
                continue
            not_other.add(side.ref.symbol_id)
            fcs, why = _range_field_changes(
                side, base_path.get(side.ref.file, side.ref.file), shas, blobs, root
            )
            row: dict[str, Any] = {**side.ref.as_payload(), "flavor": side.flavor}
            if fcs is None and why is None:
                # Not declared at head: the range removed it, or the index is
                # ahead of head_ref. Out of scope either way.
                continue
            if fcs is None:
                if shas is None and git_problem is not None:
                    why = git_problem.error
                changed.append({**row, "field_changes": None, "field_changes_unknown": why})
                continue
            changed.append({**row, "field_changes": fcs})
            for fc in fcs:
                kind = _DIFF_CHANGE.get(fc["kind"])
                if kind is None:
                    continue
                new_type = fc["after"]["type"] if kind == "retype" else None
                # Matched by key: name_key of a key is the key itself.
                asked.setdefault(
                    (fc["field_key"], kind), FieldChange(kind, fc["field_key"], new_type=new_type)
                )
                # The wire name, not the Python attribute: the other declarations
                # of the entity are keyed by it.
                call = f"schema_impact(entity={e.entity}, change={kind}, field={fc['wire_name']}"
                call += f", new_type={new_type})" if new_type else ")"
                if call not in follow_up:
                    follow_up.append(call)
        if not changed:
            continue

        declarations = [
            {
                **side.ref.as_payload(),
                "flavor": side.flavor,
                "actions": [
                    {"field_key": key, "change": kind, "action": declaration_action(side, fc)[0]}
                    for (key, kind), fc in asked.items()
                ],
            }
            for side in e.schemas
            if side.ref.symbol_id not in not_other
        ]
        readers = find_readers(
            conn,
            project_ids,
            e,
            e.schemas,
            max_depth=DIFF_READER_DEPTH,
            is_test_path=is_test_path,
        )
        by_project: list[dict[str, Any]] = []
        sample: list[dict[str, Any]] = []
        for name in dict.fromkeys(r["project"] for r in readers):
            mine = [r for r in readers if r["project"] == name]
            by_project.append({"project": name, **_role_counts(mine)})
            sample.extend(mine[:_DIFF_SAMPLE])
        reader_section: dict[str, Any] = {
            "counts": _role_counts(readers),
            "by_project": by_project,
        }
        if not summary_only:
            reader_section["sample"] = sample
        out.append(
            {
                "entity": e.entity,
                "entity_key": e.entity_key,
                "changed": changed,
                "declarations": declarations,
                "readers": reader_section,
                "follow_up": follow_up,
            }
        )
    return out
