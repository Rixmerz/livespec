"""Exact text edits for one field-level change of a schema entity.

``plan_schema_change`` asks, for every declaration of an entity, which lines
change and into what. Each declaration is re-parsed from the source it is
handed (Python with ``ast``, TypeScript and Zod with tree-sitter, SQL with the
``sql_schema`` lexer), so every field is placed by its own span. The line the
index stored is an anchor to verify, never a position to trust: a field that
cannot be placed exactly comes back ``manual`` with a one-sentence reason,
never as a guess.

Edits match the file they land in: its indentation, its separators and
trailing commas, its quotes, its line endings, and ``X | None`` against
``Optional[X]``. The SQL migration hint is text for a person and is never
written anywhere.

Pure text in, text out: nothing here opens a database or reads or writes a
project file; the caller hands in the source and decides what to write.
"""

from __future__ import annotations

import ast
import bisect
import itertools
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from livespec_mcp.domain.extractors import (
    _PY_NOT_FIELDS,
    ExtractedField,
    _py_base_name,
    _ts_collect_imports,
    _zod_object_decl,
    extract,
)
from livespec_mcp.domain.languages import GrammarUnavailableError, detect_language, get_parser
from livespec_mcp.domain.schema_drift import SchemaField, SchemaSide
from livespec_mcp.domain.schema_impact import (
    CHANGE_KINDS,
    FieldChange,
    declaration_action,
    matched_field,
)
from livespec_mcp.domain.schema_types import (
    _py_is_ellipsis,
    _py_is_none,
    _py_last,
    _py_subscript_args,
    _py_union_members,
    _ts_literal_kind,
    _ts_union_members,
    name_key,
    py_optional,
    zod_is_receiver,
    zod_type,
)
from livespec_mcp.domain.sql_schema import (
    _TABLE_CONSTRAINTS,
    _TABLE_PREFIXES,
    _TYPE_STOP,
    _depth0,
    _is_index_element,
    _is_name,
    _paren_names,
    _qualified_name_end,
    _split_statements,
    _Tok,
    _tokenize,
    _unquote,
)

RENDERABLE_TYPES: tuple[str, ...] = (
    "string",
    "int",
    "float",
    "decimal",
    "bool",
    "datetime",
    "date",
    "uuid",
    "json",
)
PLAN_CHANGE_KINDS: tuple[str, ...] = ("add", "remove", "rename", "retype", "optionality")

# The actions edit_declaration is asked for: the four that change a
# declaration in schema_impact, plus 'set' for an optionality change.
_EDIT_ACTIONS = frozenset({"add", "remove", "rename", "retype", "set"})

_PY_FLAVORS = frozenset({"pydantic", "dataclass"})
_TS_FLAVORS = frozenset({"ts_interface", "ts_type"})
_SQL_FLAVOR = "sql_table"

# neutral -> (pydantic/dataclass, ts_interface/ts_type, zod, sql_table)
_RENDER: dict[str, tuple[str, str, str, str]] = {
    "string": ("str", "string", "z.string()", "TEXT"),
    "int": ("int", "number", "z.number().int()", "INTEGER"),
    "float": ("float", "number", "z.number()", "DOUBLE PRECISION"),
    "decimal": ("Decimal", "string", "z.string()", "NUMERIC"),
    "bool": ("bool", "boolean", "z.boolean()", "BOOLEAN"),
    "datetime": ("datetime", "string", "z.string().datetime()", "TIMESTAMP"),
    "date": ("date", "string", "z.string().date()", "DATE"),
    "uuid": ("UUID", "string", "z.string().uuid()", "UUID"),
    "json": ("dict[str, Any]", "unknown", "z.unknown()", "JSON"),
}
_RENDER_COLUMN = {
    "pydantic": 0,
    "dataclass": 0,
    "ts_interface": 1,
    "ts_type": 1,
    "zod": 2,
    "sql_table": 3,
}
# The name a Python rendering needs bound, and the import that binds it.
_PY_IMPORTS: dict[str, tuple[str, str]] = {
    "decimal": ("Decimal", "from decimal import Decimal"),
    "datetime": ("datetime", "from datetime import datetime"),
    "date": ("date", "from datetime import date"),
    "uuid": ("UUID", "from uuid import UUID"),
    "json": ("Any", "from typing import Any"),
}
_DEFAULT_STYLE = {
    "pydantic": "snake",
    "dataclass": "snake",
    "sql_table": "snake",
    "ts_interface": "camel",
    "ts_type": "camel",
    "zod": "camel",
}

_TS_MEMBERS = frozenset(
    {
        "property_signature",
        "method_signature",
        "index_signature",
        "call_signature",
        "construct_signature",
    }
)
_ZOD_MEMBERS = frozenset(
    {"pair", "shorthand_property_identifier", "spread_element", "method_definition"}
)
_ZOD_PRESENCE = frozenset({"optional", "nullable", "nullish"})
# Kept through a retype: they say whether the field may be absent or null,
# or describe it, and hold nothing written for the old type.
_ZOD_RETYPE_KEEP = _ZOD_PRESENCE | {"describe"}
# Their argument is written for the field's type, so a retype or an
# optionality change cannot carry them over unread.
_ZOD_TYPED_ARGS = frozenset({"default", "catch", "refine", "superRefine", "transform", "pipe"})
# Applied after a presence modifier, they change what that modifier covers.
_ZOD_RESHAPE = frozenset({"array", "or", "and"})
_TS_UNWRAPPABLE = frozenset({"function_type", "constructor_type", "conditional_type"})
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_PY_STRING = re.compile(
    r"""(?P<prefix>[rRuU]?)(?P<quote>'''|\"\"\"|'|")(?P<body>.*)(?P=quote)""", re.S
)

_SQL_NOT_NULL_HINT = " -- NOT NULL needs a DEFAULT or a backfill on a table with rows"


@dataclass(frozen=True)
class PlannedChange:
    """One field-level change asked of every declaration of an entity."""

    kind: str  # one of PLAN_CHANGE_KINDS
    field: str
    new_name: str | None = None
    new_type: str | None = None
    optional: bool | None = None
    nullable: bool | None = None

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
            "optional": self.optional,
            "nullable": self.nullable,
        }

    def to_field_change(self) -> FieldChange:
        """The schema_impact change for add, remove, rename and retype."""
        if self.kind not in CHANGE_KINDS:
            raise ValueError(f"{self.kind!r} has no schema_impact change")
        return FieldChange(self.kind, self.field, self.new_name, self.new_type)


@dataclass(frozen=True)
class TextEdit:
    """Replace whole lines ``start_line..end_line`` (1-based, inclusive) of
    ``file``, which read ``old_text`` (line endings included), with
    ``new_text``. An insertion replaces its anchor line with the anchor plus
    the new lines; an empty ``new_text`` deletes the lines."""

    file: str
    start_line: int
    end_line: int
    old_text: str
    new_text: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass(frozen=True)
class DeclarationEdit:
    """What one declaration needs. ``edits`` is non-empty iff ``status`` is
    'edit', sorted by start line and non-overlapping; ``reason`` is set iff
    it is 'manual'; ``migration_hint`` only for an edited sql_table."""

    status: str  # 'edit' | 'manual'
    edits: tuple[TextEdit, ...]
    reason: str | None
    migration_hint: str | None


