"""Python module resolution for type refs and calls: which indexed file does an
import actually name?

The extractor stores, for a name a file imports, the module it came from
(`symbol_ref.scope_module`): absolute as written (`typing`, `click.types`) or
relative with its leading dots kept (`.blueprints`, `..core`). Matching that
text against the qualified names of every symbol (a bare module name as a
substring) is what let `import typing as t; t.Tuple` reach a project class
`Tuple` whenever some directory was merely called `typing`, and let
`from .blueprints import Blueprint` reach every `blueprints.Blueprint` in the
tree.

Here the import is resolved the way the interpreter would, against the indexed
files only:

* a relative import counts the dots from the importing file's own package;
* an absolute import is looked up by whole dotted module name under every
  directory that can be an import root (one with no ``__init__.py``: ``src``
  for ``src/click/core.py``); among several files claiming a name, the ones
  whose root encloses the importer win, and a name still claimed by two files
  resolves to nothing;
* the resolved module must then actually bind the name asked for (define it,
  or re-export it), so a directory that merely shares a stdlib name links
  nothing.

Everything is a pure function of the set of indexed Python files plus, for a
re-export (``from .core import Thing`` in ``pkg/__init__.py``), the source of
the re-exporting module — so the answer for one ref does not depend on what
else was indexed, or in which order. No DB, no MCP.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable
from dataclasses import dataclass

_STDLIB: frozenset[str] = frozenset(getattr(sys, "stdlib_module_names", ()))

# How many `from .x import Name` hops a re-export chain may take.
_MAX_REEXPORT_DEPTH = 6


@dataclass(frozen=True, slots=True)
class ModuleRef:
    """An import resolved to an indexed file: ``file_id`` plus the attribute
    path left over after the module (``from pkg import Outer; Outer.Inner``
    names module ``pkg`` with remainder ``("Outer",)``)."""

    file_id: int
    remainder: tuple[str, ...]


def qname_prefix(path: str) -> str:
    """The dotted prefix every symbol of the file at *path* carries — exactly
    the module name the extractor was handed (``src/click/core.py`` ->
    ``src.click.core``, ``pkg/__init__.py`` -> ``pkg.__init__``)."""
    stem = path[:-3] if path.endswith(".py") else path
    return ".".join(stem.split("/"))


def scope_prefix(path: str) -> str:
    """The dotted prefix of everything the module at *path* contains: its own
    qualified name, or its directory for a package's ``__init__.py``."""
    parts = path.split("/")
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1])
    return qname_prefix(path)


