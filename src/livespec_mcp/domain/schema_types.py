"""Neutral field types for schema declarations, and the per-language rules
that map a declared type onto them.

A schema field is described the same way whatever language declared it, so a
Pydantic model, a TypeScript interface, a Zod object and a SQL table that
describe one entity can be compared field by field. `optional` (the field may
be absent) and `nullable` (the field may hold null) are two separate facts and
no rule here derives one from the other: `x?: number` is optional and never
null, `x: int | None` is nullable and still required.

Pure functions over already-parsed nodes; nothing here parses a file. The
tree-sitter mappers take a `text(node) -> str` callable because node text lives
in the caller's source buffer.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

NEUTRAL_TYPES: tuple[str, ...] = (
    "string",
    "int",
    "float",
    "decimal",
    "bool",
    "datetime",
    "date",
    "uuid",
    "json",
    "enum",
    "list",
    "ref",
    "unknown",
)

FLAVORS: tuple[str, ...] = ("pydantic", "dataclass", "ts_interface", "ts_type", "zod", "sql_table")

# Flavors that describe a JSON payload, where a number has no int/decimal
# distinction and dates and ids travel as strings.
WIRE_FLAVORS: frozenset[str] = frozenset({"ts_interface", "ts_type", "zod"})

_NAME_KEY_DROP = re.compile(r"[_\-.\s]+")


def name_key(s: str) -> str:
    """Convention-free key: `user_id`, `userId` and `UserID` -> 'userid'."""
    return _NAME_KEY_DROP.sub("", s.casefold())


@dataclass(frozen=True)
class MappedType:
    """What a declared type means once mapped onto the neutral model."""

    type: str
    item_type: str | None = None
    ref_target: str | None = None
    optional: bool = False
    nullable: bool = False


_UNKNOWN = MappedType("unknown")


def _ref(name: str) -> MappedType:
    return MappedType("ref", ref_target=name)


def _list_of(item: MappedType) -> MappedType:
    return MappedType(
        "list",
        item_type=item.type,
        ref_target=item.ref_target if item.type == "ref" else None,
    )


# ---------- Python (Pydantic v2 semantics) ----------

_PY_SCALARS = {
    "str": "string",
    "int": "int",
    "float": "float",
    "Decimal": "decimal",
    "bool": "bool",
    "datetime": "datetime",
    "date": "date",
    "UUID": "uuid",
    "dict": "json",
    "Dict": "json",
    "Mapping": "json",
    "Any": "json",
    "object": "json",
    "Json": "json",
    "JsonValue": "json",
    "EmailStr": "string",
    "HttpUrl": "string",
    "AnyUrl": "string",
    "SecretStr": "string",
    "bytes": "string",
}
_PY_LISTS = frozenset({"list", "List", "Sequence", "set", "Set", "frozenset"})
_PY_CONSTRAINED = {
    "constr": "string",
    "conint": "int",
    "confloat": "float",
    "condecimal": "decimal",
}
# Names that are types but name no entity: never a `ref`.
_PY_UNMAPPED = frozenset({"tuple", "Tuple", "Callable", "type", "Type", "Union", "Optional"})


def _py_last(node: ast.AST) -> str | None:
    """`typing.Optional` -> 'Optional'; a bare name -> itself."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _py_is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _py_is_ellipsis(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is Ellipsis


def _py_subscript_args(node: ast.Subscript) -> list[ast.expr]:
    sl = node.slice
    return list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]