class _Manual(Exception):
    """The declaration cannot be edited exactly; the message says why."""


def plan_action(side: SchemaSide, change: PlannedChange) -> tuple[str, SchemaField | None]:
    """The action ``change`` asks of one declaration, with the field it matched.

    add, remove, rename and retype are schema_impact's own actions.
    optionality: 'set' when the declaration holds the field and a requested
    flag differs from it, 'unchanged' when none does, 'absent' when the field
    is not there. A column's "optional" means the database fills it in, a
    different question, so a sql_table is judged by nullable alone."""
    if change.kind != "optionality":
        return declaration_action(side, change.to_field_change())
    held = matched_field(side, change.field_key)
    if held is None:
        return "absent", None
    differs = change.nullable is not None and change.nullable != held.nullable
    if side.flavor != _SQL_FLAVOR and change.optional is not None:
        differs = differs or change.optional != held.optional
    return ("set" if differs else "unchanged"), held


def render_type(flavor: str, neutral: str) -> str:
    """How ``flavor`` writes the neutral type ``neutral`` (one of RENDERABLE_TYPES)."""
    if neutral not in _RENDER:
        raise ValueError(f"{neutral!r} has no rendering: expected one of {RENDERABLE_TYPES}")
    if flavor not in _RENDER_COLUMN:
        raise ValueError(f"unknown schema flavor: {flavor!r}")
    return _RENDER[neutral][_RENDER_COLUMN[flavor]]


def name_style(name: str) -> str | None:
    """'snake', 'kebab', 'camel' or 'pascal', or None when one word does not say."""
    if "_" in name:
        return "snake"
    if "-" in name:
        return "kebab"
    head, rest = name[:1], name[1:]
    if head.islower() and any(c.isupper() for c in rest):
        return "camel"
    if head.isupper() and any(c.islower() for c in rest):
        return "pascal"
    return None


def _words(name: str) -> list[str]:
    words: list[str] = []
    cur = ""
    for ch in name:
        if ch in "_-" or ch.isspace():
            if cur:
                words.append(cur)
            cur = ""
        elif ch.isupper() and cur[-1:].islower():
            words.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        words.append(cur)
    return words


def render_name(new_name: str, style: str | None) -> str:
    """``new_name`` spelled in ``style``; as given when ``style`` is None."""
    words = _words(new_name)
    if style is None or not words:
        return new_name
    if style == "snake":
        return "_".join(w.lower() for w in words)
    if style == "kebab":
        return "-".join(w.lower() for w in words)
    capped = [w[:1].upper() + w[1:].lower() for w in words]
    if style == "camel":
        return words[0].lower() + "".join(capped[1:])
    if style == "pascal":
        return "".join(capped)
    raise ValueError(f"unknown name style: {style!r}")


def _split_lines(source: str) -> list[str]:
    """Lines with their endings, split on '\\n' only, as ast and tree-sitter
    count them (str.splitlines also breaks on form feeds and U+2028)."""
    parts = source.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def apply_text_edits(source: str, edits: Sequence[TextEdit]) -> str:
    """``source`` with ``edits`` spliced in, bottom-up. Raises ValueError when
    an edit's lines are out of range, overlap another edit, or no longer read
    its ``old_text``."""
    lines = _split_lines(source)
    above: int | None = None
    for e in sorted(edits, key=lambda e: e.start_line, reverse=True):
        if not 1 <= e.start_line <= e.end_line <= len(lines):
            raise ValueError(
                f"{e.file}: lines {e.start_line}-{e.end_line} are outside its {len(lines)} lines"
            )
        if above is not None and e.end_line >= above:
            raise ValueError(f"{e.file}: two edits overlap at line {e.end_line}")
        if "".join(lines[e.start_line - 1 : e.end_line]) != e.old_text:
            raise ValueError(
                f"{e.file}: lines {e.start_line}-{e.end_line} changed since the edit was planned"
            )
        lines[e.start_line - 1 : e.end_line] = [e.new_text]
        above = e.start_line
    return "".join(lines)


# ---------------------------------------------------------------- text model


class _Text:
    """``source`` with line and offset arithmetic. Offsets are str indices;
    ast columns and tree-sitter positions are UTF-8 bytes and are converted
    on the way in."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = _split_lines(source)
        self.starts = list(itertools.accumulate((len(ln) for ln in self.lines), initial=0))
        try:
            self.data = source.encode("utf-8")
        except UnicodeEncodeError as e:
            raise _Manual("the file holds text that is not valid UTF-8") from e
        self.ascii = len(self.data) == len(source)
        first = next((ln for ln in self.lines if ln.endswith("\n")), "")
        self.default_eol = "\r\n" if first.endswith("\r\n") else "\n"

    def line_of(self, offset: int) -> int:
        return min(max(bisect.bisect_right(self.starts, offset), 1), len(self.lines))

    def line_start(self, line: int) -> int:
        return self.starts[line - 1]

    def eol(self, line: int) -> str:
        ln = self.lines[line - 1]
        if ln.endswith("\r\n"):
            return "\r\n"
        return "\n" if ln.endswith("\n") else self.default_eol

    def content_end(self, line: int) -> int:
        ln = self.lines[line - 1]
        return self.starts[line] - (len(ln) - len(ln.rstrip("\r\n")))

    def indent(self, line: int) -> str:
        ln = self.lines[line - 1]
        return ln[: len(ln) - len(ln.lstrip(" \t"))]

    def char(self, byte_offset: int) -> int:
        if self.ascii:
            return byte_offset
        return len(self.data[:byte_offset].decode("utf-8"))

    def node_span(self, node: Any) -> tuple[int, int]:
        return self.char(node.start_byte), self.char(node.end_byte)

    def py_offset(self, lineno: int, col: int) -> int:
        line = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8"))

    def py_span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.py_offset(node.lineno, node.col_offset),  # type: ignore[attr-defined]
            self.py_offset(node.end_lineno, node.end_col_offset),  # type: ignore[attr-defined]
        )

    def seg(self, span: tuple[int, int]) -> str:
        return self.source[span[0] : span[1]]

    def blank_before(self, offset: int) -> bool:
        return not self.source[self.line_start(self.line_of(offset)) : offset].strip()

    def rest_of_line(self, offset: int) -> str:
        return self.source[offset : self.content_end(self.line_of(offset))]


# (start, end, replacement) over str offsets of the source.
_Splice = tuple[int, int, str]


def _to_edits(text: _Text, file: str, splices: Sequence[_Splice]) -> tuple[TextEdit, ...]:
    """Whole-line TextEdits for ``splices``: splices touching the same lines
    become one edit. A pure insertion belongs to the line it sits on."""
    spans = []
    for i, (s, e, new) in enumerate(splices):
        first = text.line_of(s)
        last = text.line_of(e - 1) if e > s else first
        spans.append((first, last, s, e, i, new))
    spans.sort()
    groups: list[list[Any]] = []
    for sp in spans:
        if groups and sp[0] <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], sp[1])
            groups[-1][2].append(sp)
        else:
            groups.append([sp[0], sp[1], [sp]])
    out: list[TextEdit] = []
    for first, last, members in groups:
        lo, hi = text.starts[first - 1], text.starts[last]
        old = text.source[lo:hi]
        ordered = sorted(members, key=lambda m: (m[2], m[3], m[4]))
        for a, b in itertools.pairwise(ordered):
            if b[2] < a[3]:
                raise _Manual("two of its edits overlap, so none is placed")
        block = old
        # Bottom-up, and the later of two insertions at one offset first, so
        # the earlier one ends up in front of it.
        for _, _, s, e, _, new in reversed(ordered):
            block = block[: s - lo] + new + block[e - lo :]
        if block != old:
            out.append(TextEdit(file, first, last, old, block))
    return tuple(out)


# ---------------------------------------------------------------- separated lists


@dataclass(frozen=True)
class _Member:
    """One element of a separated list (an interface member, an object
    property, a column or table constraint): its span, the end of the
    separator right after it, if any, and where the doc comment on the lines
    right above it starts, if it has one."""

    start: int
    end: int
    sep_end: int | None
    sep: str | None
    doc_start: int | None = None


def _list_insert(
    text: _Text,
    anchor: _Member,
    next_start: int,
    member_text: str,
    sep: str,
) -> list[_Splice]:
    """Splices adding ``member_text`` right after ``anchor``. ``next_start``
    is where the next element or the closing bracket begins. On its own line
    when the anchor sits on its own lines; otherwise inline. ``sep`` is the
    list's separator ('' for a newline-separated TypeScript body)."""
    last_end = anchor.sep_end if anchor.sep_end is not None else anchor.end
    anchor_line = text.line_of(last_end - 1)
    if text.blank_before(anchor.start) and text.line_of(next_start) > anchor_line:
        indent = text.indent(text.line_of(anchor.start))
        end = text.content_end(anchor_line)
        eol = text.eol(anchor_line)
        if anchor.sep_end is not None:
            return [(end, end, eol + indent + member_text + (anchor.sep or sep))]
        splices: list[_Splice] = [(anchor.end, anchor.end, sep)] if sep else []
        return [*splices, (end, end, eol + indent + member_text)]
    if anchor.sep_end is not None:
        return [(anchor.sep_end, anchor.sep_end, f" {member_text}{anchor.sep or sep}")]
    return [(anchor.end, anchor.end, f"{sep or ';'} {member_text}")]


