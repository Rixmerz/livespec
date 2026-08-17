"""Contract closure — the unit an agent actually needs to change one symbol.

Reading code by file is a human affordance. An agent asked to change
``charge_card`` does not need the file it lives in; it needs the body, the
*signatures* of what it calls, the *definitions* of the types in those
signatures, what it can raise, and which tests cover it. That set is the
contract closure, and it is what this module builds.

The point is not compression for its own sake. A well-factored repo is more
expensive for an agent to read than a badly-factored one — more files, more
jumps, more context burned — so structure and navigability pull against each
other. A closure removes that tension: the agent pays for the unit, not for
the layout.

Three properties this module treats as non-negotiable:

**A missing type is reported, never silently dropped.** A closure that omits
the definition of ``Money`` and does not say so is worse than no closure: the
agent writes against a type it never saw and the failure surfaces as a
hallucinated field access. `unresolved` is part of the payload.

**Not-a-project-type is not the same as could-not-resolve.** ``str``, ``Path``
and ``Iterator`` are absent from the index because they are not this project's
to define. Counting them as misses made the first measurement of this idea
report 43% resolution when the real project-type figure was 100%, which would
have condemned a working design.

**Degradation is ordered and disclosed.** Over budget, the farthest callees go
first — a call into another package is less likely to matter to the edit than
one in the same file — and the payload says how many went and why.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Rough token estimate. Callers gate on an order of magnitude ("does this fit
# in a prompt"), not on an exact count, and 4 chars/token errs high on code —
# the safe direction for a budget.
_CHARS_PER_TOKEN = 4

DEFAULT_TOKEN_BUDGET = 2000
MAX_TYPE_DEF_LINES = 14

# Names that are not this project's to define. Their absence from the index is
# correct, so counting them as unresolved would be a false alarm — see the
# module docstring. Deliberately conservative: anything not listed here and not
# in the index is reported as unresolved, because a false "missing" is cheap and
# a false "fine" is not.
_KNOWN_EXTERNAL: frozenset[str] = frozenset({
    # Python builtins and typing
    "str", "int", "float", "bool", "bytes", "bytearray", "complex", "object",
    "list", "dict", "set", "frozenset", "tuple", "type", "None", "NoneType",
    "Any", "Optional", "Union", "Literal", "Callable", "Iterable", "Iterator",
    "Sequence", "Mapping", "MutableMapping", "Generator", "AsyncGenerator",
    "Awaitable", "Coroutine", "TypeVar", "ClassVar", "Final", "Annotated",
    "Self", "Never", "NoReturn", "Protocol", "TypedDict", "NamedTuple",
    "Exception", "BaseException", "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "OSError", "IOError", "StopIteration",
    "NotImplementedError", "AttributeError", "ImportError", "AssertionError",
    # stdlib types that show up constantly in signatures
    "Path", "PurePath", "PurePosixPath", "datetime", "date", "time",
    "timedelta", "timezone", "Decimal", "Enum", "IntEnum", "StrEnum",
    "UUID", "Connection", "Cursor", "Thread", "Lock", "Queue", "Counter",
    "OrderedDict", "defaultdict", "deque", "Fraction", "Pattern", "Match",
    # TypeScript / JS
    "string", "number", "boolean", "unknown", "never", "void", "undefined",
    "null", "Promise", "Array", "Record", "Partial", "Required", "Readonly",
    "Pick", "Omit", "Map", "Set", "Date", "Error", "RegExp", "JSON", "Object",
    "React", "ReactNode", "ReactElement", "JSX", "Props",
    # Go / Rust / Java surface that appears in extracted signatures
    "error", "rune", "byte", "interface", "struct",
    "String", "Vec", "Option", "Result", "Box", "Arc", "Rc", "HashMap",
    "Integer", "Long", "Double", "Boolean", "List", "Void",
})

# A CapWords-ish token inside a signature. Deliberately not a type parser: the
# index stores signatures as text across nine languages, and a real parse per
# language buys precision this step does not need — a name that is not a type
# simply fails to resolve and lands in `unresolved`, where it is visible.
_TYPE_TOKEN = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")

# Heuristic across languages: `raise X`, `throw new X`, `panic(`. Labelled as
# heuristic in the payload rather than presented as an analysis.
_RAISES = re.compile(
    r"\braise\s+([A-Za-z_][\w.]*)"
    r"|\bthrow\s+new\s+([A-Za-z_][\w.]*)"
    r"|\bpanic\s*\(",
)

# An import binding, across the languages the index covers:
#   from x import A, B      import a.b as C      import {A, B} from 'x'
#   use x::A;               const {A} = require('x')
# Used only to answer "is this name something the project imports from
# elsewhere", never to resolve the type — see `_classify_unresolved`.
_IMPORT_LINE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\s+(?P<py>.+)"
    r"|import\s+(?P<plain>[^;'\"]+)"
    r"|use\s+(?P<rust>[\w:{}, ]+);)",
    re.M,
)
_IMPORT_NAME = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")

_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$")
_TEST_NAME = re.compile(r"^(test_|Test|it_|should_)")

_TYPE_KINDS = ("class", "interface", "type", "type_alias", "struct", "enum")


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


@dataclass
class Callee:
    qualified_name: str
    signature: str
    file_path: str
    distance: int = 0  # 0 same file, 1 same directory, 2 elsewhere

    def render(self) -> str:
        """One readable line per callee.

        The index stores signatures in whatever shape the language's extractor
        produced: Python functions arrive already carrying their own name
        (``authorize(amount: Money) -> bool``) while classes arrive as a whole
        declaration (``class DeclinedError(Exception)``). Concatenating the
        qualified name onto either produced ``gateway.authorizeauthorize(...)``
        and ``billing.DeclinedErrorclass DeclinedError(...)`` — unreadable, and
        exactly the kind of thing that only shows up when a human reads the
        rendered closure instead of asserting on the dict.
        """
        sig = (self.signature or "").strip()
        if not sig:
            return f"  {self.qualified_name}(…)"
        short = self.qualified_name.rsplit(".", 1)[-1]
        module = self.qualified_name.rsplit(".", 1)[0] if "." in self.qualified_name else ""
        if sig.startswith("("):
            sig = short + sig
        if sig.startswith(short):
            return f"  {module + '.' if module else ''}{sig}"
        return f"  {self.qualified_name} — {sig}"


@dataclass
class TypeDef:
    name: str
    qualified_name: str
    definition: str
    truncated: bool = False


@dataclass
class Closure:
    qualified_name: str
    kind: str
    signature: str
    file_path: str
    start_line: int
    end_line: int
    body: str
    calls: list[Callee] = field(default_factory=list)
    types: list[TypeDef] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)
    covered_by: list[str] = field(default_factory=list)
    dropped_calls: int = 0
    budget: int = DEFAULT_TOKEN_BUDGET

    # -- rendering ---------------------------------------------------------

    def render(self) -> str:
        """The closure as the text an agent reads. Also what the budget counts."""
        out = [f"# {self.qualified_name}  ({self.kind})"]
        if self.signature:
            out.append(f"signature: {self.signature}")
        out += ["", "## body", self.body]
        if self.calls:
            out += ["", "## calls (signatures only)"]
            out += [c.render() for c in self.calls]
        if self.dropped_calls:
            out += [
                "",
                f"## {self.dropped_calls} more call(s) omitted to fit the "
                f"{self.budget}-token budget — raise token_budget or lower depth",
            ]
        if self.types:
            out += ["", "## types"]
            for t in self.types:
                out.append(f"  {t.name}:")
                out += [f"    {ln}" for ln in t.definition.splitlines()]
                if t.truncated:
                    out.append("    … (definition truncated)")
        if self.unresolved:
            out += [
                "",
                "## types NOT RESOLVED — not in the index, read them before "
                "relying on their shape",
                "  " + ", ".join(self.unresolved),
            ]
        if self.raises:
            out += ["", "## raises (heuristic)", "  " + ", ".join(self.raises)]
        if self.covered_by:
            out += ["", "## covered by", *(f"  {t}" for t in self.covered_by)]
        elif not self.covered_by:
            out += ["", "## covered by", "  (no test in the index calls this)"]
        return "\n".join(out)

    def as_dict(self) -> dict[str, Any]:
        rendered = self.render()
        return {
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "signature": self.signature,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "body": self.body,
            "calls": [
                {"qualified_name": c.qualified_name, "signature": c.signature}
                for c in self.calls
            ],
            "types": [
                {
                    "name": t.name,
                    "qualified_name": t.qualified_name,
                    "definition": t.definition,
                    "truncated": t.truncated,
                }
                for t in self.types
            ],
            "unresolved_types": self.unresolved,
            "external_types": self.external,
            "raises": self.raises,
            "covered_by": self.covered_by,
            "rendered": rendered,
            "budget": {
                "limit": self.budget,
                "estimated_tokens": estimate_tokens(rendered),
                "degraded": self.dropped_calls > 0,
                "dropped_calls": self.dropped_calls,
            },
        }


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def _read_slice(root: Path, path: str, start: int, end: int) -> str:
    try:
        fp = root / path
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[max(start - 1, 0):min(end, len(lines))])


def _distance(a: str, b: str) -> int:
    if a == b:
        return 0
    return 1 if str(Path(a).parent) == str(Path(b).parent) else 2


def _is_test(path: str, name: str) -> bool:
    return bool(_TEST_PATH.search(path) or _TEST_NAME.match(name))


def _callees(conn: sqlite3.Connection, symbol_id: int, home: str) -> list[Callee]:
    rows = conn.execute(
        "SELECT s.qualified_name, s.signature, f.path "
        "FROM symbol_edge e "
        "JOIN symbol s ON s.id = e.dst_symbol_id "
        "JOIN file f ON f.id = s.file_id "
        "WHERE e.src_symbol_id = ? AND e.edge_type = 'calls'",
        (symbol_id,),
    ).fetchall()
    seen: set[str] = set()
    out: list[Callee] = []
    for r in rows:
        qn = r["qualified_name"]
        if qn in seen:
            continue
        seen.add(qn)
        out.append(
            Callee(
                qualified_name=qn,
                signature=r["signature"] or "",
                file_path=r["path"],
                distance=_distance(home, r["path"]),
            )
        )
    # Nearest first: the budget trims from the tail, and a call in the same
    # file is the one most likely to matter to the edit.
    out.sort(key=lambda c: (c.distance, c.qualified_name))
    return out


def _lookup_type(
    conn: sqlite3.Connection, project_ids: tuple[int, ...], name: str
) -> sqlite3.Row | None:
    placeholders = ",".join("?" * len(project_ids))
    kinds = ",".join("?" * len(_TYPE_KINDS))
    return conn.execute(
        f"SELECT s.qualified_name, s.start_line, s.end_line, f.path "
        f"FROM symbol s JOIN file f ON f.id = s.file_id "
        f"WHERE s.name = ? AND s.kind IN ({kinds}) "
        f"AND f.project_id IN ({placeholders}) "
        f"ORDER BY LENGTH(s.qualified_name) LIMIT 1",
        (name, *_TYPE_KINDS, *project_ids),
    ).fetchone()


def _imported_names(root: Path, files: set[str]) -> set[str]:
    """CapWords names the given files import from somewhere else.

    A name the index cannot resolve is one of two very different things, and
    conflating them makes the closure's own quality signal useless:

      `DiGraph` (networkx), `Response` (a web framework), `Table` (a PDF lib)
      are absent from the index because they belong to a dependency. That is
      correct and expected.

      A name that is neither defined in the project nor imported from outside
      it is a real gap — a type the closure promised and did not deliver.

    Measured on this repo, keeping them merged reported 23% type resolution
    when every single miss was a third-party import. A number that low would
    have condemned the design; the split is what makes it readable.

    Import lines are matched, not parsed. A parse per language buys precision
    this question does not need: the worst case of a missed import line is a
    third-party type reported as unresolved, which is the direction that shows
    up in the payload rather than hiding.
    """
    names: set[str] = set()
    for rel in files:
        try:
            # Imports live at the top of a file in every language here, and
            # reading whole files for this would make the closure O(repo).
            text = (root / rel).read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            continue
        for m in _IMPORT_LINE.finditer(text):
            clause = m.group("py") or m.group("plain") or m.group("rust") or ""
            names.update(_IMPORT_NAME.findall(clause))
    return names


def _extract_raises(body: str) -> list[str]:
    found: list[str] = []
    for m in _RAISES.finditer(body or ""):
        name = m.group(1) or m.group(2)
        found.append(name.split(".")[-1] if name else "panic")
    return sorted(set(found))


def _covering_tests(conn: sqlite3.Connection, symbol_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT s.qualified_name, s.name, f.path "
        "FROM symbol_edge e "
        "JOIN symbol s ON s.id = e.src_symbol_id "
        "JOIN file f ON f.id = s.file_id "
        "WHERE e.dst_symbol_id = ? AND e.edge_type = 'calls'",
        (symbol_id,),
    ).fetchall()
    return sorted({
        r["qualified_name"] for r in rows if _is_test(r["path"], r["name"])
    })


def build_closure(
    conn: sqlite3.Connection,
    project_ids: tuple[int, ...],
    symbol: sqlite3.Row,
    root: Path,
    *,
    depth: int = 1,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> Closure:
    """Assemble the contract closure for one symbol.

    ``depth`` bounds how far type resolution follows types named inside other
    type definitions. ``depth=0`` returns the body and the callee signatures
    with no type bodies at all — useful when the caller only needs the shape.
    """
    home = symbol["file_path"] if "file_path" in symbol.keys() else symbol["path"]
    body = _read_slice(root, home, symbol["start_line"], symbol["end_line"])

    cl = Closure(
        qualified_name=symbol["qualified_name"],
        kind=symbol["kind"],
        signature=symbol["signature"] or "",
        file_path=home,
        start_line=symbol["start_line"],
        end_line=symbol["end_line"],
        body=body,
        budget=token_budget,
    )
    cl.calls = _callees(conn, symbol["id"], home)
    cl.raises = _extract_raises(body)
    cl.covered_by = _covering_tests(conn, symbol["id"])

    if depth <= 0:
        _apply_budget(cl, token_budget)
        return cl

    # --- types named across the surface -----------------------------------
    surface = " ".join([cl.signature, *(c.signature for c in cl.calls)])
    pending = [
        t for t in dict.fromkeys(_TYPE_TOKEN.findall(surface))
        if t not in _KNOWN_EXTERNAL
    ]
    resolved: set[str] = set()

    for _level in range(depth):
        if not pending:
            break
        nxt: list[str] = []
        for name in pending:
            if name in resolved or name in cl.unresolved:
                continue
            row = _lookup_type(conn, project_ids, name)
            if row is None:
                cl.unresolved.append(name)
                continue
            resolved.add(name)
            text = _read_slice(root, row["path"], row["start_line"], row["end_line"])
            lines = text.splitlines()
            truncated = len(lines) > MAX_TYPE_DEF_LINES
            cl.types.append(
                TypeDef(
                    name=name,
                    qualified_name=row["qualified_name"],
                    definition="\n".join(lines[:MAX_TYPE_DEF_LINES]),
                    truncated=truncated,
                )
            )
            nxt += [
                t for t in _TYPE_TOKEN.findall(text)
                if t not in _KNOWN_EXTERNAL and t not in resolved
            ]
        pending = nxt

    # Split "belongs to a dependency" from "the closure owes you this one".
    external = {t for t in _TYPE_TOKEN.findall(surface) if t in _KNOWN_EXTERNAL}
    if cl.unresolved:
        involved = {cl.file_path} | {c.file_path for c in cl.calls}
        imported = _imported_names(root, involved)
        moved = [n for n in cl.unresolved if n in imported]
        external.update(moved)
        cl.unresolved = [n for n in cl.unresolved if n not in imported]

    cl.external = sorted(external)
    cl.types.sort(key=lambda t: t.name)
    cl.unresolved.sort()
    _apply_budget(cl, token_budget)
    return cl


def _apply_budget(cl: Closure, token_budget: int) -> None:
    """Trim to fit, last — the budget is about what actually gets delivered.

    Applied before type resolution (the first version of this) it measured a
    closure that did not exist yet: callees were dropped against a render with
    no type bodies in it, so the returned payload could sit well over budget
    while reporting `degraded: False`. A budget that is checked against
    something other than the answer is not a budget.

    Body, types and the unresolved list are never trimmed. The body is the
    thing that was asked for; the types are what stop the agent guessing; and
    a dropped `unresolved` entry turns a disclosed gap into a silent one.
    """
    while cl.calls and estimate_tokens(cl.render()) > token_budget:
        cl.calls.pop()
        cl.dropped_calls += 1
