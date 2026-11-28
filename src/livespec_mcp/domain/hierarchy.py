"""Class hierarchy over the index: which method does ``super().m()`` reach, and
which subclasses override ``C.m``.

Everything here is derived from rows the indexer already wrote: the `inherits`
edges between classes, the order of each class's `inherit` refs (its base
list), and the `parent_symbol_id` that makes a method a member of its class.
Nothing is parsed again, so the answer is exactly as good as the `inherits`
edges: a base the resolver could not pin to ONE project class (weight below
``MIN_BASE_WEIGHT``: an ambiguous name, a library class) is not a base here,
and a lookup that needs it finds nothing rather than guessing.

Method resolution order is **left-to-right depth-first** over the declared
bases (not C3): a class's own definition first, then each base's whole
ancestry in the order the bases are written. It agrees with C3 for single
inheritance and for the usual mixin layouts; it can differ from Python's real
MRO for a diamond where a shared ancestor also defines the method. Cycles
(a mis-resolved base) stop at the first revisit.

No MCP and no tools import: `indexer` (during resolution) and the tools both
build one.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

#: Class kinds whose members can override one another. Mirrors
#: `indexer._TYPE_LIKE_KINDS` (kept here so this module imports nothing).
CLASS_KINDS: frozenset[str] = frozenset(
    {"class", "interface", "type_alias", "enum", "struct", "trait"}
)

#: An `inherits` edge below this weight is an ambiguous fan-out, not a base.
MIN_BASE_WEIGHT = 0.6

#: How many subclass generations an override search walks.
MAX_OVERRIDE_DEPTH = 25


@dataclass(frozen=True, slots=True)
class Overrider:
    """A method that overrides another: its id, owning class id, how many
    inheritance steps below the overridden method's class it sits."""

    method_id: int
    class_id: int
    depth: int