def _list_remove(
    text: _Text, members: Sequence[_Member], idx: int, comment_marks: tuple[str, ...]
) -> list[_Splice]:
    """Splices removing ``members[idx]``: its whole lines (with its doc
    comment) when it has them to itself, else just its text. The last element
    loses the separator it no longer needs from the one before, so a list
    that ends without one still does."""
    if len(members) == 1:
        raise _Manual("it is the declaration's only element, which would leave it empty")
    m = members[idx]
    prev = members[idx - 1] if idx > 0 else None
    nxt = members[idx + 1] if idx + 1 < len(members) else None
    last_end = m.sep_end if m.sep_end is not None else m.end
    top = m.doc_start if m.doc_start is not None else m.start
    first_line, last_line = text.line_of(top), text.line_of(last_end - 1)
    tail = text.rest_of_line(last_end).strip()
    own_lines = (
        text.blank_before(top)
        and (not tail or tail.startswith(comment_marks))
        and (prev is None or text.line_of((prev.sep_end or prev.end) - 1) < first_line)
        and (nxt is None or text.line_of(nxt.start) > last_line)
    )
    if own_lines:
        splices: list[_Splice] = [(text.line_start(first_line), text.starts[last_line], "")]
        if nxt is None and m.sep_end is None and prev is not None and prev.sep_end is not None:
            splices.append((prev.end, prev.sep_end, ""))
        return splices
    if m.sep_end is not None:
        end = m.sep_end
        while end < len(text.source) and text.source[end] in " \t":
            end += 1
        return [(m.start, end, "")]
    if prev is not None:
        return [(prev.end, m.end, "")]
    return [(m.start, m.end, "")]


def _side_style(flavor: str, fields: Sequence[ExtractedField], wire: str | None) -> str:
    """The style of the matched field's wire name; else the most common style
    among the declaration's fields; else the flavor's default."""
    if wire is not None and (style := name_style(wire)):
        return style
    counts = Counter(s for f in fields if (s := name_style(f.wire_name or f.name)))
    if counts:
        return counts.most_common(1)[0][0]
    return _DEFAULT_STYLE[flavor]


def _quoted_name(name: str, written: str) -> str:
    """``name`` as a property key, quoted like ``written`` when that was a
    string, or with double quotes when it is not an identifier."""
    if written[:1] in ("'", '"', "`"):
        return written[0] + name + written[0]
    return name if _IDENTIFIER.fullmatch(name) else f'"{name}"'


# ---------------------------------------------------------------- entry point


def _declared_fields(
    flavor: str, source: str, file: str, decl_start: int, decl_end: int
) -> list[ExtractedField]:
    """The fields of the declaration spanning ``decl_start..decl_end``, as
    the indexer reads them (inherited ones included, wire names kept)."""
    _, result = extract(Path(file), source, Path("."))
    if result.grammar_missing:
        raise _Manual(
            f"the {result.grammar_missing} grammar is unavailable here, so the file cannot be read"
        )
    if result.parse_error:
        raise _Manual("the file does not parse, so no field can be placed in it")
    spans = {
        s.qualified_name
        for s in result.symbols
        if s.start_line == decl_start and s.end_line == decl_end
    }
    found = [s for s in result.schemas if s.src_qname in spans and s.flavor == flavor]
    if len(found) != 1:
        raise _Manual(f"no single {flavor} declaration spans lines {decl_start}-{decl_end}")
    return found[0].fields


def edit_declaration(
    flavor: str,
    source: str,
    file: str,
    decl_start: int,
    decl_end: int,
    field: SchemaField | None,
    action: str,
    change: PlannedChange,
) -> DeclarationEdit:
    """@spec:schemas-coordinated-schema-change-plan

    The exact edits ``action`` (add, remove, rename, retype or set) asks of
    the declaration spanning ``decl_start..decl_end`` of ``source``, which is
    ``file`` (project-relative, POSIX). ``field`` is the field ``plan_action``
    matched (None for add); its line is verified against the source, never
    trusted. 'manual' with a reason whenever an edit cannot be placed
    exactly; a sql_table edit carries its PostgreSQL migration hint."""
    if action not in _EDIT_ACTIONS:
        raise ValueError(f"no edit for action {action!r}: expected one of {sorted(_EDIT_ACTIONS)}")
    if flavor not in _RENDER_COLUMN:
        raise ValueError(f"unknown schema flavor: {flavor!r}")
    if action != "add" and field is None:
        raise ValueError(f"action {action!r} needs the field it matched")
    try:
        if action in ("add", "retype") and change.new_type not in _RENDER:
            raise _Manual(
                f"type {change.new_type!r} has no rendering, so the declaration is edited by hand"
            )
        if field is not None and not decl_start <= field.line <= decl_end:
            raise _Manual(
                f"field '{field.name}' is declared on line {field.line}, outside the declaration "
                "(inherited from a base), and editing the base changes every subclass"
            )
        text = _Text(source)
        declared = _declared_fields(flavor, source, file, decl_start, decl_end)
        hint: str | None = None
        if flavor in _PY_FLAVORS:
            splices = _python_splices(
                text, flavor, decl_start, decl_end, field, action, change, declared
            )
        elif flavor in _TS_FLAVORS:
            splices = _ts_splices(
                text, file, flavor, decl_start, decl_end, field, action, change, declared
            )
        elif flavor == "zod":
            splices = _zod_splices(
                text, file, decl_start, decl_end, field, action, change, declared
            )
        else:
            splices, hint = _sql_splices(text, decl_start, decl_end, field, action, change)
        edits = _to_edits(text, file, splices)
    except _Manual as m:
        return DeclarationEdit("manual", (), str(m), None)
    if not edits:
        return DeclarationEdit("manual", (), "the declaration already reads as asked", None)
    return DeclarationEdit("edit", edits, None, hint)


