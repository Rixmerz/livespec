"""Static resolution of Python web-route context: framework and blueprint prefix.

``find_endpoints`` knows a handler's decorator (``@bp.route('/x')``) but not what
``bp`` is. This module answers two questions from the project's own source, with
no execution:

* which framework owns the receiver (``Blueprint``/``Flask`` -> flask,
  ``APIRouter``/``FastAPI`` -> fastapi), found by following ``from m import bp``
  to the assignment ``bp = Blueprint(...)``;
* which URL prefix Flask mounts a blueprint under: ``Blueprint(url_prefix=...)``
  or the ``url_prefix=`` of ``X.register_blueprint(bp, ...)`` (the registration
  wins, as in Flask).

When the answer is not statically certain (the blueprint is registered under two
different prefixes, the prefix is not a string literal, it is nested in another
blueprint) the prefix is reported as unknown and the path is left as written.
Pure: the caller supplies the file list and a reader.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_FLASK_CTORS = frozenset({"Blueprint", "Flask"})
_FASTAPI_CTORS = frozenset({"APIRouter", "FastAPI"})
_MAX_HOPS = 6
_UNKNOWN = object()


@dataclass(frozen=True, slots=True)
class RouterDef:
    file: str
    var: str
    ctor: str
    prefix: object  # str | None | _UNKNOWN (Blueprint url_prefix=)

    @property
    def framework(self) -> str:
        return "flask" if self.ctor in _FLASK_CTORS else "fastapi"


@dataclass(slots=True)
class _Module:
    defs: dict[str, RouterDef]
    imports: dict[str, tuple[str | None, str]]  # alias -> (module file, original name)
    roots: set[str]  # imported top-level packages
    tree: ast.AST


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ctor_name(call: ast.Call) -> str | None:
    fn = call.func
    name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
    if name in _FLASK_CTORS or name in _FASTAPI_CTORS:
        return name
    return None


def _kw_prefix(call: ast.Call, kwarg: str) -> object:
    for kw in call.keywords:
        if kw.arg == kwarg:
            lit = _const_str(kw.value)
            return lit if lit is not None else _UNKNOWN
    return None


class PythonRouteContext:
    """Lazy per-project index of routers, their imports and their registrations."""

    def __init__(self, files: Iterable[str], read: Callable[[str], str]) -> None:
        self._files = sorted(f for f in files if f.endswith(".py"))
        self._fileset = set(self._files)
        self._read = read
        self._modules: dict[str, _Module | None] = {}
        self._registrations: dict[tuple[str, str], list[object]] | None = None

    # -- module resolution -------------------------------------------------
    def _module_file(self, dotted: str) -> str | None:
        if not dotted:
            return None
        rel = dotted.replace(".", "/")
        for cand in (f"{rel}.py", f"{rel}/__init__.py"):
            if cand in self._fileset:
                return cand
        hits = [
            f for f in self._files if f.endswith(f"/{rel}.py") or f.endswith(f"/{rel}/__init__.py")
        ]
        return hits[0] if len(hits) == 1 else None

    def _import_target(self, file: str, node: ast.ImportFrom) -> str | None:
        mod = node.module or ""
        if node.level:
            base = file.split("/")[:-1]
            up = node.level - 1
            if up > len(base):
                return None
            base = base[: len(base) - up]
            dotted = ".".join([*base, *([mod] if mod else [])])
            return self._module_file(dotted) if dotted else None
        return self._module_file(mod)

    def _load(self, file: str) -> _Module | None:
        if file in self._modules:
            return self._modules[file]
        mod: _Module | None = None
        try:
            tree = ast.parse(self._read(file))
        except (SyntaxError, ValueError, OSError):
            tree = None
        if tree is not None:
            mod = _Module({}, {}, set(), tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        mod.roots.add(a.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module and not node.level:
                        mod.roots.add(node.module.split(".")[0])
                    target = self._import_target(file, node)
                    for a in node.names:
                        if a.name != "*":
                            mod.imports[a.asname or a.name] = (target, a.name)
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    ctor = _ctor_name(node.value)
                    if ctor is None:
                        continue
                    prefix = _kw_prefix(node.value, "url_prefix") if ctor == "Blueprint" else None
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            mod.defs[t.id] = RouterDef(file, t.id, ctor, prefix)
        self._modules[file] = mod
        return mod

    def resolve(self, file: str, name: str, _hops: int = 0) -> RouterDef | None:
        """The router a bare name denotes in ``file`` (following imports), or None."""
        if _hops > _MAX_HOPS:
            return None
        mod = self._load(file)
        if mod is None:
            return None
        if name in mod.defs:
            return mod.defs[name]
        target = mod.imports.get(name)
        if target and target[0]:
            return self.resolve(target[0], target[1], _hops + 1)
        return None

    # -- registrations -----------------------------------------------------
    def _all_registrations(self) -> dict[tuple[str, str], list[object]]:
        if self._registrations is not None:
            return self._registrations
        regs: dict[tuple[str, str], list[object]] = {}
        for file in self._files:
            try:
                src = self._read(file)
            except OSError:
                continue
            if "register_blueprint" not in src:
                continue
            mod = self._load(file)
            if mod is None:
                continue
            for node in ast.walk(mod.tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register_blueprint"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                ):
                    continue
                bp = self.resolve(file, node.args[0].id)
                if bp is None or bp.ctor != "Blueprint":
                    continue
                prefix: object = _kw_prefix(node, "url_prefix")
                recv = node.func.value
                if isinstance(recv, ast.Name):
                    parent = self.resolve(file, recv.id)
                    if parent is not None and parent.ctor == "Blueprint":
                        prefix = _UNKNOWN  # nested blueprint: parent prefix unknown here
                regs.setdefault((bp.file, bp.var), []).append(prefix)
        self._registrations = regs
        return regs

    def blueprint_prefix(self, bp: RouterDef) -> str | None:
        """The URL prefix ``bp`` is mounted under, or None when unknown/absent."""
        if bp.ctor != "Blueprint":
            return None
        regs = self._all_registrations().get((bp.file, bp.var), [])
        if not regs:
            return bp.prefix if isinstance(bp.prefix, str) else None
        # Flask: the registration's url_prefix overrides the blueprint's own.
        effective = {bp.prefix if r is None else r for r in regs}
        if len(effective) != 1:
            return None
        only = next(iter(effective))
        return only if isinstance(only, str) else None

    # -- public: one route -------------------------------------------------
    def framework_of(self, file: str, receiver: str | None) -> str | None:
        if receiver:
            router = self.resolve(file, receiver)
            if router is not None:
                return router.framework
        mod = self._load(file)
        if mod is None:
            return None
        flask = "flask" in mod.roots
        fast = bool(mod.roots & {"fastapi", "starlette"})
        if flask != fast:
            return "flask" if flask else "fastapi"
        return None

    def prefixed_path(self, file: str, receiver: str | None, path: str) -> tuple[str, str | None]:
        """``(path, applied_prefix)``; the path is untouched when no prefix is known."""
        if not receiver:
            return path, None
        router = self.resolve(file, receiver)
        if router is None:
            return path, None
        prefix = self.blueprint_prefix(router)
        if not prefix or not path.startswith("/"):
            return path, None
        return prefix.rstrip("/") + path, prefix