class PyModules:
    def __init__(
        self,
        paths: dict[int, str],
        read_source: Callable[[int], str | None] | None = None,
    ) -> None:
        """*paths*: file id -> project-relative posix path, Python files only.
        *read_source* returns a file's text (or None) for re-export lookups."""
        self._paths = paths
        self._read = read_source
        self._by_path = {p: fid for fid, p in paths.items()}
        self._by_name: dict[str, list[tuple[int, str]]] = {}
        for fid, path in paths.items():
            for root, name in self._roots_and_names(path):
                self._by_name.setdefault(name, []).append((fid, root))
        self._exports_cache: dict[int, dict[str, tuple[str, str]]] = {}
        # file id -> (modules it star-imports, its literal `__all__` or None)
        self._stars_cache: dict[int, tuple[list[str], frozenset[str] | None]] = {}

    def _roots_and_names(self, path: str) -> list[tuple[str, str]]:
        """Every (import root, module name under it) the file can be imported
        as: a root is a directory without ``__init__.py``."""
        parts = path.split("/")
        stem = parts[-1][:-3]
        out: list[tuple[str, str]] = []
        for r in range(len(parts) - 1, -1, -1):
            if r and "/".join([*parts[:r], "__init__.py"]) in self._by_path:
                continue
            names = parts[r:-1] if stem == "__init__" else [*parts[r:-1], stem]
            if names:
                out.append(("/".join(parts[:r]), ".".join(names)))
        return out

    def path_of(self, file_id: int) -> str:
        return self._paths[file_id]

    # ----- import -> file ------------------------------------------------

    def resolve_module(self, scope: str, importer: int) -> ModuleRef | None:
        """The indexed module *scope* names from *importer*, with whatever
        trails it; None when it names nothing indexed (stdlib, third party, an
        ambiguous name)."""
        if not scope:
            return None
        if scope.startswith("."):
            return self._resolve_relative(scope, importer)
        segs = scope.split(".")
        for k in range(len(segs), 0, -1):
            claims = self._by_name.get(".".join(segs[:k]))
            if not claims:
                continue
            home = self._paths[importer].rsplit("/", 1)[0] + "/"
            near = {f for f, root in claims if not root or home.startswith(root + "/")}
            if segs[0] in _STDLIB:
                # `typing`, `types`, `logging`: a project module of that name
                # shadows the standard library only where its root is on the
                # importer's path (the repo root, or a root enclosing the
                # importer) — `tests/typing/` does not make `typing` ours for
                # a file elsewhere.
                pick = sorted(near)
            else:
                pick = sorted(near or {f for f, _ in claims})
            if len(pick) != 1:
                return None
            return ModuleRef(pick[0], tuple(segs[k:]))
        return None

    def _resolve_relative(self, scope: str, importer: int) -> ModuleRef | None:
        dots = len(scope) - len(scope.lstrip("."))
        rest = [s for s in scope[dots:].split(".") if s]
        base = self._paths[importer].split("/")[:-1]
        if dots - 1 > len(base):
            return None
        base = base[: len(base) - (dots - 1)]
        for k in range(len(rest), -1, -1):
            head = "/".join([*base, *rest[:k]])
            cands = (
                [f"{head}.py", f"{head}/__init__.py"] if k else ["/".join([*base, "__init__.py"])]
            )
            for cand in cands:
                fid = self._by_path.get(cand)
                if fid is not None:
                    return ModuleRef(fid, tuple(rest[k:]))
        return None

    # ----- type refs ------------------------------------------------------

    def lookup_type(
        self,
        scope: str,
        name: str,
        importer: int,
        candidates: list[tuple[int, str, int]] | Callable[[str], list[tuple[int, str, int]]],
    ) -> tuple[tuple[int, str, int], bool] | None:
        """The ONE candidate named *name* that the import ``scope`` brings to
        *importer*, and whether it came through a re-export. None when the
        import is not a project module, the module does not bind the name, or
        the answer is not a single symbol. *candidates* may be a function from a
        name to its candidates, for a re-export that renames (`import a as b`
        along the way)."""
        return self._lookup(scope, name, importer, candidates, set(), 0)

    def _lookup(
        self,
        scope: str,
        name: str,
        importer: int,
        candidates: list[tuple[int, str, int]] | Callable[[str], list[tuple[int, str, int]]],
        seen: set[tuple[int, str]],
        depth: int,
    ) -> tuple[tuple[int, str, int], bool] | None:
        ref = self.resolve_module(scope, importer)
        if ref is None:
            return None
        owner = qname_prefix(self._paths[ref.file_id])
        wanted = ".".join([owner, *ref.remainder, name])
        named = candidates(name) if callable(candidates) else candidates
        hits = [c for c in named if c[2] == ref.file_id and c[1] == wanted]
        if len(hits) == 1:
            return hits[0], depth > 0
        if hits or ref.remainder or depth >= _MAX_REEXPORT_DEPTH:
            return None
        key = (ref.file_id, name)
        if key in seen:
            return None
        seen.add(key)
        bound = self._exports(ref.file_id).get(name)
        if bound is not None:
            next_scope, real = bound
            return self._lookup(next_scope, real, ref.file_id, candidates, seen, depth + 1)
        return self._lookup_star(name, ref.file_id, candidates, seen, depth)

    def _lookup_star(
        self,
        name: str,
        file_id: int,
        candidates: list[tuple[int, str, int]] | Callable[[str], list[tuple[int, str, int]]],
        seen: set[tuple[int, str]],
        depth: int,
    ) -> tuple[tuple[int, str, int], bool] | None:
        """`from m import *` in *file_id*: the one symbol *name* reaches through
        the modules it star-imports. A star import brings what the module's
        literal ``__all__`` lists, or without one every name not starting with
        an underscore; a name two star imports bring as different symbols
        resolves to nothing."""
        stars, _ = self._stars(file_id)
        found: dict[int, tuple[tuple[int, str, int], bool]] = {}
        for scope in stars:
            mod = self.resolve_module(scope, file_id)
            if mod is None or mod.remainder:
                continue
            _, exported = self._stars(mod.file_id)
            if exported is not None:
                if name not in exported:
                    continue
            elif name.startswith("_"):
                continue
            hit = self._lookup(scope, name, file_id, candidates, seen, depth + 1)
            if hit is not None:
                found[hit[0][0]] = (hit[0], True)
        return next(iter(found.values())) if len(found) == 1 else None

    def _stars(self, file_id: int) -> tuple[list[str], frozenset[str] | None]:
        """Modules a file star-imports (as written, in order) and its
        ``__all__`` when that is a literal list/tuple of strings (None when
        absent or built any other way)."""
        cached = self._stars_cache.get(file_id)
        if cached is not None:
            return cached
        stars: list[str] = []
        names: set[str] | None = None
        literal = True
        self._stars_cache[file_id] = (stars, None)
        source = self._read(file_id) if self._read else None
        tree: ast.Module | None = None
        if source:
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                tree = None
        for node in tree.body if tree else ():
            if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                stars.append("." * node.level + (node.module or ""))
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                    continue
                value = node.value
                if isinstance(value, (ast.List, ast.Tuple)) and all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
                ):
                    items = {str(e.value) for e in value.elts}  # type: ignore[attr-defined]
                    # `__all__ += [...]` extends what is already listed.
                    names = (names or set()) | items if isinstance(node, ast.AugAssign) else items
                else:
                    literal = False
        out = (stars, frozenset(names) if names is not None and literal else None)
        self._stars_cache[file_id] = out
        return out

    def _exports(self, file_id: int) -> dict[str, tuple[str, str]]:
        """Module-level ``from X import a as b`` of one file: local name ->
        (X as written, a). Only names bound by exactly one import and never
        rebound by an assignment, def or class qualify; a wildcard is followed by `_lookup_star`."""
        cached = self._exports_cache.get(file_id)
        if cached is not None:
            return cached
        out: dict[str, tuple[str, str]] = {}
        self._exports_cache[file_id] = out
        source = self._read(file_id) if self._read else None
        if not source:
            return out
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return out
        seen_once: dict[str, int] = {}
        stack: list[ast.stmt] = list(tree.body)
        while stack:
            node = stack.pop(0)
            if isinstance(node, ast.ImportFrom):
                mod = "." * node.level + (node.module or "")
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    seen_once[local] = seen_once.get(local, 0) + 1
                    out[local] = (mod, alias.name)
            elif isinstance(node, (ast.If, ast.Try)):
                stack = [
                    *node.body,
                    *getattr(node, "orelse", []),
                    *[s for h in getattr(node, "handlers", []) for s in h.body],
                    *getattr(node, "finalbody", []),
                    *stack,
                ]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                seen_once[node.name] = seen_once.get(node.name, 0) + 2
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        seen_once[t.id] = seen_once.get(t.id, 0) + 2
        for local, n in seen_once.items():
            if n != 1:
                out.pop(local, None)
        return out

    # ----- calls ----------------------------------------------------------

    def module_scope_prefix(self, scope: str, importer: int) -> tuple[str, bool, int] | None:
        """For a call's import scope: the qualified-name prefix every symbol of
        the module (or package) it names carries, and whether the scope goes
        past that module (``from pkg import client; client.get()``: `client` is
        a value the module holds, not a module), and the module's file id. None when the scope is not an
        indexed module."""
        ref = self.resolve_module(scope, importer)
        if ref is None:
            return None
        return scope_prefix(self._paths[ref.file_id]), bool(ref.remainder), ref.file_id