def _new_flags(change: PlannedChange, cur_optional: bool, cur_nullable: bool) -> tuple[bool, bool]:
    optional = cur_optional if change.optional is None else change.optional
    nullable = cur_nullable if change.nullable is None else change.nullable
    return optional, nullable


# ---------------------------------------------------------------- Python


def _py_fields(cls: ast.ClassDef) -> list[ast.AnnAssign]:
    """The class-body statements the extractor reads as fields."""
    out: list[ast.AnnAssign] = []
    for stmt in cls.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        if name.startswith("_") or name == "model_config":
            continue
        if _py_base_name(stmt.annotation) in _PY_NOT_FIELDS:
            continue
        out.append(stmt)
    return out


def _py_bound_names(tree: ast.Module) -> set[str]:
    """Names bound at module level, through if/try/with blocks."""
    names: set[str] = set()

    def visit(stmts: Sequence[ast.stmt]) -> None:
        for s in stmts:
            if isinstance(s, ast.Import):
                names.update(a.asname or a.name.split(".")[0] for a in s.names)
            elif isinstance(s, ast.ImportFrom):
                names.update(a.asname or a.name for a in s.names)
            elif isinstance(s, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(s.name)
            elif isinstance(s, ast.Assign):
                names.update(t.id for t in s.targets if isinstance(t, ast.Name))
            elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                names.add(s.target.id)
            for attr in ("body", "orelse", "finalbody"):
                if not isinstance(s, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(getattr(s, attr, []) or [])
            for h in getattr(s, "handlers", []) or []:
                visit(h.body)

    visit(tree.body)
    return names


def _py_import_splice(text: _Text, tree: ast.Module, neutral: str) -> list[_Splice]:
    """One import line after the last top-level import when the rendering of
    ``neutral`` names something the module does not bind."""
    needed = _PY_IMPORTS.get(neutral)
    if needed is None or needed[0] in _py_bound_names(tree):
        return []
    imports = [s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))]
    anchor: int | None = None
    if imports:
        anchor = imports[-1].end_lineno
    elif ast.get_docstring(tree, clean=False) is not None:
        anchor = tree.body[0].end_lineno
    if anchor is None:
        return [(0, 0, needed[1] + text.default_eol)]
    end = text.content_end(anchor)
    return [(end, end, text.eol(anchor) + needed[1])]


def _py_nullable_style(tree: ast.Module, text: _Text) -> str | None:
    """How the file spells Optional (e.g. 'Optional', 'typing.Optional') when
    it uses Optional and never '| None'; None to write 'X | None'."""
    spelling: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if _py_is_none(node.left) or _py_is_none(node.right):
                return None
        elif (
            spelling is None
            and isinstance(node, ast.Subscript)
            and _py_last(node.value) == "Optional"
        ):
            spelling = text.seg(text.py_span(node.value))
    return spelling


def _py_nullable(type_text: str, optional_spelling: str | None) -> str:
    if optional_spelling:
        return f"{optional_spelling}[{type_text}]"
    return f"{type_text} | None"


def _py_core(annotation: ast.expr) -> tuple[ast.expr, ast.expr | None, bool]:
    """(the annotation's type node, the one type it holds besides None or
    None when several, whether None is a member), looking through
    ``Annotated[...]``."""
    ann = annotation
    if isinstance(ann, ast.Subscript) and _py_last(ann.value) == "Annotated":
        ann = _py_subscript_args(ann)[0]
    if isinstance(ann, ast.Constant):
        raise _Manual("its annotation is a string, which is edited by hand")
    members = _py_union_members(ann)
    if members is None:
        return ann, ann, False
    non_null = [m for m in members if not _py_is_none(m)]
    has_none = len(non_null) < len(members)
    return ann, (non_null[0] if len(non_null) == 1 else None), has_none


def _py_field_call(value: ast.expr | None) -> ast.Call | None:
    if isinstance(value, ast.Call) and _py_last(value.func) in ("Field", "field"):
        return value
    return None


def _py_default(value: ast.expr | None) -> ast.expr | None:
    """The default a field value gives, or None when it gives none."""
    if value is None or _py_is_ellipsis(value):
        return None
    call = _py_field_call(value)
    if call is None:
        return value
    for kw in call.keywords:
        if kw.arg in ("default", "default_factory"):
            return None if _py_is_ellipsis(kw.value) else kw.value
    if call.args and not _py_is_ellipsis(call.args[0]):
        return call.args[0]
    return None


def _py_alias_literal(stmt: ast.AnnAssign) -> ast.Constant | None:
    """The string literal py_field_alias reads the field's alias from: the
    value first, then the last ``Annotated`` metadata entry."""
    candidates: list[ast.expr | None] = [stmt.value]
    ann = stmt.annotation
    if isinstance(ann, ast.Subscript) and _py_last(ann.value) == "Annotated":
        candidates.extend(reversed(_py_subscript_args(ann)[1:]))
    for node in candidates:
        if isinstance(node, ast.Call) and _py_last(node.func) == "Field":
            for kw in node.keywords:
                if (
                    kw.arg == "alias"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                    and kw.value.value
                ):
                    return kw.value
    return None


def _py_string(text: _Text, node: ast.Constant, value: str) -> _Splice:
    """Rewrite one string literal, keeping its prefix and quotes."""
    s, e = text.py_span(node)
    m = _PY_STRING.fullmatch(text.source[s:e])
    if m is None or m.group("quote")[0] in m.group("body"):
        raise _Manual("its alias is not a single plain string literal")
    return s, e, f"{m.group('prefix')}{m.group('quote')}{value}{m.group('quote')}"


def _py_paren_after(text: _Text, func: ast.expr) -> int:
    """The offset right after the '(' that opens a call of ``func``."""
    pos = text.py_span(func)[1]
    while pos < len(text.source) and text.source[pos] in " \t":
        pos += 1
    if text.source[pos : pos + 1] != "(":
        raise _Manual("its Field() call cannot be placed exactly")
    return pos + 1