def _py_union_members(node: ast.AST) -> list[ast.expr] | None:
    """Members of `X | Y`, `Union[X, Y]` or `Optional[X]` (as `[X, None]`)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        out: list[ast.expr] = []
        for side in (node.left, node.right):
            out.extend(_py_union_members(side) or [side])
        return out
    if isinstance(node, ast.Subscript):
        base = _py_last(node.value)
        if base == "Optional":
            return [*_py_subscript_args(node), ast.Constant(value=None)]
        if base == "Union":
            return _py_subscript_args(node)
    return None


def py_type(annotation: ast.AST | None, enum_names: frozenset[str]) -> MappedType:
    """@spec:schemas-neutral-field-types

    Map a Python annotation. `enum_names` are the Enum subclasses declared in
    the same file. `optional` is never set here: in Python it comes from the
    default value (`py_optional`), not from the annotation.
    """
    if annotation is None:
        return _UNKNOWN
    if isinstance(annotation, ast.Constant):
        if not isinstance(annotation.value, str):
            return _UNKNOWN
        # A forward reference: `items: list["OrderItem"]`.
        try:
            inner = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return _UNKNOWN
        return py_type(inner, enum_names)

    members = _py_union_members(annotation)
    if members is not None:
        non_null = [m for m in members if not _py_is_none(m)]
        nullable = len(non_null) < len(members)
        if len(non_null) == 1:
            m = py_type(non_null[0], enum_names)
            return replace(m, nullable=m.nullable or nullable)
        return MappedType("unknown", nullable=nullable)

    if isinstance(annotation, ast.Subscript):
        base = _py_last(annotation.value)
        args = _py_subscript_args(annotation)
        if base == "Annotated":
            return py_type(args[0], enum_names)
        if base in _PY_LISTS:
            return _list_of(py_type(args[0], enum_names))
        if base in ("tuple", "Tuple"):
            if len(args) == 2 and _py_is_ellipsis(args[1]):
                return _list_of(py_type(args[0], enum_names))
            return _UNKNOWN
        if base == "Literal":
            return MappedType("enum")
        if base in _PY_SCALARS:
            return MappedType(_PY_SCALARS[base])
        if base is None or base in _PY_UNMAPPED:
            return _UNKNOWN
        return _ref(base)

    if isinstance(annotation, ast.Call):
        base = _py_last(annotation.func)
        return MappedType(_PY_CONSTRAINED[base]) if base in _PY_CONSTRAINED else _UNKNOWN

    name = _py_last(annotation)
    if name is None or name in _PY_UNMAPPED or name == "Literal":
        return _UNKNOWN
    if name in enum_names:
        return MappedType("enum")
    if name in _PY_SCALARS:
        return MappedType(_PY_SCALARS[name])
    if name in _PY_LISTS:
        return _list_of(_UNKNOWN)
    return _ref(name)


def py_optional(value: ast.AST | None) -> bool:
    """@spec:schemas-neutral-field-types

    Whether a class-body field may be omitted: it has a default. `...` and a
    `Field(...)` / dataclass `field()` call without a default are required.
    """
    if value is None or _py_is_ellipsis(value):
        return False
    if isinstance(value, ast.Call) and _py_last(value.func) in ("Field", "field"):
        for kw in value.keywords:
            if kw.arg == "default_factory":
                return True
            if kw.arg == "default":
                return not _py_is_ellipsis(kw.value)
        return bool(value.args) and not _py_is_ellipsis(value.args[0])
    return True


# ---------- TypeScript (tree-sitter type nodes) ----------

TextFn = Callable[[Any], str]

_TS_PREDEFINED = {
    "string": "string",
    "number": "float",
    "bigint": "int",
    "boolean": "bool",
    "any": "json",
    "unknown": "json",
    "object": "json",
}


def _ts_union_members(node: Any) -> list[Any]:
    out: list[Any] = []
    for c in node.named_children:
        if c.type == "union_type":
            out.extend(_ts_union_members(c))
        else:
            out.append(c)
    return out


def _ts_literal_kind(node: Any) -> str | None:
    """'null' / 'undefined' / 'enum' (string or number literal) for a member."""
    if node.type == "predefined_type" and node.child_count and node.children[0].type == "undefined":
        return "undefined"
    if node.type != "literal_type" or not node.children:
        return None
    inner = node.children[0]
    if inner.type in ("null", "undefined"):
        return inner.type
    if inner.type in ("string", "number", "unary_expression"):
        return "enum"
    return None


def ts_type(node: Any, text: TextFn, enum_names: frozenset[str]) -> MappedType:
    """@spec:schemas-neutral-field-types

    Map a TypeScript type node (the `type_annotation` or the type inside it).
    `enum_names` are the same-file `enum`s and aliases of literal unions.
    `| undefined` sets optional and `| null` sets nullable; `name?:` is the
    caller's to read, since it sits on the property, not on the type.
    """
    if node is None:
        return _UNKNOWN
    t = node.type
    if t in ("type_annotation", "parenthesized_type", "readonly_type"):
        inner = node.named_children[-1] if node.named_children else None
        return ts_type(inner, text, enum_names)
    if t == "union_type":
        members = _ts_union_members(node)
        kinds = [_ts_literal_kind(m) for m in members]
        optional = "undefined" in kinds
        nullable = "null" in kinds
        rest = [m for m, k in zip(members, kinds, strict=True) if k not in ("null", "undefined")]
        if rest and all(_ts_literal_kind(m) == "enum" for m in rest):
            return MappedType("enum", optional=optional, nullable=nullable)
        if len(rest) == 1:
            m = ts_type(rest[0], text, enum_names)
            return replace(m, optional=m.optional or optional, nullable=m.nullable or nullable)
        return MappedType("unknown", optional=optional, nullable=nullable)
    if t == "predefined_type":
        return MappedType(_TS_PREDEFINED.get(text(node).strip(), "unknown"))
    if t == "literal_type":
        kind = _ts_literal_kind(node)
        if kind == "enum":
            return MappedType("enum")
        return MappedType("unknown", optional=kind == "undefined", nullable=kind == "null")
    if t in ("type_identifier", "nested_type_identifier"):
        name = text(node).rsplit(".", 1)[-1].strip()
        if name == "Date":
            return MappedType("datetime")
        # tree-sitter-typescript parses `bigint` as a type identifier.
        if t == "type_identifier" and name in _TS_PREDEFINED:
            return MappedType(_TS_PREDEFINED[name])
        if t == "type_identifier" and name in enum_names:
            return MappedType("enum")
        return _ref(name)
    if t == "generic_type":
        name_node = node.child_by_field_name("name")
        name = text(name_node).rsplit(".", 1)[-1].strip() if name_node is not None else ""
        targs = node.child_by_field_name("type_arguments")
        args = list(targs.named_children) if targs is not None else []
        if name in ("Array", "ReadonlyArray"):
            return _list_of(ts_type(args[0], text, enum_names) if args else _UNKNOWN)
        if name == "Record":
            return MappedType("json")
        return _ref(name) if name else _UNKNOWN
    if t == "array_type":
        inner = node.named_children[0] if node.named_children else None
        return _list_of(ts_type(inner, text, enum_names))
    if t == "object_type":
        return MappedType("json")
    return _UNKNOWN


# ---------- Zod (tree-sitter call chains) ----------

_ZOD_BASES = {
    "string": "string",
    "number": "float",
    "bigint": "int",
    "boolean": "bool",
    "date": "datetime",
    "enum": "enum",
    "nativeEnum": "enum",
    "literal": "enum",
    "record": "json",
    "any": "json",
    "unknown": "json",
    "object": "json",
}
_ZOD_STRING_FORMATS = frozenset({"datetime", "date", "uuid"})


def zod_is_receiver(node: Any, text: TextFn, receivers: frozenset[str]) -> bool:
    """`z` (or another local bound to the zod module), or `z.coerce`."""
    if node is None:
        return False
    if node.type == "identifier":
        return text(node) in receivers
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return (
            prop is not None
            and text(prop) == "coerce"
            and zod_is_receiver(node.child_by_field_name("object"), text, receivers)
        )
    return False


def zod_type(node: Any, text: TextFn, receivers: frozenset[str]) -> MappedType:
    """@spec:schemas-neutral-field-types

    Map a Zod field expression: a base (`z.string()`, `z.coerce.number()`, or
    the identifier of another schema const) and the methods chained on it,
    applied innermost first.
    """
    mods: list[str] = []
    cur = node
    base: str | None = None
    base_args: Any = None
    ref_name: str | None = None
    while True:
        if cur is None:
            return _UNKNOWN
        if cur.type == "parenthesized_expression" and cur.named_children:
            cur = cur.named_children[-1]
            continue
        if cur.type == "identifier":
            ref_name = text(cur)
            break
        if cur.type != "call_expression":
            return _UNKNOWN
        fn = cur.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return _UNKNOWN
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        if prop is None:
            return _UNKNOWN
        if zod_is_receiver(obj, text, receivers):
            base = text(prop)
            base_args = cur.child_by_field_name("arguments")
            break
        mods.append(text(prop))
        cur = obj

    if ref_name is not None:
        m = _ref(ref_name)
    elif base == "array":
        first = base_args.named_children[0] if base_args and base_args.named_children else None
        m = _list_of(zod_type(first, text, receivers))
    else:
        m = MappedType(_ZOD_BASES.get(base or "", "unknown"))

    for mod in reversed(mods):
        if mod in ("optional", "default"):
            m = replace(m, optional=True)
        elif mod == "nullable":
            m = replace(m, nullable=True)
        elif mod == "nullish":
            m = replace(m, optional=True, nullable=True)
        elif mod == "int" and m.type == "float":
            m = replace(m, type="int")
        elif mod in _ZOD_STRING_FORMATS and m.type == "string":
            m = replace(m, type=mod)
        elif mod == "array":
            m = _list_of(m)
        elif mod in ("or", "and"):
            m = MappedType("unknown", optional=m.optional, nullable=m.nullable)
    return m


# ---------- SQL column types ----------

_SQL_BASES = {
    **dict.fromkeys(("TEXT", "VARCHAR", "CHAR", "CHARACTER", "CITEXT", "CLOB"), "string"),
    **dict.fromkeys(
        (
            "INTEGER",
            "INT",
            "SMALLINT",
            "BIGINT",
            "TINYINT",
            "MEDIUMINT",
            "INT2",
            "INT4",
            "INT8",
            "SERIAL",
            "BIGSERIAL",
            "SMALLSERIAL",
        ),
        "int",
    ),
    **dict.fromkeys(("REAL", "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE"), "float"),
    **dict.fromkeys(("NUMERIC", "DECIMAL", "MONEY"), "decimal"),
    **dict.fromkeys(("BOOLEAN", "BOOL"), "bool"),
    **dict.fromkeys(("TIMESTAMP", "TIMESTAMPTZ", "DATETIME"), "datetime"),
    "DATE": "date",
    **dict.fromkeys(("UUID", "UNIQUEIDENTIFIER"), "uuid"),
    **dict.fromkeys(("JSON", "JSONB"), "json"),
    "ENUM": "enum",
}
_SQL_SERIALS = frozenset({"SERIAL", "BIGSERIAL", "SMALLSERIAL"})
_SQL_PARENS = re.compile(r"\([^()]*\)")
_SQL_ARRAY = re.compile(r"\[[^\]]*\]|\bARRAY\b")


def sql_type(
    type_text: str | None,
    enum_names: frozenset[str],
    *,
    not_null: bool = False,
    primary_key: bool = False,
    has_default: bool = False,
    generated: bool = False,
) -> MappedType:
    """@spec:schemas-neutral-field-types

    Map a SQL column. `type_text` is the type as written (`VARCHAR(20)`,
    `TIMESTAMP WITH TIME ZONE`, `INTEGER[]`); `enum_names` are the same-file
    `CREATE TYPE x AS ENUM` names, casefolded. A column is nullable unless it
    is NOT NULL or a PRIMARY KEY, and optional when the database fills it in:
    a DEFAULT, a SERIAL type, an AUTOINCREMENT/IDENTITY/GENERATED column, or
    SQLite's INTEGER PRIMARY KEY rowid alias (`generated` covers the middle
    three).
    """
    raw = (type_text or "").strip()
    words = _SQL_PARENS.sub(" ", raw.upper())
    is_array = bool(_SQL_ARRAY.search(words))
    parts = _SQL_ARRAY.sub(" ", words).split()
    first = parts[0] if parts else ""
    type_name = raw.split("(", 1)[0].split("[", 1)[0].strip().strip('"`')
    if first in _SQL_BASES:
        base = MappedType(_SQL_BASES[first])
    elif type_name and type_name.rsplit(".", 1)[-1].strip('"`').casefold() in enum_names:
        base = MappedType("enum")
    else:
        base = _UNKNOWN
    mapped = _list_of(base) if is_array else base
    optional = (
        has_default
        or generated
        or first in _SQL_SERIALS
        or (primary_key and first == "INTEGER" and len(parts) == 1 and not is_array)
    )
    return replace(mapped, optional=optional, nullable=not not_null and not primary_key)