class Hierarchy:
    def __init__(self, conn: sqlite3.Connection, project_ids: Iterable[int]) -> None:
        pids = sorted({int(p) for p in project_ids})
        marks = ",".join("?" * len(pids))
        self._class_ids: set[int] = set()
        self._parent: dict[int, int | None] = {}
        self._kind: dict[int, str] = {}
        self._name: dict[int, str] = {}
        self._members: dict[tuple[int, str], list[int]] = {}
        for r in conn.execute(
            f"""SELECT s.id, s.name, s.kind, s.parent_symbol_id
                FROM symbol s JOIN file f ON f.id = s.file_id
                WHERE f.project_id IN ({marks}) AND s.kind IN
                  ('class','interface','type_alias','enum','struct','trait','method','function')
                ORDER BY s.id""",
            pids,
        ):
            sid = int(r[0])
            self._kind[sid] = r[2]
            self._name[sid] = r[1]
            self._parent[sid] = int(r[3]) if r[3] is not None else None
            if r[2] in CLASS_KINDS:
                self._class_ids.add(sid)
        for sid, parent in self._parent.items():
            if self._kind[sid] == "method" and parent in self._class_ids:
                self._members.setdefault((parent, self._name[sid]), []).append(sid)

        edges: dict[int, set[int]] = {}
        for r in conn.execute(
            f"""SELECT e.src_symbol_id, e.dst_symbol_id
                FROM symbol_edge e JOIN symbol s ON s.id = e.src_symbol_id
                JOIN file f ON f.id = s.file_id
                WHERE e.edge_type = 'inherits' AND e.weight >= ? AND f.project_id IN ({marks})""",
            [MIN_BASE_WEIGHT, *pids],
        ):
            if int(r[0]) in self._class_ids and int(r[1]) in self._class_ids:
                edges.setdefault(int(r[0]), set()).add(int(r[1]))
        # Declared order: the order the class's inherit refs were written in.
        ref_order: dict[int, list[str]] = {}
        for src, name in conn.execute(
            "SELECT src_symbol_id, target_name FROM symbol_ref WHERE ref_type='inherit' ORDER BY id"
        ):
            ref_order.setdefault(int(src), []).append(name)
        self._declared: dict[int, int] = {k: len(set(v)) for k, v in ref_order.items()}
        self._bases: dict[int, list[int]] = {}
        for src, dsts in edges.items():
            ordered: list[int] = []
            for name in ref_order.get(src, []):
                for d in sorted(dsts):
                    if self._name.get(d) == name and d not in ordered:
                        ordered.append(d)
            ordered.extend(d for d in sorted(dsts) if d not in ordered)
            self._bases[src] = ordered
        self._subs: dict[int, list[int]] = {}
        for src, bases in self._bases.items():
            for b in bases:
                self._subs.setdefault(b, []).append(src)

    # ----- structure ------------------------------------------------------

    def is_class(self, symbol_id: int) -> bool:
        return symbol_id in self._class_ids

    def bases(self, class_id: int) -> list[int]:
        return self._bases.get(class_id, [])

    def owner_class(self, symbol_id: int) -> int | None:
        """The class a symbol belongs to: itself when it is one, its parent for
        a method, and for a function nested in a method the method's class."""
        seen: set[int] = set()
        cur: int | None = symbol_id
        while cur is not None and cur not in seen:
            if cur in self._class_ids:
                return cur
            seen.add(cur)
            cur = self._parent.get(cur)
        return None

    def method_owner(self, method_id: int) -> int | None:
        parent = self._parent.get(method_id)
        return parent if parent in self._class_ids else None

    def name(self, symbol_id: int) -> str | None:
        return self._name.get(symbol_id)

    def own_method(self, class_id: int, name: str) -> int | None:
        found = self._members.get((class_id, name))
        return found[0] if found else None

    def is_complete(self, class_id: int) -> bool:
        """True when every base of *class_id*, transitively, is an indexed
        class the resolver pinned: nothing in its ancestry is a library class, a
        wildcard re-export or an ambiguous name. Only then does "no indexed
        base defines `m`" mean "nobody does"."""
        seen: set[int] = set()
        stack = [class_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if self._declared.get(cur, 0) != len(self._bases.get(cur, [])):
                return False
            stack.extend(self._bases.get(cur, []))
        return True

    # ----- lookups --------------------------------------------------------

    def find_in_bases(self, class_id: int, name: str) -> int | None:
        """The method *name* of the nearest base of *class_id* that defines it,
        left-to-right depth-first; None when no indexed base does."""
        visited = {class_id}
        stack = list(reversed(self.bases(class_id)))
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            found = self.own_method(cur, name)
            if found is not None:
                return found
            stack.extend(reversed(self.bases(cur)))
        return None

    def find_in_mro(self, class_id: int, name: str) -> int | None:
        """*name* on *class_id* itself, else on its nearest base."""
        own = self.own_method(class_id, name)
        return own if own is not None else self.find_in_bases(class_id, name)

    def overridden_method(self, method_id: int) -> int | None:
        """The base method *method_id* overrides, if an indexed base has one."""
        owner = self.method_owner(method_id)
        name = self._name.get(method_id)
        if owner is None or name is None:
            return None
        return self.find_in_bases(owner, name)

    def overriders(self, method_id: int) -> list[Overrider]:
        """Every method of a class with an `inherits` path to the method's
        class that defines the same name, nearest generations first."""
        owner = self.method_owner(method_id)
        name = self._name.get(method_id)
        if owner is None or name is None:
            return []
        out: list[Overrider] = []
        seen = {owner}
        queue: deque[tuple[int, int]] = deque((s, 1) for s in self._subs.get(owner, []))
        while queue:
            cls, depth = queue.popleft()
            if cls in seen or depth > MAX_OVERRIDE_DEPTH:
                continue
            seen.add(cls)
            m = self.own_method(cls, name)
            if m is not None:
                out.append(Overrider(m, cls, depth))
            queue.extend((s, depth + 1) for s in self._subs.get(cls, []))
        return out