def _py_set_default_none(text: _Text, stmt: ast.AnnAssign) -> list[_Splice]:
    """Give a field without a default the default None."""
    value = stmt.value
    if value is None:
        end = text.py_offset(stmt.end_lineno, stmt.end_col_offset)  # type: ignore[arg-type]
        return [(end, end, " = None")]
    if _py_is_ellipsis(value):
        return [(*text.py_span(value), "None")]
    call = _py_field_call(value)
    if call is None:
        raise _Manual("its value is not a default livespec can read")
    if call.args and _py_is_ellipsis(call.args[0]):
        return [(*text.py_span(call.args[0]), "None")]
    for kw in call.keywords:
        if kw.arg == "default" and _py_is_ellipsis(kw.value):
            return [(*text.py_span(kw.value), "None")]
    at = _py_paren_after(text, call.func)
    more = ", " if call.args or call.keywords else ""
    return [(at, at, f"default=None{more}")]


def _py_drop_default(text: _Text, stmt: ast.AnnAssign, flavor: str) -> list[_Splice]:
    """Make a field with a default required."""
    value = stmt.value
    assert value is not None
    call = _py_field_call(value)
    if call is None:
        return [(text.py_span(stmt.annotation)[1], text.py_span(value)[1], "")]
    if flavor == "dataclass":
        raise _Manual("a dataclass field() default is removed by hand")
    if any(kw.arg == "default_factory" for kw in call.keywords):
        raise _Manual("its default comes from a default_factory, which is removed by hand")
    for kw in call.keywords:
        if kw.arg == "default":
            return [(*text.py_span(kw.value), "...")]
    return [(*text.py_span(call.args[0]), "...")]


def _dataclass_order_broken(cls: ast.ClassDef, optional_flags: Sequence[bool]) -> bool:
    """Whether a dataclass would hold a required field after a defaulted one,
    which it rejects at import (unless every field is keyword-only)."""
    for dec in cls.decorator_list:
        if isinstance(dec, ast.Call) and any(
            kw.arg == "kw_only" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in dec.keywords
        ):
            return False
    seen_default = False
    for optional in optional_flags:
        if optional:
            seen_default = True
        elif seen_default:
            return True
    return False


def _python_splices(
    text: _Text,
    flavor: str,
    decl_start: int,
    decl_end: int,
    field: SchemaField | None,
    action: str,
    change: PlannedChange,
    declared: Sequence[ExtractedField],
) -> list[_Splice]:
    tree = ast.parse(text.source)
    cls = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.lineno == decl_start and n.end_lineno == decl_end
        ),
        None,
    )
    if cls is None:
        raise _Manual(f"no class spans lines {decl_start}-{decl_end}")
    own = _py_fields(cls)

    def owns_its_lines(stmt: ast.stmt) -> bool:
        lo, hi = stmt.lineno, stmt.end_lineno or stmt.lineno
        if lo == cls.lineno:
            return False
        return not any(
            o is not stmt and o.lineno <= hi and (o.end_lineno or o.lineno) >= lo for o in cls.body
        )

    if action == "add":
        return _py_add(text, tree, cls, own, flavor, change, declared, owns_its_lines)

    assert field is not None
    stmt = next(
        (s for s in own if s.lineno == field.line and s.target.id == field.name),  # type: ignore[union-attr]
        None,
    )
    if stmt is None:
        raise _Manual(f"field '{field.name}' is not at its indexed line {field.line}")
    if not owns_its_lines(stmt):
        raise _Manual(f"field '{field.name}' shares its line with another statement")

    if action == "remove":
        if len(cls.body) == 1:
            raise _Manual("it is the class's only statement, which would leave the body empty")
        end_line = stmt.end_lineno or stmt.lineno
        return [(text.line_start(stmt.lineno), text.starts[end_line], "")]

    if action == "rename":
        assert change.new_name is not None
        alias = _py_alias_literal(stmt)
        aliased = field.field_key != name_key(field.name)
        if aliased and alias is None:
            raise _Manual(
                "its wire name comes from somewhere other than a Field(alias=...) literal"
            )
        splices: list[_Splice] = []
        # An alias that keys like the attribute still keys the field, so it
        # moves with the attribute.
        if alias is not None and (aliased or name_key(alias.value) == name_key(field.name)):
            style = _side_style(flavor, declared, alias.value)
            splices.append(_py_string(text, alias, render_name(change.new_name, style)))
        if not aliased:
            splices.append((*text.py_span(stmt.target), render_name(change.new_name, "snake")))
        return splices

    if action == "retype":
        assert change.new_type is not None
        default = _py_default(stmt.value)
        if default is not None and not _py_is_none(default):
            raise _Manual(f"its default value is written for its current type, {field.type}")
        outer, core, has_none = _py_core(stmt.annotation)
        rendered = render_type(flavor, change.new_type)
        if core is not None:
            splices = [(*text.py_span(core), rendered)]
        else:
            style = _py_nullable_style(tree, text)
            new = _py_nullable(rendered, style) if has_none else rendered
            splices = [(*text.py_span(outer), new)]
        return splices + _py_import_splice(text, tree, change.new_type)

    # set: optional and/or nullable
    outer, core, has_none = _py_core(stmt.annotation)
    cur_optional = py_optional(stmt.value)
    optional, nullable = _new_flags(change, cur_optional, has_none)
    default = _py_default(stmt.value)
    if optional and not nullable and (default is None or _py_is_none(default)):
        raise _Manual("needs a default value")
    splices = []
    if nullable != has_none:
        if nullable:
            wrapped = _py_nullable(text.seg(text.py_span(outer)), _py_nullable_style(tree, text))
            splices.append((*text.py_span(outer), wrapped))
        elif core is None:
            raise _Manual("its type holds several types besides None, which is edited by hand")
        else:
            splices.append((*text.py_span(outer), text.seg(text.py_span(core))))
    if optional != cur_optional:
        if optional:
            splices.extend(_py_set_default_none(text, stmt))
        else:
            splices.extend(_py_drop_default(text, stmt, flavor))
    if flavor == "dataclass":
        flags = [
            optional if (f.line == field.line and f.name == field.name) else f.optional
            for f in declared
        ]
        if _dataclass_order_broken(cls, flags) and not _dataclass_order_broken(
            cls, [f.optional for f in declared]
        ):
            raise _Manual("a dataclass field without a default cannot follow one with a default")
    return splices


def _py_add(
    text: _Text,
    tree: ast.Module,
    cls: ast.ClassDef,
    own: Sequence[ast.AnnAssign],
    flavor: str,
    change: PlannedChange,
    declared: Sequence[ExtractedField],
    owns_its_lines: Any,
) -> list[_Splice]:
    assert change.new_type is not None
    optional, nullable = bool(change.optional), bool(change.nullable)
    if optional and not nullable:
        raise _Manual("needs a default value")
    if flavor == "dataclass" and _dataclass_order_broken(
        cls, [*(f.optional for f in declared), optional]
    ):
        raise _Manual("a dataclass field without a default cannot follow one with a default")
    annotation = render_type(flavor, change.new_type)
    if nullable:
        annotation = _py_nullable(annotation, _py_nullable_style(tree, text))
    line = f"{render_name(change.field, 'snake')}: {annotation}" + (" = None" if optional else "")
    if own:
        anchor = own[-1]
        if not owns_its_lines(anchor):
            raise _Manual("its last field shares a line with another statement")
        end_line = anchor.end_lineno or anchor.lineno
        end = text.content_end(end_line)
        splices: list[_Splice] = [
            (end, end, text.eol(end_line) + text.indent(anchor.lineno) + line)
        ]
    else:
        first = cls.body[0]
        if first.lineno == cls.lineno:
            raise _Manual("its body sits on the class line, so a field cannot be placed under it")
        if ast.get_docstring(cls, clean=False) is not None:
            # Under the docstring: a statement above it would demote it.
            end_line = first.end_lineno or first.lineno
            end = text.content_end(end_line)
            splices = [(end, end, text.eol(end_line) + text.indent(first.lineno) + line)]
        else:
            start = text.line_start(first.lineno)
            splices = [(start, start, text.indent(first.lineno) + line + text.eol(first.lineno))]
    return splices + _py_import_splice(text, tree, change.new_type)


