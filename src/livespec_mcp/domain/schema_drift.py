"""Field-level drift between the schemas bound to one entity.

`@schema: <entity>` binds declarations that live in different layers (a
Pydantic model, a TypeScript interface, a Zod object, a SQL table) and, in a
shared `group_db`, in different repositories. `load_entities` reads the sides
of every entity bound in a set of projects; `compare_entity` compares them
field by field, matching fields by their convention-free key so `created_at`
and `createdAt` are one field.

The comparator is pure. Nothing here shapes an MCP error: an unknown entity is
for the caller to report.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from livespec_mcp.domain.schema_types import WIRE_FLAVORS

FINDING_KINDS: tuple[str, ...] = (
    "missing_field",
    "type_mismatch",
    "nullability_mismatch",
    "optional_mismatch",
)
_KIND_ORDER = {k: i for i, k in enumerate(FINDING_KINDS)}

_SQL_FLAVOR = "sql_table"

# Compatible beyond equality, keyed by the type the named side holds. JSON has
# one number type and carries dates, ids and decimals as strings; a database
# stores nested objects and arrays as JSON columns and enums as text.
_WIRE_LOOSE: dict[str, frozenset[str]] = {
    "float": frozenset({"int", "decimal"}),
    "string": frozenset({"datetime", "date", "uuid", "decimal"}),
}
_SQL_LOOSE: dict[str, frozenset[str]] = {
    "json": frozenset({"ref", "list"}),
    "string": frozenset({"enum"}),
}


@dataclass(frozen=True)
class SchemaField:
    name: str
    field_key: str
    type: str
    item_type: str | None
    ref_target: str | None
    raw_type: str | None
    optional: bool
    nullable: bool
    line: int


@dataclass(frozen=True)
class SymbolRef:
    qualified_name: str
    project: str
    file: str
    line: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "qualified_name": self.qualified_name,
            "project": self.project,
            "file": self.file,
            "line": self.line,
        }


@dataclass(frozen=True)
class SchemaSide:
    """One declaration of an entity: its location, flavor and fields in source order."""

    ref: SymbolRef
    flavor: str
    fields: tuple[SchemaField, ...]


@dataclass(frozen=True)
class BoundEntity:
    entity: str
    entity_key: str
    schemas: tuple[SchemaSide, ...]
    not_schemas: tuple[SymbolRef, ...]
    links: int


def load_entities(conn: sqlite3.Connection, project_ids: Sequence[int]) -> list[BoundEntity]:
    """Every entity bound in `project_ids`, sorted by entity key.

    Sides are ordered by project (in `project_ids` order, home first), file
    and line. A bound symbol without a schema declaration goes to
    `not_schemas` and is not compared. `links` counts the schema links whose
    two ends are both sides of the entity in scope.
    """
    if not project_ids:
        return []
    rank = {pid: i for i, pid in enumerate(project_ids)}
    marks = ",".join("?" for _ in project_ids)
    bindings = conn.execute(
        f"""SELECT b.entity, b.entity_key, s.id AS symbol_id, s.qualified_name,
                   s.start_line, f.path, f.project_id, p.name AS project, d.flavor
            FROM schema_binding b
            JOIN symbol s ON s.id = b.symbol_id
            JOIN file f ON f.id = s.file_id
            JOIN project p ON p.id = f.project_id
            LEFT JOIN schema_decl d ON d.symbol_id = s.id
            WHERE f.project_id IN ({marks})""",
        tuple(project_ids),
    ).fetchall()
    if not bindings:
        return []
    bindings = sorted(
        bindings,
        key=lambda r: (rank[r["project_id"]], r["path"], r["start_line"], r["qualified_name"]),
    )

    fields: dict[int, list[SchemaField]] = {}
    for r in conn.execute(
        f"""SELECT sf.symbol_id, sf.name, sf.field_key, sf.type, sf.item_type,
                   sf.ref_target, sf.raw_type, sf.optional, sf.nullable, sf.line
            FROM schema_field sf
            WHERE sf.symbol_id IN (
                SELECT b.symbol_id FROM schema_binding b
                JOIN symbol s ON s.id = b.symbol_id
                JOIN file f ON f.id = s.file_id
                WHERE f.project_id IN ({marks}))
            ORDER BY sf.symbol_id, sf.ordinal""",
        tuple(project_ids),
    ):
        fields.setdefault(int(r["symbol_id"]), []).append(
            SchemaField(
                name=r["name"],
                field_key=r["field_key"],
                type=r["type"],
                item_type=r["item_type"],
                ref_target=r["ref_target"],
                raw_type=r["raw_type"],
                optional=bool(r["optional"]),
                nullable=bool(r["nullable"]),
                line=int(r["line"]),
            )
        )

    by_key: dict[str, list[sqlite3.Row]] = {}
    for r in bindings:
        by_key.setdefault(r["entity_key"], []).append(r)

    links: dict[str, set[tuple[int, int]]] = {}
    for r in conn.execute("SELECT entity_key, a_symbol_id, b_symbol_id FROM schema_link"):
        links.setdefault(r["entity_key"], set()).add((int(r["a_symbol_id"]), int(r["b_symbol_id"])))

    out: list[BoundEntity] = []
    for key in sorted(by_key):
        rows = by_key[key]
        schemas: list[SchemaSide] = []
        not_schemas: list[SymbolRef] = []
        for r in rows:
            ref = SymbolRef(
                qualified_name=r["qualified_name"],
                project=r["project"],
                file=r["path"],
                line=int(r["start_line"]),
            )
            if r["flavor"] is None:
                not_schemas.append(ref)
            else:
                schemas.append(
                    SchemaSide(
                        ref=ref,
                        flavor=r["flavor"],
                        fields=tuple(fields.get(int(r["symbol_id"]), ())),
                    )
                )
        side_ids = {int(r["symbol_id"]) for r in rows if r["flavor"] is not None}
        # The entity is named as the first schema side writes it, so the
        # spelling is stable for a given scope.
        named_by = next((r for r in rows if r["flavor"] is not None), rows[0])
        out.append(
            BoundEntity(
                entity=named_by["entity"],
                entity_key=key,
                schemas=tuple(schemas),
                not_schemas=tuple(not_schemas),
                links=sum(1 for a, b in links.get(key, ()) if a in side_ids and b in side_ids),
            )
        )
    return out


def compare_entity(
    entity_key: str, sides: Sequence[SchemaSide]
) -> tuple[list[dict[str, Any]], int]:
    """@spec:schemas-field-level-schema-drift

    Compare the sides of one entity field by field. Returns the findings,
    sorted by (kind order, field key), and how many fields were not
    type-compared because a side holding them has an `unknown` type.

    Fields match by `field_key`; within one side the first by position wins.
    `missing_field`: the field is on at least one side and absent from
    another. `type_mismatch`: some pair of sides holding it is incompatible.
    `nullability_mismatch`: the sides holding it disagree on nullable, any
    flavor. `optional_mismatch`: the non-SQL sides holding it disagree on
    optional; a column's "optional" means the database fills it on insert,
    which is a different question from whether a payload may omit it.
    """
    keyed = [_first_by_key(s) for s in sides]
    findings: list[dict[str, Any]] = []
    uncomparable = 0
    for key in sorted({k for m in keyed for k in m}):
        holders = [(s, m[key]) for s, m in zip(sides, keyed, strict=True) if key in m]
        missing = [s for s, m in zip(sides, keyed, strict=True) if key not in m]
        name = holders[0][1].name
        if missing:
            findings.append(_finding(entity_key, "missing_field", name, key, holders, missing))
        if len(holders) > 1:
            if any(_uncomparable(f) for _, f in holders):
                uncomparable += 1
            elif any(
                not _compatible(a.flavor, fa.type, fa.item_type, b.flavor, fb.type, fb.item_type)
                for (a, fa), (b, fb) in combinations(holders, 2)
            ):
                findings.append(_finding(entity_key, "type_mismatch", name, key, holders))
        if len({f.nullable for _, f in holders}) > 1:
            findings.append(_finding(entity_key, "nullability_mismatch", name, key, holders))
        judged = [(s, f) for s, f in holders if s.flavor != _SQL_FLAVOR]
        if len({f.optional for _, f in judged}) > 1:
            findings.append(_finding(entity_key, "optional_mismatch", name, key, judged))
    findings.sort(key=lambda f: (_KIND_ORDER[f["kind"]], f["field_key"]))
    return findings, uncomparable


def _first_by_key(side: SchemaSide) -> dict[str, SchemaField]:
    out: dict[str, SchemaField] = {}
    for f in side.fields:
        out.setdefault(f.field_key, f)
    return out


def _uncomparable(f: SchemaField) -> bool:
    # A list whose element type no rule maps (`tags: list`, SQL `ARRAY`) has
    # nothing to compare its items against, so it is counted, not judged.
    return f.type == "unknown" or (f.type == "list" and f.item_type in (None, "unknown"))


def _loose(flavor: str, mine: str, theirs: str) -> bool:
    if flavor in WIRE_FLAVORS:
        rules = _WIRE_LOOSE
    elif flavor == _SQL_FLAVOR:
        rules = _SQL_LOOSE
    else:
        return False
    return theirs in rules.get(mine, frozenset())


def _compatible(
    a_flavor: str,
    a_type: str | None,
    a_item: str | None,
    b_flavor: str,
    b_type: str | None,
    b_item: str | None,
) -> bool:
    """Whether two neutral types can describe the same value; `ref` ~ `ref`
    whatever the targets, and lists compare their items by the same rules."""
    if a_type == b_type == "list":
        return _compatible(a_flavor, a_item, None, b_flavor, b_item, None)
    if a_type == b_type:
        return True
    if a_type is None or b_type is None:
        return False
    return _loose(a_flavor, a_type, b_type) or _loose(b_flavor, b_type, a_type)


def _finding(
    entity_key: str,
    kind: str,
    name: str,
    key: str,
    holders: Sequence[tuple[SchemaSide, SchemaField]],
    missing: Sequence[SchemaSide] = (),
) -> dict[str, Any]:
    return {
        "entity_key": entity_key,
        "kind": kind,
        "field": name,
        "field_key": key,
        "sides": [
            {
                **side.ref.as_payload(),
                "line": f.line,
                "name": f.name,
                "type": f.type,
                "item_type": f.item_type,
                "ref_target": f.ref_target,
                "raw_type": f.raw_type,
                "optional": f.optional,
                "nullable": f.nullable,
            }
            for side, f in holders
        ],
        "missing_in": [side.ref.as_payload() for side in missing],
    }