# ---------------------------------------------------------------- TypeScript


def _ts_tree(text: _Text, file: str, languages: tuple[str, ...]) -> tuple[Any, str]:
    lang = detect_language(Path(file))
    if lang not in languages:
        raise _Manual(f"a {lang or 'file of this kind'} file cannot hold this declaration")
    try:
        parser = get_parser(lang)
    except GrammarUnavailableError as e:
        raise _Manual(f"the {lang} grammar is unavailable here: {e.error}") from e
    return parser.parse(text.data), lang


def _ts_members(text: _Text, body: Any, kinds: frozenset[str]) -> list[tuple[Any, _Member]]:
    """(node, member) for each element of an interface body, object type or
    object literal, with the ';' or ',' that follows it and the ``/** */``
    comment alone on the lines right above it."""
    out: list[tuple[Any, _Member]] = []
    pending: Any = None
    doc: int | None = None
    comment: Any = None  # the last comment since the previous element or separator

    def close(sep: Any) -> None:
        s, e = text.node_span(pending)
        sep_end = text.char(sep.end_byte) if sep is not None else None
        out.append((pending, _Member(s, e, sep_end, sep.type if sep is not None else None, doc)))

    for c in body.children:
        if c.type in kinds:
            if pending is not None:
                close(None)
            pending, doc, comment = c, _ts_doc_start(text, comment, c), None
        elif c.type in (";", ",") and pending is not None:
            close(c)
            pending = comment = None
        elif c.type == "comment":
            comment = c
    if pending is not None:
        close(None)
    return out


def _ts_doc_start(text: _Text, comment: Any, member: Any) -> int | None:
    """Where ``comment`` starts when it is the ``/** */`` doc of ``member``:
    alone on its lines, ending on the line right above it."""
    if comment is None or comment.end_point[0] != member.start_point[0] - 1:
        return None
    start = text.char(comment.start_byte)
    if not text.source.startswith("/**", start) or not text.blank_before(start):
        return None
    return start


def _ts_list_sep(members: Sequence[tuple[Any, _Member]]) -> str | None:
    return next((m.sep for _, m in members if m.sep), None)


def _ts_closing(text: _Text, body: Any) -> int:
    last = body.children[-1] if body.children else None
    if last is None or last.type != "}":
        raise _Manual("the declaration's closing brace cannot be found")
    return text.char(last.start_byte)


def _ts_splices(
    text: _Text,
    file: str,
    flavor: str,
    decl_start: int,
    decl_end: int,
    field: SchemaField | None,
    action: str,
    change: PlannedChange,
    declared: Sequence[ExtractedField],
) -> list[_Splice]:
    tree, _ = _ts_tree(text, file, ("typescript", "tsx"))
    node_type = "interface_declaration" if flavor == "ts_interface" else "type_alias_declaration"
    decls = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if (
            n.type == node_type
            and n.start_point[0] + 1 == decl_start
            and n.end_point[0] + 1 == decl_end
        ):
            decls.append(n)
        stack.extend(n.children)
    if len(decls) != 1:
        raise _Manual(f"no single {node_type} spans lines {decl_start}-{decl_end}")
    body = decls[0].child_by_field_name("body" if flavor == "ts_interface" else "value")
    if body is None or body.type not in ("interface_body", "object_type"):
        raise _Manual("the declaration has no body of fields")
    members = _ts_members(text, body, _TS_MEMBERS)
    sep = _ts_list_sep(members)

    if action == "add":
        assert change.new_type is not None
        if not members:
            raise _Manual("the declaration has no member to place the new field after")
        name = render_name(change.field, _side_style(flavor, declared, None))
        member = (
            f"{_quoted_name(name, name)}{'?' if change.optional else ''}: "
            f"{render_type(flavor, change.new_type)}{' | null' if change.nullable else ''}"
        )
        anchor = members[-1][1]
        return _list_insert(text, anchor, _ts_closing(text, body), member, sep or "")

    assert field is not None
    idx = next(
        (
            i
            for i, (n, _) in enumerate(members)
            if n.type == "property_signature"
            and n.start_point[0] + 1 == field.line
            and _ts_key(text, n.child_by_field_name("name")) == field.name
        ),
        None,
    )
    if idx is None:
        raise _Manual(f"field '{field.name}' is not at its indexed line {field.line}")
    prop = members[idx][0]

    if action == "remove":
        return _list_remove(text, [m for _, m in members], idx, ("//", "/*"))

    if action == "rename":
        assert change.new_name is not None
        key = prop.child_by_field_name("name")
        name = render_name(change.new_name, _side_style(flavor, declared, field.name))
        return [(*text.node_span(key), _quoted_name(name, text.seg(text.node_span(key))))]

    ann = prop.child_by_field_name("type")
    if ann is None or not ann.named_children:
        raise _Manual(f"field '{field.name}' has no type annotation to edit")
    ty = ann.named_children[-1]
    union = _ts_union_members(ty) if ty.type == "union_type" else [ty]
    kinds = [_ts_literal_kind(m) for m in union]

    if action == "retype":
        assert change.new_type is not None
        rendered = render_type(flavor, change.new_type)
        rest = [m for m, k in zip(union, kinds, strict=True) if k not in ("null", "undefined")]
        if len(rest) == 1:
            return [(*text.node_span(rest[0]), rendered)]
        tail = [
            text.seg(text.node_span(m))
            for m, k in zip(union, kinds, strict=True)
            if k in ("null", "undefined")
        ]
        return [(*text.node_span(ty), " | ".join([rendered, *tail]))]

    # set
    question = next((c for c in prop.children if c.type == "?"), None)
    has_null, has_undefined = "null" in kinds, "undefined" in kinds
    optional, nullable = _new_flags(change, question is not None or has_undefined, has_null)
    splices: list[_Splice] = []
    if optional and question is None and not has_undefined:
        at = text.node_span(prop.child_by_field_name("name"))[1]
        splices.append((at, at, "?"))
    if not optional and question is not None:
        splices.append((*text.node_span(question), ""))
    kept = [
        m
        for m, k in zip(union, kinds, strict=True)
        if not (k == "null" and not nullable) and not (k == "undefined" and not optional)
    ]
    parts = [text.seg(text.node_span(m)) for m in kept]
    if nullable and not has_null:
        if ty.type in _TS_UNWRAPPABLE:
            raise _Manual(
                "its type cannot take '| null' without parentheses, which is edited by hand"
            )
        parts.append("null")
    if parts != [text.seg(text.node_span(m)) for m in union]:
        if not kept:
            raise _Manual("its type holds nothing besides null and undefined")
        splices.append((*text.node_span(ty), " | ".join(parts)))
    return splices


def _ts_key(text: _Text, node: Any) -> str | None:
    """A property key as the extractor names it: quotes dropped."""
    if node is None:
        return None
    raw = text.seg(text.node_span(node))
    if node.type == "string":
        return raw[1:-1] or None
    if node.type in ("property_identifier", "identifier", "number"):
        return raw
    return None


# ---------------------------------------------------------------- Zod


def _zod_chain(
    value: Any, tx: Any, receivers: frozenset[str]
) -> tuple[Any, list[tuple[str, Any, Any]]] | None:
    """(base node, modifiers innermost first as (name, '.' token, call)) for
    a Zod field expression, or None when it is not a plain chain."""
    mods: list[tuple[str, Any, Any]] = []
    cur = value
    while True:
        if cur.type == "identifier":
            break
        if cur.type != "call_expression":
            return None
        fn = cur.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return None
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        if prop is None or obj is None:
            return None
        if zod_is_receiver(obj, tx, receivers):
            break
        dot = next((c for c in fn.children if c.type == "."), None)
        if dot is None:
            return None
        mods.append((tx(prop), dot, cur))
        cur = obj
    return cur, list(reversed(mods))


def _zod_receiver_name(base: Any, tx: Any) -> str | None:
    if base.type != "call_expression":
        return None
    obj = base.child_by_field_name("function").child_by_field_name("object")
    while obj is not None and obj.type == "member_expression":
        obj = obj.child_by_field_name("object")
    return tx(obj) if obj is not None and obj.type == "identifier" else None


def _zod_presence(optional: bool, nullable: bool) -> str:
    if optional and nullable:
        return ".nullish()"
    if optional:
        return ".optional()"
    return ".nullable()" if nullable else ""


def _zod_splices(
    text: _Text,
    file: str,
    decl_start: int,
    decl_end: int,
    field: SchemaField | None,
    action: str,
    change: PlannedChange,
    declared: Sequence[ExtractedField],
) -> list[_Splice]:
    tree, _ = _ts_tree(text, file, ("javascript", "typescript", "tsx"))

    def tx(n: Any) -> str:
        return text.data[n.start_byte : n.end_byte].decode("utf-8")

    imports = _ts_collect_imports(tree.root_node, text.data, Path(file).parent.parts)
    receivers = frozenset(
        {"z", *(k for k, v in imports.items() if v == "zod" or v.startswith("zod/"))}
    )
    declarators = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "variable_declarator":
            declarators.append(n)
        stack.extend(n.children)
    names = {
        tx(d.child_by_field_name("name")): True
        for d in declarators
        if d.child_by_field_name("name")
    }
    objects = []
    for d in declarators:
        value = d.child_by_field_name("value")
        if value is None:
            continue
        # The symbol is the declarator, or the call itself when the object is
        # chained with a callback (`.refine(v => ...)`).
        if decl_start not in (d.start_point[0] + 1, value.start_point[0] + 1):
            continue
        found = _zod_object_decl(value, tx, receivers, names)
        if found is None:
            continue
        obj = found[1]
        if decl_start <= obj.start_point[0] + 1 and obj.end_point[0] + 1 <= decl_end:
            objects.append(obj)
    if len(objects) != 1:
        raise _Manual(f"no single Zod object spans lines {decl_start}-{decl_end}")
    obj = objects[0]
    members = _ts_members(text, obj, _ZOD_MEMBERS)

    def receiver() -> str:
        for n, _ in members:
            value = n.child_by_field_name("value") if n.type == "pair" else None
            chain = _zod_chain(value, tx, receivers) if value is not None else None
            name = _zod_receiver_name(chain[0], tx) if chain else None
            if name:
                return name
        return "z"

    def rendered(neutral: str) -> str:
        return receiver() + render_type("zod", neutral)[1:]

    if action == "add":
        assert change.new_type is not None
        if not members:
            raise _Manual("the object has no property to place the new field after")
        name = render_name(change.field, _side_style("zod", declared, None))
        member = (
            f"{_quoted_name(name, name)}: {rendered(change.new_type)}"
            f"{_zod_presence(bool(change.optional), bool(change.nullable))}"
        )
        return _list_insert(text, members[-1][1], _ts_closing(text, obj), member, ",")

    assert field is not None
    idx = next(
        (
            i
            for i, (n, _) in enumerate(members)
            if n.start_point[0] + 1 == field.line
            and (
                (n.type == "pair" and _ts_key(text, n.child_by_field_name("key")) == field.name)
                or (n.type == "shorthand_property_identifier" and tx(n) == field.name)
            )
        ),
        None,
    )
    if idx is None:
        raise _Manual(f"field '{field.name}' is not at its indexed line {field.line}")
    node = members[idx][0]

    if action == "remove":
        return _list_remove(text, [m for _, m in members], idx, ("//", "/*"))
    if node.type != "pair":
        raise _Manual(f"field '{field.name}' is a shorthand property, which is edited by hand")

    if action == "rename":
        assert change.new_name is not None
        key = node.child_by_field_name("key")
        name = render_name(change.new_name, _side_style("zod", declared, field.name))
        return [(*text.node_span(key), _quoted_name(name, text.seg(text.node_span(key))))]

    value = node.child_by_field_name("value")
    chain = _zod_chain(value, tx, receivers)
    if chain is None:
        raise _Manual(f"field '{field.name}' is not a plain Zod chain")
    base, mods = chain
    mod_names = [name for name, _, _ in mods]
    typed = sorted(set(mod_names) & _ZOD_TYPED_ARGS)
    if typed:
        raise _Manual(
            f"its .{typed[0]}() is written for its current type, so the change is made by hand"
        )

    def mod_span(dot: Any, call: Any) -> tuple[int, int]:
        return text.char(dot.start_byte), text.char(call.end_byte)

    if action == "retype":
        assert change.new_type is not None
        kept = "".join(
            text.seg(mod_span(dot, call)) for name, dot, call in mods if name in _ZOD_RETYPE_KEEP
        )
        name = _zod_receiver_name(base, tx) or receiver()
        new = name + render_type("zod", change.new_type)[1:] + kept
        return [(*text.node_span(value), new)]

    # set
    current = zod_type(value, tx, receivers)
    optional, nullable = _new_flags(change, current.optional, current.nullable)
    presence = [(i, dot, call) for i, (name, dot, call) in enumerate(mods) if name in _ZOD_PRESENCE]
    suffix = _zod_presence(optional, nullable)
    if not presence:
        end = text.node_span(value)[1]
        return [(end, end, suffix)]
    if any(name in _ZOD_RESHAPE for name, _, _ in mods[presence[0][0] + 1 :]):
        raise _Manual("a modifier after its .optional()/.nullable() reshapes the field")
    splices: list[_Splice] = [(*mod_span(presence[0][1], presence[0][2]), suffix)]
    splices.extend((*mod_span(dot, call), "") for _, dot, call in presence[1:])
    return splices


# ---------------------------------------------------------------- SQL


@dataclass(frozen=True)
class _SqlElement:
    toks: list[_Tok]
    member: _Member
    is_column: bool


def _sql_type_toks(el: list[_Tok]) -> tuple[list[_Tok], list[_Tok]]:
    """(type tokens, the rest) of a column element, as sql_schema splits them."""
    depth = 0
    k = 1
    while k < len(el):
        t = el[k]
        if depth == 0 and t.upper in _TYPE_STOP:
            break
        depth += (t.text == "(") - (t.text == ")")
        k += 1
    return el[1:k], el[k:]


def _sql_splices(
    text: _Text,
    decl_start: int,
    decl_end: int,
    field: SchemaField | None,
    action: str,
    change: PlannedChange,
) -> tuple[list[_Splice], str]:
    # (statement, index of the table name, index of the '(' after it)
    table: tuple[list[_Tok], int, int] | None = None
    for st in _split_statements(_tokenize(text.source)):
        if (
            st[0].upper != "CREATE"
            or text.line_of(st[0].start) != decl_start
            or text.line_of(st[-1].end - 1) != decl_end
        ):
            continue
        i = 1
        while i < len(st) and st[i].upper in _TABLE_PREFIXES:
            i += 1
        if i >= len(st) or st[i].upper != "TABLE":
            continue
        i += 1
        if [t.upper for t in st[i : i + 3]] == ["IF", "NOT", "EXISTS"]:
            i += 3
        end = _qualified_name_end(st, i)
        if end == i or end >= len(st) or st[end].text != "(":
            continue
        table = (st, i, end)
        break
    if table is None:
        raise _Manual(f"no CREATE TABLE spans lines {decl_start}-{decl_end}")
    st, name_at, open_at = table
    depth = 0
    close_at = None
    for k in range(open_at, len(st)):
        depth += (st[k].text == "(") - (st[k].text == ")")
        if depth == 0:
            close_at = k
            break
    if close_at is None:
        raise _Manual("the table's column list does not close")
    table_name = text.source[st[name_at].start : st[open_at - 1].end]

    elements: list[_SqlElement] = []
    cur: list[_Tok] = []
    depth = 0
    for t in st[open_at + 1 : close_at]:
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
        elif t.text == "," and depth == 0:
            if cur:
                elements.append(_sql_element(cur, t))
            cur = []
            continue
        cur.append(t)
    if cur:
        elements.append(_sql_element(cur, None))
    columns = [e for e in elements if e.is_column]
    table_pk: list[str] = []
    for e in elements:
        if e.is_column:
            continue
        words = _depth0(e.toks)
        for k, w in enumerate(words[:-1]):
            if w.upper == "PRIMARY" and words[k + 1].upper == "KEY":
                table_pk = _paren_names(e.toks, e.toks.index(words[k + 1]))

    if action == "add":
        assert change.new_type is not None
        if not columns:
            raise _Manual("the table has no column to place the new one after")
        anchor = columns[-1]
        name = render_name(change.field, "snake")
        heads = [c.toks[0] for c in columns]
        if all(h.kind == "qident" for h in heads):
            name = heads[-1].text[0] + name + heads[-1].text[0]
        sql_type = render_type(_SQL_FLAVOR, change.new_type)
        not_null = " NOT NULL" if not change.nullable else ""
        pos = elements.index(anchor)
        next_start = (
            elements[pos + 1].member.start if pos + 1 < len(elements) else st[close_at].start
        )
        splices = _list_insert(text, anchor.member, next_start, f"{name} {sql_type}{not_null}", ",")
        hint = f"ALTER TABLE {table_name} ADD COLUMN {name} {sql_type}{not_null};"
        return splices, hint + (_SQL_NOT_NULL_HINT if not_null else "")

    assert field is not None
    idx = next(
        (
            i
            for i, e in enumerate(elements)
            if e.is_column
            and _unquote(e.toks[0]) == field.name
            and text.line_of(e.toks[0].start) == field.line
        ),
        None,
    )
    if idx is None:
        raise _Manual(f"column '{field.name}' is not at its indexed line {field.line}")
    col = elements[idx]
    head = col.toks[0]
    type_toks, rest = _sql_type_toks(col.toks)
    if action in ("remove", "rename"):
        folded = field.name.casefold()
        for e in elements:
            if e is col:
                continue
            names = e.toks[1:] if e.is_column else e.toks
            if any(_is_name(t) and _unquote(t).casefold() == folded for t in names):
                raise _Manual(
                    f"another element of the table names '{field.name}', so it changes by hand"
                )

    if action == "remove":
        splices = _list_remove(text, [e.member for e in elements], idx, ("--", "/*"))
        return splices, f"ALTER TABLE {table_name} DROP COLUMN {head.text};"

    if action == "rename":
        assert change.new_name is not None
        name = render_name(change.new_name, "snake")
        if head.kind == "qident":
            name = head.text[0] + name + head.text[0]
        return [(head.start, head.end, name)], (
            f"ALTER TABLE {table_name} RENAME COLUMN {head.text} TO {name};"
        )

    if action == "retype":
        assert change.new_type is not None
        sql_type = render_type(_SQL_FLAVOR, change.new_type)
        if type_toks:
            splice: _Splice = (type_toks[0].start, type_toks[-1].end, sql_type)
        else:
            splice = (head.end, head.end, f" {sql_type}")
        return [splice], f"ALTER TABLE {table_name} ALTER COLUMN {head.text} TYPE {sql_type};"

    # set: a column's nullability. Its "optional" (the database fills it in)
    # is never edited.
    words = _depth0(rest)
    not_null = next(
        (
            k
            for k in range(len(words) - 1)
            if words[k].upper == "NOT" and words[k + 1].upper == "NULL"
        ),
        None,
    )
    primary = (
        any(a.upper == "PRIMARY" and b.upper == "KEY" for a, b in itertools.pairwise(words))
        or field.name in table_pk
    )
    nullable = field.nullable if change.nullable is None else change.nullable
    if nullable == field.nullable:
        return [], ""
    if nullable:
        if primary:
            raise _Manual("a primary key column is never nullable")
        if not_null is None:
            raise _Manual("its NOT NULL cannot be found")
        start = words[not_null].start
        while start > 0 and text.source[start - 1] in " \t":
            start -= 1
        splices = [(start, words[not_null + 1].end, "")]
        return splices, f"ALTER TABLE {table_name} ALTER COLUMN {head.text} DROP NOT NULL;"
    explicit = next((w for w in words if w.upper == "NULL"), None)
    if explicit is not None:
        splices = [(explicit.start, explicit.end, "NOT NULL")]
    else:
        at = type_toks[-1].end if type_toks else head.end
        splices = [(at, at, " NOT NULL")]
    return splices, f"ALTER TABLE {table_name} ALTER COLUMN {head.text} SET NOT NULL;"


def _sql_element(toks: list[_Tok], comma: _Tok | None) -> _SqlElement:
    head = toks[0]
    is_column = (
        _is_name(head) and head.upper not in _TABLE_CONSTRAINTS and not _is_index_element(toks)
    )
    member = _Member(
        toks[0].start,
        toks[-1].end,
        comma.end if comma is not None else None,
        "," if comma is not None else None,
    )
    return _SqlElement(toks, member, is_column)
