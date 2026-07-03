"""Analysis tools.

P1.2 consolidation: `find_references` removed — use
`analyze_impact(target_type='symbol', target=qname, max_depth=1)` and read
the `impacted_callers` list (matches the old shape).
v0.3 P1.1 adds `git_diff_impact` for CI/PR-review use cases.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.domain.extractors import (
    HTTP_ROUTE_DECORATOR_LASTSEGS,
    infer_python_http_framework,
    parse_python_http_route,
    scan_hono_routes,
    ts_registered_callback_names,
)
from livespec_mcp.domain.graph import (
    GraphView,
    ancestors_within,
    descendants_within,
    load_graph,
    page_rank,
)
from livespec_mcp.state import AppState, get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

_INFRA_NAME_SUFFIXES = ("_state", "_settings", "_config", "_session")

_PAYLOAD_WARN_BYTES = 500 * 1024
_DEFAULT_META_BYTES = 400


def _payload_warning(
    total_count: int,
    *,
    limit: int,
    summary_only: bool,
    bytes_per_item: int = _DEFAULT_META_BYTES,
) -> str | None:
    """Pre-flight hint when an unpaginated result would be large."""
    if summary_only or total_count == 0:
        return None
    est_bytes = total_count * bytes_per_item
    if total_count <= limit and est_bytes <= _PAYLOAD_WARN_BYTES:
        return None
    kb = max(1, est_bytes // 1024)
    return (
        f"Estimated full payload ~{kb}KB ({total_count} items). "
        "Use summary_only=True for counts only, or paginate with limit/cursor."
    )


def _attach_payload_warning(
    payload: dict[str, Any],
    warning: str | None,
) -> dict[str, Any]:
    if warning:
        payload["payload_warning"] = warning
    return payload


def _grep_indexed_files_core(
    st: AppState,
    pattern: str,
    path_glob: str | None,
    kind: str | None,
    limit: int,
    cursor: int,
) -> dict[str, Any]:
    """Search indexed file contents on disk (substring or regex)."""
    pid = st.project_id
    sql = "SELECT path, language FROM file WHERE project_id=?"
    params: list[Any] = [pid]
    if kind:
        sql += " AND language=?"
        params.append(kind)
    file_rows = st.conn.execute(sql, params).fetchall()

    try:
        regex = re.compile(pattern)
        use_regex = True
    except re.error:
        regex = None
        use_regex = False
        needle = pattern

    matches: list[dict[str, Any]] = []
    ws = st.settings.workspace
    for row in file_rows:
        path = row["path"]
        if path_glob and not fnmatch.fnmatch(path, path_glob):
            continue
        fp = ws / path
        if not fp.is_file():
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            if use_regex:
                if regex is None or not regex.search(line):
                    continue
            elif needle not in line:
                continue
            matches.append({
                "file_path": path,
                "language": row["language"],
                "line": line_no,
                "text": line[:240],
            })

    total = len(matches)
    page = matches[cursor : cursor + limit]
    next_cursor = cursor + limit if cursor + limit < total else None
    return {
        "pattern": pattern,
        "matches": page,
        "count": total,
        "next_cursor": next_cursor,
    }

# v0.5 P1: framework decorator names that imply hidden callers (HTTP routers,
# CLI dispatchers, test frameworks, plugin systems, message brokers, MCP).
# We match on the LAST dotted segment so `app.route`, `router.get`,
# `bp.before_request`, `mcp.tool` all qualify. Keep this list short and well-
# known; users can opt out via include_infrastructure=True.
_ENTRY_POINT_DECORATOR_LASTSEG = frozenset({
    # HTTP verbs (Flask/FastAPI/Bottle/etc.)
    "route", "get", "post", "put", "delete", "patch", "head", "options",
    "api_route", "websocket",
    # Flask/FastAPI hooks
    "before_request", "after_request", "errorhandler", "teardown_appcontext",
    "before_first_request", "context_processor",
    # CLI dispatchers
    "command", "group",
    # Task brokers
    "task", "shared_task",
    # Test frameworks
    "fixture",
    # FastMCP / Anthropic agent SDK
    "tool", "resource", "prompt",
    # Plugin systems / event dispatch
    "hookimpl", "event", "event_handler", "handler", "listener",
    # Cron / schedules
    "cron", "schedule", "scheduled",
    # v0.13 P2: Spring Boot annotations (Java) — the DI container / web
    # layer instantiates and invokes these; zero in-project callers is
    # expected, not dead code.
    "getmapping", "postmapping", "putmapping", "deletemapping",
    "patchmapping", "requestmapping", "restcontroller", "controller",
    "service", "repository", "configuration", "bean", "autowired",
    "eventlistener", "postconstruct", "predestroy", "exceptionhandler",
    "kafkalistener", "rabbitlistener", "jmslistener",
    "springbootapplication",
    # v0.13 P2: Angular decorators (TS) — framework-instantiated, methods
    # reachable from HTML templates the indexer can't parse.
    "component", "injectable", "directive", "pipe", "ngmodule",
    "hostlistener",
})

# Per-framework decorator presets for `find_endpoints(framework=...)`.
_FRAMEWORK_DECORATOR_PATTERNS: dict[str, tuple[str, ...]] = {
    "flask": (
        "route", "get", "post", "put", "delete", "patch",
        "before_request", "after_request", "errorhandler",
    ),
    "fastapi": (
        "route", "get", "post", "put", "delete", "patch", "head", "options",
        "api_route", "websocket",
    ),
    "click": ("command", "group"),
    "pytest": ("fixture",),
    "fastmcp": ("tool", "resource", "prompt"),
    "celery": ("task", "shared_task"),
    "django": ("login_required", "permission_required", "staff_member_required"),
    # v0.13 P2: Java Spring Boot (annotations extracted since migration v8)
    "spring": (
        "GetMapping", "PostMapping", "PutMapping", "DeleteMapping",
        "PatchMapping", "RequestMapping", "RestController", "Controller",
        "ExceptionHandler", "EventListener", "Scheduled",
    ),
    # v0.13 P2: Angular (TS decorators extracted since migration v8)
    "angular": ("Component", "Injectable", "Directive", "Pipe", "NgModule"),
}

# v0.13 P2: Angular lifecycle hooks — invoked by the framework, never by
# in-project code. Protected when the parent class carries any Angular
# decorator.
_NG_LIFECYCLE_HOOKS = frozenset({
    "ngOnInit", "ngOnDestroy", "ngOnChanges", "ngDoCheck",
    "ngAfterViewInit", "ngAfterViewChecked",
    "ngAfterContentInit", "ngAfterContentChecked",
})

# Angular decorators whose classes are TEMPLATE-bound: any public method may
# be referenced from HTML the indexer can't parse (`(click)="save()"`), so
# every method of such a class is protected from dead-code flagging.
_NG_TEMPLATE_DECORATOR_LASTSEGS = frozenset({"component", "directive", "pipe"})
_NG_ANY_DECORATOR_LASTSEGS = _NG_TEMPLATE_DECORATOR_LASTSEGS | frozenset(
    {"injectable", "ngmodule"}
)


def _decorator_lastseg(name: str) -> str:
    """Return the last dotted segment of a decorator name, lowercase."""
    return name.rsplit(".", 1)[-1].lower()


def _has_entry_point_decorator(
    decorators_json: str | None,
    alias_lastsegs: frozenset[str] = frozenset(),
) -> bool:
    if not decorators_json:
        return False
    try:
        names = json.loads(decorators_json)
    except (json.JSONDecodeError, TypeError):
        return False
    return any(
        _decorator_lastseg(n) in _ENTRY_POINT_DECORATOR_LASTSEG
        or _decorator_lastseg(n) in alias_lastsegs
        for n in names
    )


def _decorator_matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    """True if `name` equals or has-as-last-segment any of `patterns`."""
    last = _decorator_lastseg(name)
    return last in {p.lower() for p in patterns}


_DJANGO_CBV_BASES = frozenset({
    # Generic class-based views
    "View", "TemplateView", "RedirectView", "ListView", "DetailView",
    "FormView", "CreateView", "UpdateView", "DeleteView",
    "BaseDetailView", "BaseListView", "BaseFormView", "BaseCreateView",
    "BaseUpdateView", "BaseDeleteView", "ProcessFormView",
    "ArchiveIndexView", "YearArchiveView", "MonthArchiveView",
    "WeekArchiveView", "DayArchiveView", "DateDetailView",
    # Auth views (django.contrib.auth.views)
    "LoginView", "LogoutView",
    "PasswordChangeView", "PasswordChangeDoneView",
    "PasswordResetView", "PasswordResetDoneView",
    "PasswordResetConfirmView", "PasswordResetCompleteView",
    # Auth mixins
    "LoginRequiredMixin", "PermissionRequiredMixin",
    "UserPassesTestMixin", "AccessMixin",
    # Middleware base
    "MiddlewareMixin",
    # Admin views
    "AutocompleteJsonView",
    # API view patterns from DRF (common-enough adjacent)
    "APIView", "ViewSet", "ModelViewSet", "GenericViewSet",
    "ReadOnlyModelViewSet",
})


def _django_cbv_base_from_signature(sig: str | None) -> str | None:
    """Return the matched Django CBV base class name found in `sig`, or None.

    `sig` is the class signature string emitted by the Python extractor —
    e.g. ``class LoginView(SuccessURLAllowedHostsMixin, FormView)``. We
    take the bases between the first ``(`` and last ``)``, split on
    commas, strip dotted-path prefixes, and return the first hit
    against `_DJANGO_CBV_BASES`. Returns None when the class has no
    bases or none match.
    """
    if not sig or not sig.startswith("class "):
        return None
    open_paren = sig.find("(")
    close_paren = sig.rfind(")")
    if open_paren < 0 or close_paren <= open_paren:
        return None
    bases_part = sig[open_paren + 1 : close_paren]
    if not bases_part.strip():
        return None
    for raw in bases_part.split(","):
        # Strip generic / subscript suffix (`View[T]`) and dotted prefix.
        token = raw.strip().split("[", 1)[0].split("=", 1)[0].strip()
        last = token.rsplit(".", 1)[-1]
        if last in _DJANGO_CBV_BASES:
            return last
    return None


def _collect_module_refs(node: ast.AST, into: set[str]) -> None:
    """Walk an AST node, collecting Name/Attribute identifiers, but PRUNE
    bodies of nested function/class defs so refs inside their scopes are
    NOT counted as module-level. Decorators, base classes, default-arg
    expressions, and class-level type-annotations *are* still walked.

    v0.9 P4: also captures the trailing segment of dotted-path string
    constants (`"app.apps.AdminConfig"` → `AdminConfig`). Django and
    other frameworks register implementations through string paths in
    settings (INSTALLED_APPS, MIDDLEWARE, PASSWORD_HASHERS, STORAGES,
    DEFAULT_AUTO_FIELD, default_app_config, ...). Without this the
    referenced classes look dead to the static analyzer.
    """
    if isinstance(node, ast.Name):
        into.add(node.id)
        return
    if isinstance(node, ast.Attribute):
        into.add(node.attr)
        if isinstance(node.value, ast.AST):
            _collect_module_refs(node.value, into)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        s = node.value
        if 1 < len(s) < 200 and "." in s and not s.startswith("."):
            tail = s.rsplit(".", 1)[-1]
            if tail.isidentifier():
                # Validate the full path looks like a dotted Python ref
                # (alphanumeric + underscores + dots only).
                core = s.replace(".", "").replace("_", "")
                if core.isalnum():
                    into.add(tail)
        return
    skip_field = None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        skip_field = "body"
    for field_name, child in ast.iter_fields(node):
        if field_name == skip_field:
            continue
        if isinstance(child, list):
            for item in child:
                if isinstance(item, ast.AST):
                    _collect_module_refs(item, into)
        elif isinstance(child, ast.AST):
            _collect_module_refs(child, into)


@lru_cache(maxsize=128)
def _used_nested_def_names(file_path_abs: str) -> frozenset[str]:
    """Names of function/class defs nested inside another function whose
    name is referenced within the enclosing function's body.

    The pattern this catches:

        def start_watcher():
            def _do_reindex():
                ...
            watcher = Watcher(on_reindex=_do_reindex)  # closure callback
            ...

    `_do_reindex` has zero call edges (the enclosing function passes it
    by reference, doesn't *call* it itself), so without this helper it
    looks dead. The reference inside `start_watcher`'s body is enough
    signal to mark it as live.

    Recursively walks every FunctionDef, AsyncFunctionDef, and ClassDef
    in the AST so deeply nested defs are also covered. Same caveats as
    `_module_level_referenced_names`: parse failure → empty set,
    Python-only.
    """
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return frozenset()

    used: set[str] = set()

    def _visit_scope(scope: ast.AST) -> None:
        # Find direct nested defs in this scope's body (not transitive —
        # those will be visited recursively).
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        nested_def_names: set[str] = set()
        for stmt in scope.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nested_def_names.add(stmt.name)

        if nested_def_names:
            # Collect Name/Attribute references in scope.body, EXCLUDING
            # the body of the nested defs themselves (those refs are
            # internal to the nested fn, not "uses" of it).
            referenced: set[str] = set()
            for stmt in scope.body:
                # Skip the nested def's own body recursion — but keep its
                # decorators, args, default values which may reference
                # sibling nested defs.
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for sub in ast.walk(stmt.args):
                        if isinstance(sub, ast.Name):
                            referenced.add(sub.id)
                    for dec in stmt.decorator_list:
                        for sub in ast.walk(dec):
                            if isinstance(sub, ast.Name):
                                referenced.add(sub.id)
                            elif isinstance(sub, ast.Attribute):
                                referenced.add(sub.attr)
                    if stmt.returns:
                        for sub in ast.walk(stmt.returns):
                            if isinstance(sub, ast.Name):
                                referenced.add(sub.id)
                    continue
                if isinstance(stmt, ast.ClassDef):
                    for base in stmt.bases:
                        for sub in ast.walk(base):
                            if isinstance(sub, ast.Name):
                                referenced.add(sub.id)
                    for dec in stmt.decorator_list:
                        for sub in ast.walk(dec):
                            if isinstance(sub, ast.Name):
                                referenced.add(sub.id)
                            elif isinstance(sub, ast.Attribute):
                                referenced.add(sub.attr)
                    continue
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Name):
                        referenced.add(sub.id)
                    elif isinstance(sub, ast.Attribute):
                        referenced.add(sub.attr)
            used.update(nested_def_names & referenced)

        # Recurse into all child scopes so nested-of-nested defs are found.
        for child in ast.walk(scope):
            if child is scope:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _visit_scope(child)

    # Top-level scopes: every direct child function/class in the module.
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _visit_scope(top)

    return frozenset(used)


@lru_cache(maxsize=128)
def _publicly_exported_names(file_path_abs: str) -> frozenset[str]:
    """Names a Python file exposes as part of its public surface.

    v0.10: handles two patterns the static caller graph doesn't see:

    1. ``from .x import Foo, Bar`` (any module-level ``ImportFrom``).
       Re-export from a package — the imported names are part of this
       package's public API. Critical for `__init__.py` files in
       library codebases: `django/contrib/auth/__init__.py` imports
       `Argon2PasswordHasher` etc. so it can be referenced as
       ``django.contrib.auth.Argon2PasswordHasher`` from user code.
       Both the original name and the ``as`` alias are recorded.
    2. ``__all__ = ['Foo', 'Bar']`` (module-level list/tuple of string
       constants assigned to ``__all__``). Each string is a public
       export; record the trailing identifier.

    Different from `_module_level_referenced_names`, which captures
    *all* top-level identifier references (Names/Attributes/Constants
    walked recursively). This function is narrower: only the two
    explicit "this is the public surface" patterns. The two sets are
    UNIONed in `find_dead_code` — both protect a candidate from being
    flagged dead.

    Cached. Non-Python files / parse failures return empty.
    """
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return frozenset()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                out.add(a.name)
                if a.asname:
                    out.add(a.asname)
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out.add(a.asname)
                else:
                    # `import x.y.z` exposes `x` (the bound name); take head
                    out.add(a.name.split(".", 1)[0])
            continue
        if isinstance(node, ast.Assign):
            # __all__ = ['Foo', 'Bar', ...]
            targets = node.targets
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and targets[0].id == "__all__"
                and isinstance(node.value, (ast.List, ast.Tuple))
            ):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        s = elt.value
                        tail = s.rsplit(".", 1)[-1]
                        if tail.isidentifier():
                            out.add(tail)
            continue
    return frozenset(out)


# v0.11 P3: registration-verb set. Conservative — false-skip risk (hiding
# real dead code) is worse than false-flag risk. Verbs here unambiguously
# mean "hand this callable/class to a framework for later invocation".
# Excluded (too broad): `add`, `set`, `put`, `push`, `bind`, `attach`,
# `register_converter` (SQLite — registers a TYPE converter, not a callable
# in the Django sense), `signal` (too generic). Kept tight on purpose.
_REGISTRATION_VERBS: frozenset[str] = frozenset({
    "register",
    "register_lookup",
    "register_function",
    "register_view",
    "register_filter",
    "register_tag",
    "register_serializer",
    "register_admin",
    "connect",
    "add_handler",
    "subscribe",
    "add_middleware",
    "add_listener",
    "on",
    "use",
})


@lru_cache(maxsize=128)
def _runtime_registered_names(file_path_abs: str) -> frozenset[str]:
    """Names passed as arguments to known runtime-registration method calls.

    v0.11 P3: covers the pattern where a class or function is handed to a
    framework via a method call so the framework invokes it later, leaving
    zero in-project call edges. Examples:

    * ``Field.register_lookup(MyLookup)``   in ``apps.py:AppConfig.ready()``
    * ``pre_save.connect(my_handler)``       at module level
    * ``app.add_middleware(MyMiddleware)``   inside a setup function

    Walks ALL function/method/class bodies (not just module level), since
    registration calls frequently live inside ``AppConfig.ready()``, model
    class bodies, test fixtures, etc.

    Conservative rules to minimise false-skips:
    * Only attribute-call nodes whose attribute name is in
      ``_REGISTRATION_VERBS``.
    * Only positional ``ast.Name`` args (direct identifier references).
    * Only keyword values that are ``ast.Name`` nodes.
    * String args, lambda args, and complex expressions are ignored.

    Cached. Non-Python files / parse failures return empty frozenset.
    """
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return frozenset()

    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Must be an attribute call: x.register_lookup(...)
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in _REGISTRATION_VERBS:
            continue
        # Collect positional Name args.
        for arg in node.args:
            if isinstance(arg, ast.Name):
                out.add(arg.id)
        # Collect keyword Name values.
        for kw in node.keywords:
            if isinstance(kw.value, ast.Name):
                out.add(kw.value.id)

    return frozenset(out)


@lru_cache(maxsize=128)
def _entry_point_decorator_aliases(file_path_abs: str) -> frozenset[str]:
    """Alias names assigned to entry-point decorator factories, lowercased.

    v0.13 P0: the plugin-framework pattern
    ``agentic_tool = mcp.tool if cond else _noop_decorator`` hides the
    real decorator from `_has_entry_point_decorator` — the stored
    decorator name is the ALIAS (``agentic_tool``), whose last segment
    is not in `_ENTRY_POINT_DECORATOR_LASTSEG`. Surfaced as 22 false
    positives when force-reindexing livespec-mcp itself.

    Collects assignment targets whose value — directly or through either
    branch of a conditional expression — is a dotted name with an
    entry-point last segment (``mcp.tool``, ``app.route``, ...).
    Assignments anywhere in the file count: the pattern lives inside
    ``register()`` function bodies, not at module level.

    Cached. Non-Python files / parse failures return empty frozenset.
    """
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return frozenset()

    def _lastseg(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr.lower()
        if isinstance(node, ast.Name):
            return node.id.lower()
        return None

    def _hits(node: ast.AST) -> bool:
        if isinstance(node, ast.IfExp):
            return _hits(node.body) or _hits(node.orelse)
        seg = _lastseg(node)
        return seg is not None and seg in _ENTRY_POINT_DECORATOR_LASTSEG

    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _hits(node.value):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id.lower())
            # Both branches of the conditional are decorator machinery.
            # The non-winning branch (`_noop_decorator`) is typically
            # referenced ONLY inside this expression — without this it
            # shows up as dead.
            if isinstance(node.value, ast.IfExp):
                for branch in (node.value.body, node.value.orelse):
                    seg = _lastseg(branch)
                    if seg:
                        out.add(seg)
    return frozenset(out)


@lru_cache(maxsize=256)
def _ts_runtime_registered_names(file_path_abs: str, language: str) -> frozenset[str]:
    """TS/JS mirror of `_runtime_registered_names`: identifiers passed to
    registration-style calls (`app.get('/x', handler)`, `emitter.on(...)`)
    at any nesting level, module top-level included — the canonical Hono /
    Express pattern registers handlers outside any symbol body, so
    extract-time refs can't see it. Cached per file path."""
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    return ts_registered_callback_names(source, language)


_TS_SCOPE_NODE_TYPES = frozenset({
    "function_declaration", "generator_function_declaration",
    "function_expression", "arrow_function", "method_definition",
})
_TS_NESTED_DEF_TYPES = frozenset({
    "function_declaration", "generator_function_declaration", "class_declaration",
})
_RUST_SCOPE_NODE_TYPES = frozenset({"function_item", "closure_expression"})
_RUST_NESTED_DEF_TYPES = frozenset({"function_item"})


@lru_cache(maxsize=256)
def _treesitter_used_nested_def_names(file_path_abs: str, language: str) -> frozenset[str]:
    """TS/JS/TSX + Rust mirror of `_used_nested_def_names` (v0.14, closes
    the closure-capture gap open since v0.8): a function declared inside
    another function whose NAME is referenced in the parent's body —
    `new Watcher(onEvent)`, `let cb = on_event;` — is reachable as a
    callback even with zero call edges. Go is intentionally absent: it has
    no named nested functions (closures are anonymous), so the false
    positive can't occur. Same caveats as the Python version: direct
    children of the parent body only, parse failure → empty set."""
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    try:
        from livespec_mcp.domain.languages import get_parser

        tree = get_parser(language).parse(source.encode("utf-8"))
    except Exception:
        return frozenset()

    if language == "rust":
        scope_types, def_types = _RUST_SCOPE_NODE_TYPES, _RUST_NESTED_DEF_TYPES
    else:
        scope_types, def_types = _TS_SCOPE_NODE_TYPES, _TS_NESTED_DEF_TYPES
    ident_types = frozenset({"identifier", "shorthand_property_identifier"})

    used: set[str] = set()

    def _visit_scope(scope_node) -> None:
        body = scope_node.child_by_field_name("body")
        if body is None:
            return
        nested_names: set[str] = set()
        for child in body.named_children:
            if child.type in def_types:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    nested_names.add(name_node.text.decode("utf-8", "replace"))
        if not nested_names:
            return
        # References in the parent body, excluding the nested defs
        # themselves (refs inside a nested fn are internal, not "uses").
        referenced: set[str] = set()
        stack = [c for c in body.named_children if c.type not in def_types]
        while stack:
            node = stack.pop()
            if node.type in ident_types:
                referenced.add(node.text.decode("utf-8", "replace"))
            stack.extend(node.named_children)
        used.update(nested_names & referenced)

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in scope_types:
            _visit_scope(node)
        stack.extend(node.named_children)
    return frozenset(used)


@lru_cache(maxsize=128)
def _module_level_referenced_names(file_path_abs: str) -> frozenset[str]:
    """Names referenced at Python module top-level (outside any function /
    class body). Captures three patterns that fool the "zero callers ⇒
    dead code" heuristic:

      1. ``if __name__ == "__main__": main()`` → `main` is referenced.
      2. ``MIGRATIONS = [(1, "n", _m001_drop_dead_tables), ...]`` → the
         migration fns appear in a module-level list literal.
      3. ``mcp.add_middleware(AgentLogMiddleware())`` → the middleware
         class is referenced; its method hooks (`on_call_tool`, etc.) are
         entry points reached via duck-typing.

    Cached because find_dead_code may evaluate many candidates per file.
    Non-Python files return empty (these patterns are Python-specific;
    other-language extractor work lands later). On parse failure we
    return empty rather than raising — find_dead_code keeps working.
    """
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return frozenset()
    refs: set[str] = set()
    for top_node in tree.body:
        _collect_module_refs(top_node, refs)
    return frozenset(refs)


_FRAMEWORK_INNER_CLASS_NAMES = frozenset({
    # Django ORM model + form metaclass hook — reflected via ModelBase.
    "Meta",
    # Django migration unit — registered via MigrationLoader.
    "Migration",
})


def _is_implicit_entry_point(meta: dict) -> bool:
    """Stricter subset of `_is_infrastructure`: only the cases where a symbol
    has invisible callers (Python protocol dunders, FastMCP `register`, DI
    helpers). Excludes the tiny-wrapper rule because a 1-line wrapper that
    nobody calls IS a dead-code candidate."""
    name = meta.get("name") or ""
    qname = meta.get("qualified_name") or ""
    kind = meta.get("kind") or ""
    if name.startswith("__") and name.endswith("__"):
        return True
    if any(seg.startswith("__") and seg.endswith("__") for seg in qname.split(".")):
        return True
    if name == "register" and kind == "function":
        return True
    if kind in ("function", "method") and any(
        name.endswith(suf) for suf in _INFRA_NAME_SUFFIXES
    ):
        return True
    # v0.9 P4: framework inner-class hooks. Django's ModelBase / FormMeta
    # metaclass reads `class Meta:` reflectively; MigrationLoader does the
    # same with `class Migration:`. They have zero direct callers but are
    # never dead. Heuristic guard: parent segment starts with an uppercase
    # letter, signaling it's an outer class (PascalCase) rather than a
    # module path (lowercase). Avoids protecting a stray top-level
    # `class Meta:` in a normal module.
    if kind == "class" and name in _FRAMEWORK_INNER_CLASS_NAMES:
        parts = qname.split(".")
        if len(parts) >= 3 and parts[-2][:1].isupper():
            return True
    return False


_STRUCTURAL_NAME_FILE_THRESHOLD = 3

# v0.11 P0 (bug #18): top-level bundler/build output dirs. Symbols extracted
# from files under these paths are noise for "what's in this codebase" — they
# are generated artifacts, not source. Applied to top_symbols (project
# overview) and find_dead_code. Set, not regex, for cheap prefix checks.
_BUNDLER_OUTPUT_DIRS = (
    "_fresh/",
    "dist/",
    "build/",
    ".next/",
    "out/",
    "node_modules/",
    ".svelte-kit/",
    "target/",
    "__pycache__/",
    ".turbo/",
    ".vite/",
    ".cache/",
    ".parcel-cache/",
)

# Minified-file suffixes (fallback signal: bundlers emit `*.min.js` etc.)
_MINIFIED_SUFFIXES = (".min.js", ".min.mjs", ".min.css", ".bundle.js")


def _is_bundler_output_path(path: str) -> bool:
    """True if path lives under a known bundler/build output dir or looks
    like a minified artifact. Path is project-relative (no leading slash)."""
    if not path:
        return False
    p = path[2:] if path.startswith("./") else path
    for d in _BUNDLER_OUTPUT_DIRS:
        if p.startswith(d) or f"/{d}" in p:
            return True
    for sfx in _MINIFIED_SUFFIXES:
        if p.endswith(sfx):
            return True
    return False


# v0.11 P1 (bug #19): TS framework entry-point detection.
# Files under these path patterns are reachable via filesystem-based routing,
# not call edges — symbols in them are NOT dead code.
#
# Pattern: (path_segment_check, optional_basename_set_or_None)
#   - path_segment_check: substring that must appear anywhere in the path
#   - optional_basename_set: if not None, ONLY files whose stem is in this
#     set are treated as entry points (app-router style). If None, ANY file
#     under the segment is treated as entry-point (islands, pages-router).
_TS_FRAMEWORK_ENTRY_PATTERNS: tuple[tuple[str, frozenset[str] | None], ...] = (
    # Deno Fresh islands: any .ts/.tsx/.js/.jsx under islands/
    ("/islands/", None),
    # Next.js pages router: any file under pages/
    ("/pages/", None),
    # Next.js app router: only these magic basenames
    (
        "/app/",
        frozenset(
            {
                "page", "layout", "loading", "error",
                "not-found", "template", "default", "route",
            }
        ),
    ),
    # SvelteKit routes: only +page, +layout, +server, +error files
    (
        "/routes/",
        frozenset(
            {
                "+page", "+layout", "+server", "+error",
                "+page.server", "+layout.server",
            }
        ),
    ),
    # Remix app/routes: any file under app/routes/
    ("/routes/", None),
)

# TS/JS source extensions supported by the extractor
_TS_SRC_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".svelte"})


def _ts_framework_entry_point_kind(path: str) -> str | None:
    """Return a framework label if *path* is a TS/JS/Svelte file that lives
    inside a filesystem-based routing directory.

    Returns one of ``"fresh"``, ``"nextjs_pages"``, ``"nextjs_app"``,
    ``"sveltekit"``, ``"remix"`` or ``None``.

    Path must be project-relative (no leading slash).  Both
    ``pages/index.tsx`` and ``src/pages/index.tsx`` must match — the
    patterns check for the dir segment anywhere in the path so the
    optional ``src/`` prefix is handled automatically.
    """
    if not path:
        return None
    # Normalise: strip leading "./" and ensure leading "/" for segment checks
    p = path[2:] if path.startswith("./") else path
    # Extension filter first — cheap and eliminates most paths
    dot = p.rfind(".")
    ext = p[dot:].lower() if dot >= 0 else ""
    if ext not in _TS_SRC_EXTS:
        return None

    # Derive the file stem (basename without extension, and for SvelteKit
    # also strip the .server / .js/.ts secondary extension from "+page.server.ts")
    slash = p.rfind("/")
    basename = p[slash + 1 :] if slash >= 0 else p
    # strip extension(s): e.g. "+page.server.ts" → "+page.server"
    stem = basename[: basename.find(".")] if "." in basename else basename

    # Normalise path with a leading slash for consistent segment matching
    normalised = "/" + p

    # Islands (Fresh)
    if "/islands/" in normalised:
        return "fresh"

    # SvelteKit routes (must check before generic /routes/ below)
    if "/routes/" in normalised:
        sveltekit_stems = frozenset(
            {"+page", "+layout", "+server", "+error"}
        )
        if stem in sveltekit_stems or basename.startswith("+page.server") or basename.startswith("+layout.server"):
            return "sveltekit"
        # SvelteKit .svelte files under routes/ are always entry points
        if ext == ".svelte":
            return "sveltekit"
        # Remix: any .ts/.tsx/.js/.jsx under app/routes/
        if "/app/routes/" in normalised or normalised.startswith("/app/routes/"):
            return "remix"

    # Next.js pages router
    if "/pages/" in normalised:
        return "nextjs_pages"

    # Next.js app router
    if "/app/" in normalised:
        app_router_stems = frozenset(
            {
                "page", "layout", "loading", "error",
                "not-found", "template", "default", "route",
            }
        )
        if stem in app_router_stems:
            return "nextjs_app"

    return None


def _is_ts_framework_entry_point(meta: dict) -> bool:
    """True when the symbol lives in a TS framework filesystem-routing path.

    Used by ``find_dead_code`` to skip symbols that are reachable via routing
    conventions (Fresh islands, Next.js pages/app, SvelteKit routes, Remix
    routes) rather than explicit call edges.

    Any top-level ``function`` or ``class`` in those files counts as an
    entry point, regardless of whether the extractor captured a ``default``
    export marker (since tree-sitter TS extraction doesn't reliably surface
    that yet — v0.11 P2 will extend edges for JSX; for now, the path
    heuristic is the safe conservative choice).
    """
    file_path = meta.get("file_path") or ""
    kind = meta.get("kind") or ""
    return (
        kind in ("function", "class", "method")
        and _ts_framework_entry_point_kind(file_path) is not None
    )


def _structural_pattern_names(conn, project_id: int, threshold: int) -> set[str]:
    """Names appearing as a symbol in ≥`threshold` distinct files in the project.

    Captures repeated structural patterns (`.get`, `add_parser`, `run`,
    `__init__`, `from_dict`) that PageRank correctly identifies as
    high-centrality but carry near-zero "what is this codebase about"
    signal. v0.8 P2 session-01 fix.
    """
    rows = conn.execute(
        """SELECT s.name, COUNT(DISTINCT s.file_id) AS file_count
           FROM symbol s JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?
           GROUP BY s.name
           HAVING file_count >= ?""",
        (project_id, threshold),
    ).fetchall()
    return {r["name"] for r in rows if r["name"]}


def compute_endpoints(
    st: AppState,
    framework: str | None = None,
) -> list[dict[str, Any]]:
    """Module-level shared computation: the full unpaginated endpoint list.

    Both ``find_endpoints`` (the paginated tool wrapper) and
    ``export_explorer`` consume this. Returns the same per-endpoint dicts
    the tool emits (``qualified_name``, ``kind``, ``file_path``,
    ``start_line``, ``end_line``, ``decorators``, plus optional
    framework-specific keys: ``ts_framework`` / ``django_cbv_base`` /
    ``hono_method`` / ``hono_path`` / ``http_method`` / ``http_path``),
    sorted by ``(file_path, start_line)``.
    """
    pid = st.project_id

    rows = st.conn.execute(
        """SELECT s.qualified_name, s.kind, s.decorators, s.start_line, s.end_line,
                  f.path AS file_path
           FROM symbol s JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND s.decorators IS NOT NULL
           ORDER BY f.path, s.start_line""",
        (pid,),
    ).fetchall()

    if framework is not None:
        patterns = _FRAMEWORK_DECORATOR_PATTERNS.get(framework, ())

        def keep(decs: list[str]) -> list[str]:
            return [d for d in decs if _decorator_matches_any(d, patterns)]
    else:
        # v0.14: mirror find_dead_code's alias detection so plugin-
        # registered tools decorated via an alias factory
        # (`mutation_tool = mcp.tool if X else _noop`, used by the
        # Spec plugin's `@mutation_tool`/`@agentic_tool`) are surfaced
        # too — without this they read as plain decorators whose last
        # segment isn't in _ENTRY_POINT_DECORATOR_LASTSEG and get missed.
        workspace_path = st.settings.workspace
        alias_lastsegs: set[str] = set()
        for path_row in st.conn.execute(
            "SELECT f.path FROM file f WHERE f.project_id=? AND f.path LIKE '%.py'",
            (pid,),
        ):
            try:
                abs_path = str(workspace_path / path_row["path"])
                alias_lastsegs |= _entry_point_decorator_aliases(abs_path)
            except Exception:
                continue

        def keep(decs: list[str]) -> list[str]:
            return [
                d
                for d in decs
                if _decorator_lastseg(d) in _ENTRY_POINT_DECORATOR_LASTSEG
                or _decorator_lastseg(d) in alias_lastsegs
            ]

    endpoints: list[dict[str, Any]] = []
    seen_qnames: set[Any] = set()
    workspace_path = st.settings.workspace
    py_source_cache: dict[str, str] = {}

    def _py_source(rel_path: str) -> str:
        if rel_path not in py_source_cache:
            try:
                py_source_cache[rel_path] = (
                    workspace_path / rel_path
                ).read_text(encoding="utf-8", errors="replace")
            except OSError:
                py_source_cache[rel_path] = ""
        return py_source_cache[rel_path]

    def _attach_python_http_route(entry: dict[str, Any], matching_decs: list[str]) -> None:
        if not any(
            _decorator_lastseg(d) in HTTP_ROUTE_DECORATOR_LASTSEGS for d in matching_decs
        ):
            return
        fp = entry["file_path"]
        if not fp.endswith(".py"):
            return
        source = _py_source(fp)
        if not source:
            return
        route = parse_python_http_route(source, int(entry["start_line"]))
        if route["http_method"] is not None:
            entry["http_method"] = route["http_method"]
        if route["http_path"] is not None:
            entry["http_path"] = route["http_path"]
            entry["http_framework"] = infer_python_http_framework(source)

    for r in rows:
        try:
            decs = json.loads(r["decorators"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        matching = keep(decs)
        if not matching:
            continue
        entry = {
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "file_path": r["file_path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "decorators": matching,
        }
        _attach_python_http_route(entry, matching)
        endpoints.append(entry)
        seen_qnames.add(r["qualified_name"])

    # v0.11 P1: TS framework filesystem-routing detection (bug #19).
    # Fresh islands, Next.js pages/app, SvelteKit routes, and Remix routes
    # are reachable via path conventions — no decorators needed.
    # Run when framework is one of the TS framework names or None.
    _TS_FRAMEWORKS = {"nextjs", "fresh", "sveltekit", "remix"}
    if framework in _TS_FRAMEWORKS or framework is None:
        ts_rows = st.conn.execute(
            """SELECT s.qualified_name, s.kind, s.start_line, s.end_line,
                      f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.kind IN ('function', 'class')
               ORDER BY f.path, s.start_line""",
            (pid,),
        ).fetchall()
        for r in ts_rows:
            if r["qualified_name"] in seen_qnames:
                continue
            fp = r["file_path"]
            fw_kind = _ts_framework_entry_point_kind(fp)
            if fw_kind is None:
                continue
            # When a specific TS framework is requested, filter to it
            if framework is not None and framework != fw_kind and not (
                framework == "nextjs" and fw_kind in ("nextjs_pages", "nextjs_app")
            ):
                continue
            endpoints.append({
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file_path": fp,
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "decorators": [],
                "ts_framework": fw_kind,
            })
            seen_qnames.add(r["qualified_name"])

    # v0.9 P5: Django class-based view detection. Classes that
    # inherit from a Django CBV / mixin base are entry points without
    # decorators — the framework dispatches them via URL routing or
    # MIDDLEWARE setting. Run only when the requested framework is
    # 'django' or None (no filter).
    if framework in ("django", None):
        cbv_rows = st.conn.execute(
            """SELECT s.qualified_name, s.kind, s.signature, s.start_line,
                      s.end_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.kind='class' AND s.signature IS NOT NULL
               ORDER BY f.path, s.start_line""",
            (pid,),
        ).fetchall()
        for r in cbv_rows:
            if r["qualified_name"] in seen_qnames:
                continue
            cbv_base = _django_cbv_base_from_signature(r["signature"])
            if cbv_base is None:
                continue
            endpoints.append({
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file_path": r["file_path"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "decorators": [],
                "django_cbv_base": cbv_base,
            })
            seen_qnames.add(r["qualified_name"])

    # v0.13 P3: Hono call-style routes. Opt-in (reads files on demand).
    if framework == "hono":
        workspace_path = st.settings.workspace
        for fr in st.conn.execute(
            """SELECT id, path, language FROM file
               WHERE project_id=? AND language IN
                 ('typescript', 'javascript', 'tsx')
               ORDER BY path""",
            (pid,),
        ).fetchall():
            try:
                src = (workspace_path / fr["path"]).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            if "hono" not in src.lower():
                continue
            for rt in scan_hono_routes(src, fr["language"]):
                qname = None
                kind = "route"
                start_line = end_line = rt["line"]
                if rt["handler_name"]:
                    sym = st.conn.execute(
                        """SELECT s.qualified_name, s.kind, s.start_line,
                                  s.end_line
                           FROM symbol s JOIN file f ON f.id=s.file_id
                           WHERE f.project_id=? AND s.name=?
                           ORDER BY (s.file_id=?) DESC LIMIT 1""",
                        (pid, rt["handler_name"], fr["id"]),
                    ).fetchone()
                    if sym is not None:
                        qname = sym["qualified_name"]
                        kind = sym["kind"]
                        start_line = sym["start_line"]
                        end_line = sym["end_line"]
                entry_qname = qname or f"{fr['path']}:{rt['line']}"
                route_key = (rt["method"], rt["path"], entry_qname)
                if route_key in seen_qnames:
                    continue
                endpoints.append({
                    "qualified_name": entry_qname,
                    "kind": kind,
                    "file_path": fr["path"],
                    "start_line": start_line,
                    "end_line": end_line,
                    "decorators": [],
                    "hono_method": rt["method"],
                    "hono_path": rt["path"],
                })
                seen_qnames.add(route_key)

    endpoints.sort(key=lambda e: (e["file_path"], e["start_line"]))
    return endpoints


def _is_test_scaffold_path(file_path: str) -> bool:
    """True for tests/, conftest.py, and pytest fixture helper dirs."""
    fp = file_path.replace("\\", "/").lstrip("/")
    if fp.startswith("tests/") or "/tests/" in f"/{fp}":
        return True
    base = fp.rsplit("/", 1)[-1]
    if base == "conftest.py" or base.startswith("conftest_"):
        return True
    for seg in ("/fixtures/", "/__fixtures__/", "/test_utils/", "/test_helpers/"):
        if seg in f"/{fp}":
            return True
    return False


def _endpoint_is_pytest_fixture(ep: dict[str, Any]) -> bool:
    for dec in ep.get("decorators") or []:
        if _decorator_lastseg(str(dec)) == "fixture":
            return True
    return False


def filter_api_endpoints(
    endpoints: list[dict[str, Any]],
    framework: str | None,
    *,
    exclude_tests: bool = True,
) -> list[dict[str, Any]]:
    """Drop pytest fixtures and test-scaffold paths unless ``framework='pytest'``."""
    if framework == "pytest" or not exclude_tests:
        return endpoints
    out: list[dict[str, Any]] = []
    for ep in endpoints:
        if _endpoint_is_pytest_fixture(ep):
            continue
        if _is_test_scaffold_path(ep.get("file_path") or ""):
            continue
        out.append(ep)
    return out


# v0.15: forward-BFS depth bound for auto-derived Spec test coverage. A test
# reaches an implementation through at most: test → helper/fixture → impl
# (two indirections). Depth-LIMITED, not a full transitive closure, so the
# multi-source BFS stays cheap on big repos (Django ~40K symbols). Computed
# ONCE per audit, not per-Spec.
_TEST_REACH_DEPTH = 3


def _is_test_file_path(path: str) -> bool:
    """True if `path` is a test file. Same heuristic as the nested
    ``is_test_path`` in ``find_orphan_tests`` (lifted to module scope so the
    Spec-test-coverage derivation reuses it instead of reinventing detection):
    anything under a ``tests/`` tree or matching ``test_*`` / ``*_test.*``
    naming. Path is project-relative (no leading slash)."""
    base = path.rsplit("/", 1)[-1]
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base.endswith("_test.go")
        or "_test." in base
    )


def compute_spec_test_coverage(
    st: AppState,
    view: GraphView,
) -> dict[str, dict[str, Any]]:
    """Auto-derive per-Spec test coverage from the call graph (v0.15).

    Reuses the already-loaded cached ``view`` (do NOT reload the graph).

    Algorithm:
    1. TEST symbols = symbols whose file is a test file (``_is_test_file_path``).
    2. ``tested_symbols`` = multi-source forward BFS from ALL test symbols
       over the call graph, bounded ``_TEST_REACH_DEPTH``. Computed once.
    3. For each Spec: an ``implements`` symbol S counts as TESTED if it is in
       ``tested_symbols`` (derived) OR carries an explicit ``relation='tests'``
       link (explicit). ``coverage_source`` ∈ {derived, explicit, both, none}
       records which kinds contributed.

    Returns a mapping ``spec_id -> {spec_id, title, test_coverage_ratio,
    tested_symbols, total_symbols, coverage_source}``. ``test_coverage_ratio``
    is 0.0 when the Spec has no ``implements`` symbols.
    """
    pid = st.project_id
    g = view.g

    # Step 1: collect TEST symbol ids (those whose file is a test file).
    test_sids: set[int] = {
        sid
        for sid, meta in view.sym_meta.items()
        if _is_test_file_path(meta.get("file_path") or "")
    }

    # Step 2: multi-source forward BFS, bounded depth, computed ONCE.
    tested_symbols: set[int] = set()
    frontier: list[tuple[int, int]] = [
        (sid, 0) for sid in test_sids if sid in g
    ]
    # Seed: a test symbol is itself "covered" trivially, but we only care
    # about what tests REACH — production impl symbols downstream. We still
    # add the seeds so an impl symbol that is *itself* a test symbol (rare)
    # is counted. Forward edges expand from there up to _TEST_REACH_DEPTH.
    tested_symbols.update(sid for sid, _ in frontier)
    while frontier:
        node, depth = frontier.pop()
        if depth >= _TEST_REACH_DEPTH:
            continue
        for succ in g.successors(node):
            if succ in tested_symbols:
                continue
            tested_symbols.add(succ)
            frontier.append((succ, depth + 1))

    # Step 3: per-Spec rollup. One pass over the spec/spec_symbol join.
    rows = st.conn.execute(
        """SELECT r.spec_id, r.title, rs.symbol_id, rs.relation
           FROM spec r JOIN spec_symbol rs ON rs.spec_id=r.id
           WHERE r.project_id=?
           ORDER BY r.spec_id""",
        (pid,),
    ).fetchall()

    # spec_id -> {"title", "impl": set[int], "explicit": set[int]}
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        spec_id = r["spec_id"]
        bucket = agg.get(spec_id)
        if bucket is None:
            bucket = {"title": r["title"], "impl": set(), "explicit": set()}
            agg[spec_id] = bucket
        sid = int(r["symbol_id"])
        relation = r["relation"]
        if relation == "implements":
            bucket["impl"].add(sid)
        elif relation == "tests":
            bucket["explicit"].add(sid)

    # v0.16 B: cap on the per-Spec uncovered drill-down list.
    _UNCOVERED_CAP = 50

    out: dict[str, dict[str, Any]] = {}
    for spec_id, bucket in agg.items():
        impl: set[int] = bucket["impl"]
        explicit: set[int] = bucket["explicit"]
        total = len(impl)
        derived_hit = False
        explicit_hit = False
        tested_count = 0
        uncovered_sids: list[int] = []
        for sid in impl:
            in_derived = sid in tested_symbols
            in_explicit = sid in explicit
            if in_derived:
                derived_hit = True
            if in_explicit:
                explicit_hit = True
            if in_derived or in_explicit:
                tested_count += 1
            else:
                uncovered_sids.append(sid)
        ratio = (tested_count / total) if total else 0.0
        if derived_hit and explicit_hit:
            source = "both"
        elif derived_hit:
            source = "derived"
        elif explicit_hit:
            source = "explicit"
        else:
            source = "none"
        # v0.16 B: drill-down — qnames of `implements` symbols that are
        # neither test-reached (derived) nor explicitly `tests`-linked.
        # Resolve sids -> qnames via the cached graph meta; sort for a
        # stable payload and cap at _UNCOVERED_CAP with an exact count.
        uncovered_qnames = sorted(
            view.sym_meta[sid]["qualified_name"]
            for sid in uncovered_sids
            if sid in view.sym_meta and view.sym_meta[sid].get("qualified_name")
        )
        uncovered_total = len(uncovered_qnames)
        out[spec_id] = {
            "spec_id": spec_id,
            "title": bucket["title"],
            "test_coverage_ratio": round(ratio, 4),
            "tested_symbols": tested_count,
            "total_symbols": total,
            "coverage_source": source,
            "uncovered_symbols": uncovered_qnames[:_UNCOVERED_CAP],
            "uncovered_symbols_count": uncovered_total,
        }
    return out


def compute_coverage(st: AppState) -> dict[str, Any]:
    """Module-level shared computation: the full unpaginated coverage audit.

    Both ``audit_coverage`` (the paginated tool wrapper) and
    ``export_explorer`` consume this. Returns the complete lists plus an
    exact ``counts`` dict — pagination is applied by the tool wrapper, not
    here.
    """
    pid = st.project_id

    # v0.8 P2 fix #8: filter package-marker basenames out of the
    # "modules without Spec" candidate set. They are import infrastructure,
    # never the right home for a `@spec:` annotation.
    _PACKAGE_MARKER_BASENAMES = frozenset({
        "__init__.py",
        "package-info.java",
        "mod.rs",
        "lib.rs",
        "index.ts",  # only when content-empty / re-export only — kept here for the common case
        "index.js",
    })

    def _is_package_marker(path: str) -> bool:
        return path.rsplit("/", 1)[-1] in _PACKAGE_MARKER_BASENAMES

    from pathlib import Path as _Path

    from livespec_mcp.domain.languages import (
        ANNOTATION_SUPPORTED_LANGUAGES,
        detect_language,
    )

    def _annotation_supported(path: str) -> bool:
        lang = detect_language(_Path(path))
        return lang in ANNOTATION_SUPPORTED_LANGUAGES

    all_no_spec = [
        r["path"]
        for r in st.conn.execute(
            """SELECT f.path FROM file f
               WHERE f.project_id=?
                 AND NOT EXISTS (
                   SELECT 1 FROM symbol s
                   JOIN spec_symbol rs ON rs.symbol_id=s.id
                   WHERE s.file_id=f.id
                 )
               ORDER BY f.path""",
            (pid,),
        )
        if not _is_package_marker(r["path"])
    ]
    # Split off files whose language has no annotation extractor —
    # these are not "truly orphan", just outside what we can scan.
    modules_unsupported_language = [p for p in all_no_spec if not _annotation_supported(p)]
    modules_no_spec = [p for p in all_no_spec if _annotation_supported(p)]

    # Split direct-orphan into implicitly-covered vs truly-orphan via the
    # call graph: a file is implicitly covered if any of its symbols has
    # a spec-linked symbol in its ancestor cone (someone calls in here from
    # an annotated entry point).
    # Load the cached call graph once and reuse it for both the
    # implicit-coverage split below and the per-Spec test-coverage derivation
    # (v0.15). load_graph is cached by (db, project, run_id) — cheap.
    view = load_graph(st.conn, pid)

    modules_implicit: list[str] = []
    modules_truly_orphan: list[str] = []
    if modules_no_spec:
        spec_linked_sids: set[int] = {
            int(r["symbol_id"])
            for r in st.conn.execute(
                """SELECT DISTINCT rs.symbol_id FROM spec_symbol rs
                   JOIN symbol s ON s.id=rs.symbol_id
                   JOIN file f ON f.id=s.file_id
                   WHERE f.project_id=?""",
                (pid,),
            )
        }
        for path in modules_no_spec:
            file_sids = {
                int(r["id"])
                for r in st.conn.execute(
                    """SELECT s.id FROM symbol s
                       JOIN file f ON f.id=s.file_id
                       WHERE f.project_id=? AND f.path=?""",
                    (pid, path),
                )
            }
            covered = False
            if spec_linked_sids and file_sids:
                for sid in file_sids:
                    if sid not in view.g:
                        continue
                    if ancestors_within(view.g, sid, 10) & spec_linked_sids:
                        covered = True
                        break
            (modules_implicit if covered else modules_truly_orphan).append(path)

    specs_no_impl = [
        dict(r)
        for r in st.conn.execute(
            """SELECT r.spec_id, r.title, r.status, r.priority FROM spec r
               WHERE r.project_id=?
                 AND NOT EXISTS (
                   SELECT 1 FROM spec_symbol rs WHERE rs.spec_id=r.id
                 )
               ORDER BY r.spec_id""",
            (pid,),
        )
    ]

    specs_low_conf = [
        {
            "spec_id": r["spec_id"],
            "title": r["title"],
            "avg_confidence": round(float(r["avg_confidence"]), 3),
            "link_count": int(r["link_count"]),
        }
        for r in st.conn.execute(
            """SELECT r.spec_id, r.title,
                      AVG(rs.confidence) AS avg_confidence,
                      COUNT(rs.id) AS link_count
               FROM spec r JOIN spec_symbol rs ON rs.spec_id=r.id
               WHERE r.project_id=?
               GROUP BY r.id
               HAVING avg_confidence < 0.7
               ORDER BY avg_confidence ASC""",
            (pid,),
        )
    ]

    # v0.8 P2 fix #9: Specs with at least one spec_symbol row whose
    # relation is 'tests'. Schema already supports this; surface it.
    spec_test_coverage = [
        {
            "spec_id": r["spec_id"],
            "title": r["title"],
            "test_count": int(r["test_count"]),
        }
        for r in st.conn.execute(
            """SELECT r.spec_id, r.title, COUNT(rs.id) AS test_count
               FROM spec r JOIN spec_symbol rs ON rs.spec_id=r.id
               WHERE r.project_id=? AND rs.relation='tests'
               GROUP BY r.id
               ORDER BY test_count DESC, r.spec_id""",
            (pid,),
        )
    ]

    # v0.15: auto-derived per-Spec test coverage from the call graph. Reuses
    # the cached `view` (no reload). Additive — leaves the explicit-link
    # `spec_test_coverage` / `specs_with_test_coverage` above untouched.
    spec_coverage_map = compute_spec_test_coverage(st, view)
    spec_coverage = sorted(
        spec_coverage_map.values(),
        key=lambda d: (-d["test_coverage_ratio"], d["spec_id"]),
    )
    specs_with_any_test_coverage = sum(
        1 for d in spec_coverage if d["test_coverage_ratio"] > 0
    )
    avg_test_coverage = (
        round(
            sum(d["test_coverage_ratio"] for d in spec_coverage) / len(spec_coverage),
            4,
        )
        if spec_coverage
        else 0.0
    )

    # v0.16 D: record one coverage trend snapshot per audit. The avg +
    # verified-Spec count + per-Spec ratios are appended to spec_coverage_snapshot
    # so the explorer can plot coverage over time. Best-effort: a recording
    # failure must never break the audit itself.
    try:
        from datetime import UTC, datetime

        from livespec_mcp.storage import trends

        trends.record_snapshot(
            st.conn,
            pid,
            per_spec={d["spec_id"]: d["test_coverage_ratio"] for d in spec_coverage},
            avg=avg_test_coverage if spec_coverage else None,
            verified_count=specs_with_any_test_coverage,
            ts=datetime.now(UTC).isoformat(),
        )
    except Exception:
        pass

    counts = {
        "modules_without_spec": len(modules_no_spec),
        "modules_implicitly_covered": len(modules_implicit),
        "modules_truly_orphan": len(modules_truly_orphan),
        "modules_unsupported_language": len(modules_unsupported_language),
        "specs_without_implementation": len(specs_no_impl),
        "specs_low_confidence": len(specs_low_conf),
        "specs_with_test_coverage": len(spec_test_coverage),
        "specs_with_any_test_coverage": specs_with_any_test_coverage,
        "avg_test_coverage": avg_test_coverage,
    }
    return {
        "counts": counts,
        "modules_without_spec": modules_no_spec,
        "modules_implicitly_covered": modules_implicit,
        "modules_truly_orphan": modules_truly_orphan,
        "modules_unsupported_language": modules_unsupported_language,
        "specs_without_implementation": specs_no_impl,
        "specs_low_confidence": specs_low_conf,
        "spec_coverage": spec_coverage,
        "avg_test_coverage": avg_test_coverage,
        "specs_with_any_test_coverage": specs_with_any_test_coverage,
        "spec_test_coverage": spec_test_coverage,
    }


def _git_diff_changed_files(
    ws_root: str, base_ref: str, head_ref: str
) -> tuple[list[str] | None, dict[str, Any] | None]:
    """Run ``git diff --name-only base..head`` for ``ws_root``.

    Shared core for ``git_diff_impact`` (the paginated tool) and
    ``compute_diff_spec_impact`` (the explorer helper). Returns
    ``(changed_paths, None)`` on success or ``(None, error_dict)`` when git
    is missing / the range is unknown / the workspace has no history — the
    error dict is already shaped by ``mcp_error`` so callers can return it
    verbatim or treat its presence as "git unavailable".
    """
    try:
        proc = subprocess.run(
            ["git", "-C", ws_root, "diff", "--name-only", f"{base_ref}..{head_ref}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, mcp_error(
            "git not found on PATH",
            hint="install git and ensure it is on PATH for this MCP server process",
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        stdout = (e.stdout or "").strip()
        stderr_lower = stderr.lower()
        if "not a git repository" in stderr_lower:
            msg = (
                f"workspace is not a git repository: {ws_root}. "
                "git_diff_impact requires git history; run `git init` "
                "and at least one commit first."
            )
        elif "unknown revision" in stderr_lower or "bad revision" in stderr_lower:
            msg = (
                f"unknown git ref(s): base_ref='{base_ref}', "
                f"head_ref='{head_ref}'. Check `git rev-parse` for both."
            )
        elif "ambiguous argument" in stderr_lower:
            msg = (
                f"ambiguous ref: '{base_ref}..{head_ref}'. "
                "Use full SHAs or branch names that exist locally."
            )
        else:
            first_line = next(
                (ln for ln in (stderr or stdout).splitlines() if ln.strip()),
                "",
            )
            msg = (
                f"git diff failed: {first_line[:200]}"
                if first_line
                else "git diff failed (no diagnostic output)"
            )
        return None, mcp_error(msg)
    except subprocess.TimeoutExpired:
        return None, mcp_error(
            "git diff timed out after 10s",
            hint="narrow the ref range or check for a runaway git hook",
        )

    return [p for p in proc.stdout.splitlines() if p.strip()], None


def compute_diff_spec_impact(
    st: AppState,
    base: str,
    head: str,
    max_depth: int = 5,
) -> dict[str, Any]:
    """Spec-centric impact of a git range, for the explorer export (v0.16 A).

    Walks the diff the same way ``git_diff_impact`` does (changed files →
    indexed symbols → backward caller cone → touched Specs), then folds each
    touched Spec down to ``{spec_id, title, files, test_coverage_ratio}``:

    * ``files`` — the CHANGED files that contribute symbols to that Spec
      (either directly linked or whose change reaches the Spec's symbols
      through the caller cone), sorted.
    * ``test_coverage_ratio`` — pulled from ``compute_spec_test_coverage`` so
      the explorer can flag "touched but under-tested" Specs.

    Returns ``{base, head, files_changed, specs_touched}``. When git
    or the range is unavailable, returns the same keys with empty lists so
    the caller can simply omit the section.
    """
    empty: dict[str, Any] = {
        "base": base,
        "head": head,
        "files_changed": [],
        "specs_touched": [],
    }

    changed_paths, err = _git_diff_changed_files(
        str(st.settings.workspace), base, head
    )
    if err is not None or not changed_paths:
        return empty

    pid = st.project_id
    view = load_graph(st.conn, pid)

    # changed file -> set of symbol ids defined in it
    sids_by_file: dict[str, set[int]] = {}
    changed_sym_ids: set[int] = set()
    for path in changed_paths:
        rows = st.conn.execute(
            """SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id
               WHERE f.project_id=? AND f.path=?""",
            (pid, path),
        ).fetchall()
        if not rows:
            continue
        ids = {int(r["id"]) for r in rows}
        sids_by_file[path] = ids
        changed_sym_ids |= ids

    if not changed_sym_ids:
        return empty

    # For each changed file, the cone of symbols its change touches
    # (the file's own symbols + their backward callers). Used to map a
    # touched Spec back to the changed file(s) responsible.
    cone_by_file: dict[str, set[int]] = {}
    for path, ids in sids_by_file.items():
        cone: set[int] = set(ids)
        for sid in ids:
            if sid in view.g:
                cone |= ancestors_within(view.g, sid, max_depth)
        cone_by_file[path] = cone

    all_touched: set[int] = set()
    for cone in cone_by_file.values():
        all_touched |= cone
    if not all_touched:
        return empty

    # Map every touched symbol id -> the Specs that link it.
    placeholders = ",".join("?" * len(all_touched))
    sym_to_specs: dict[int, list[str]] = {}
    spec_titles: dict[str, str] = {}
    for r in st.conn.execute(
        f"""SELECT rs.symbol_id, r.spec_id, r.title
            FROM spec_symbol rs JOIN spec r ON r.id = rs.spec_id
            WHERE r.project_id=? AND rs.symbol_id IN ({placeholders})""",
        [pid, *list(all_touched)],
    ):
        sym_to_specs.setdefault(int(r["symbol_id"]), []).append(r["spec_id"])
        spec_titles[r["spec_id"]] = r["title"]

    if not spec_titles:
        return {
            "base": base,
            "head": head,
            "files_changed": sorted(sids_by_file.keys()),
            "specs_touched": [],
        }

    # Per-Spec: which CHANGED files reach it (via that file's cone).
    files_by_spec: dict[str, set[str]] = {}
    for path, cone in cone_by_file.items():
        for sid in cone:
            for spec_id in sym_to_specs.get(sid, ()):
                files_by_spec.setdefault(spec_id, set()).add(path)

    coverage_map = compute_spec_test_coverage(st, view)
    specs_touched = [
        {
            "spec_id": spec_id,
            "title": spec_titles[spec_id],
            "files": sorted(files_by_spec.get(spec_id, set())),
            "test_coverage_ratio": (
                coverage_map[spec_id]["test_coverage_ratio"]
                if spec_id in coverage_map
                else 0.0
            ),
        }
        for spec_id in sorted(spec_titles)
    ]

    return {
        "base": base,
        "head": head,
        "files_changed": sorted(sids_by_file.keys()),
        "specs_touched": specs_touched,
    }


def compute_project_overview(
    st: AppState,
    include_infrastructure: bool = False,
    include_structural_patterns: bool = False,
) -> dict[str, Any]:
    """Module-level shared computation. Resources and the tool wrapper use this.

    `include_structural_patterns=False` (default) hides symbols whose short
    name appears in ≥3 distinct files — `.get`, `add_parser`, `run` etc.
    PageRank correctly ranks them as high-centrality but they're structural
    patterns, not semantically distinctive symbols. Set True to see the
    raw PageRank top.
    """
    pid = st.project_id
    langs = [
        dict(r)
        for r in st.conn.execute(
            "SELECT language, COUNT(*) files FROM file WHERE project_id=? GROUP BY language",
            (pid,),
        )
    ]
    structural_names: set[str] = (
        set()
        if include_structural_patterns
        else _structural_pattern_names(st.conn, pid, _STRUCTURAL_NAME_FILE_THRESHOLD)
    )
    view = load_graph(st.conn, pid)
    ranks = page_rank(view.g)
    ordered = sorted(ranks.items(), key=lambda x: x[1], reverse=True)
    top_syms: list[dict[str, Any]] = []
    for sid, score in ordered:
        meta = view.sym_meta.get(sid)
        if meta is None:
            continue
        if not include_infrastructure and _is_infrastructure(meta):
            continue
        if structural_names and meta.get("name") in structural_names:
            continue
        if _is_bundler_output_path(meta.get("file_path") or ""):
            continue
        top_syms.append({**meta, "pagerank": round(score, 6)})
        if len(top_syms) >= 20:
            break
    spec_total = st.conn.execute(
        "SELECT COUNT(*) c FROM spec WHERE project_id=?", (pid,)
    ).fetchone()["c"]
    spec_linked = st.conn.execute(
        """SELECT COUNT(DISTINCT r.id) c FROM spec r
           JOIN spec_symbol rs ON rs.spec_id=r.id WHERE r.project_id=?""",
        (pid,),
    ).fetchone()["c"]
    return {
        "workspace": str(st.settings.workspace),
        "languages": langs,
        "top_symbols": top_syms,
        "structural_patterns_filtered": sorted(structural_names),
        "specs_total": int(spec_total),
        "specs_linked": int(spec_linked),
    }


def _is_infrastructure(meta: dict) -> bool:
    """Heuristic for symbols that rank high by PageRank but carry little
    semantic weight: DI helpers, FastMCP `register` outers, dunders, tiny
    wrappers. P0.3."""
    qname = meta.get("qualified_name") or ""
    name = meta.get("name") or ""
    kind = meta.get("kind") or ""
    start = meta.get("start_line") or 0
    end = meta.get("end_line") or 0
    line_count = max(0, end - start)

    # Dunders (anywhere in the name path, e.g. Foo.__init__)
    if name.startswith("__") and name.endswith("__"):
        return True
    if any(seg.startswith("__") and seg.endswith("__") for seg in qname.split(".")):
        return True
    # FastMCP `register` outer functions live at module scope and contain inner tools.
    if name == "register" and kind == "function":
        return True
    # Common DI / config helpers
    if kind in ("function", "method") and any(name.endswith(suf) for suf in _INFRA_NAME_SUFFIXES):
        return True
    # One-line wrappers: function/method whose body is shorter than 5 lines
    if kind in ("function", "method") and 0 < line_count < 5:
        return True
    return False


def _resolve_symbol(conn, project_id: int, identifier: str) -> dict | None:
    """Resolve a symbol by qualified_name (exact) or short name (best match)."""
    row = conn.execute(
        """SELECT s.*, f.path as file_path FROM symbol s
           JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
        (project_id, identifier),
    ).fetchone()
    if row:
        return dict(row)
    rows = conn.execute(
        """SELECT s.*, f.path as file_path FROM symbol s
           JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND s.name=? LIMIT 5""",
        (project_id, identifier),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    return None


def did_you_mean_symbols(conn, project_id: int, identifier: str, limit: int = 3) -> list[dict]:
    """Top-N symbol suggestions for a misspelled or partial identifier.

    Used by tools that raise 'Symbol not found' to surface likely intended
    targets in the error payload (P2.D3). Combines two passes:
      1. SQL substring match on name / qualified_name (catches partials,
         prefix mistypes).
      2. difflib SequenceMatcher ratio on the short name (catches typos
         where the substring path doesn't fire — e.g. 'logn' ≈ 'login').
    Ranked by ratio descending. Project-scoped.
    """
    short = identifier.split(".")[-1]
    rows = conn.execute(
        """SELECT s.qualified_name, s.kind, f.path AS file_path, s.name
           FROM symbol s JOIN file f ON f.id=s.file_id
           WHERE f.project_id=?""",
        (project_id,),
    ).fetchall()
    if not rows:
        return []

    name_to_rows: dict[str, list] = {}
    for r in rows:
        name_to_rows.setdefault(r["name"], []).append(r)

    candidates = list(name_to_rows.keys())
    matches = difflib.get_close_matches(short, candidates, n=limit * 2, cutoff=0.55)

    seen: set[str] = set()
    out: list[dict] = []
    short_lower = short.lower()
    # Substring hits first (treated as ratio=0.99 for ranking ties)
    for r in rows:
        if len(out) >= limit:
            break
        if short_lower in (r["name"] or "").lower() or short_lower in (r["qualified_name"] or "").lower():
            qn = r["qualified_name"]
            if qn in seen:
                continue
            seen.add(qn)
            out.append(
                {"qualified_name": qn, "kind": r["kind"], "file_path": r["file_path"]}
            )
    for m in matches:
        if len(out) >= limit:
            break
        for r in name_to_rows.get(m, []):
            qn = r["qualified_name"]
            if qn in seen:
                continue
            seen.add(qn)
            out.append(
                {"qualified_name": qn, "kind": r["kind"], "file_path": r["file_path"]}
            )
            if len(out) >= limit:
                break
    return out


def symbol_not_found_error(conn, project_id: int, identifier: str) -> dict:
    """Build the standard 'Symbol not found' error payload with did_you_mean."""
    return mcp_error(
        f"Symbol '{identifier}' not found",
        did_you_mean=did_you_mean_symbols(conn, project_id, identifier),
        hint="run `find_symbol(query=<short_name>)` to discover qualified names",
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_symbol(
        query: str,
        kind: str | None = None,
        limit: int = 50,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Search symbols by name substring or qualified name.

        Returns lightweight refs (qualified_name, file, line, signature, kind).
        Use `get_symbol_info` for full details on a single match.

        v0.7 (B5): separator-agnostic match. The query and the qualified_name
        are both normalized so that `Type::method`, `Type.method`, and
        `module/Type::method` all match the same symbols. Useful in Rust
        repos where qnames mix `.` (file path) and `::` (impl method)
        separators.""" + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        pid = st.project_id

        # Normalize separators so `::` queries match `.`-separated stored
        # qnames and vice-versa. SQLite's LIKE doesn't support regex, so we
        # use the REPLACE() function on the column to compare normalized
        # forms. The query is normalized in Python before binding.
        normalized_query = query.replace("::", ".").replace("/", ".")
        like = f"%{normalized_query}%"
        sql = [
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.signature,
                      s.start_line, s.end_line, f.path as file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND (
                   s.name LIKE ?
                   OR s.qualified_name LIKE ?
                   OR REPLACE(s.qualified_name, '::', '.') LIKE ?
               )"""
        ]
        args: list[Any] = [pid, f"%{query}%", f"%{query}%", like]
        if kind:
            sql.append("AND s.kind = ?")
            args.append(kind)
        sql.append("ORDER BY length(s.qualified_name) LIMIT ?")
        args.append(limit)
        rows = st.conn.execute(" ".join(sql), args).fetchall()
        out: dict[str, Any] = {"matches": [dict(r) for r in rows]}
        if not rows:
            # v0.14: zero matches on the project's own fuzzy-lookup tool is
            # a dead end for an agent — surface typo-distance suggestions
            # the same way the not-found errors do. Not an error payload:
            # empty matches is a valid result, did_you_mean rides along.
            suggestions = did_you_mean_symbols(st.conn, pid, query)
            if suggestions:
                out["did_you_mean"] = suggestions
        return out

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_symbol_source(
        qname: str,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Source body for a symbol — file slice between start_line and end_line.

        Lighter alternative to `get_symbol_info(detail='full')` when only the
        body text is needed. Returns `{qualified_name, file_path, start_line,
        end_line, source, body_hash}`. Resolution accepts either a fully-
        qualified name (preferred) or a short name when unambiguous.
        """
        st = get_state(workspace)
        pid = st.project_id
        sym = _resolve_symbol(st.conn, pid, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pid, qname)
        try:
            fp = st.settings.workspace / sym["file_path"]
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(sym["start_line"] - 1, 0)
            end = min(sym["end_line"], len(lines))
            source = "\n".join(lines[start:end])
        except OSError as e:
            return mcp_error(
                f"file unreadable: {sym['file_path']}",
                hint=str(e),
            )
        return {
            "qualified_name": sym["qualified_name"],
            "file_path": sym["file_path"],
            "start_line": sym["start_line"],
            "end_line": sym["end_line"],
            "source": source,
            "body_hash": sym["body_hash"],
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def who_calls(
        qname: str,
        max_depth: int = 1,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        min_weight: float = 0.6,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Symbols that call `qname` (transitive backward cone up to max_depth).

        Slim alias of `analyze_impact(target_type='symbol', target=qname,
        max_depth=...)` that returns only the callers list — no forward cone,
        no Spec rollup. Use when an agent only needs the answer to "what would
        break if I touched this?".

        v0.9 P2: pagination contract. ``limit`` (default 200) caps the
        ``callers`` array; ``cursor`` resumes from a prior call's
        ``next_cursor``; ``summary_only=True`` returns only ``count`` +
        ``root`` + ``max_depth`` (no caller list). Surfaced by Django
        battle-test where ``max_depth=2`` produced 102KB / 400 callers.
        ``count`` is always exact regardless of pagination.

        v0.9 P3: ``min_weight`` (default 0.6) skips the resolver
        fan-out edges that the static analyzer couldn't disambiguate
        (weight 0.5 — multiple short-name candidates, no scope match).
        Pass ``min_weight=0.0`` to see the unfiltered cone (legacy).
        """
        st = get_state(workspace)
        pid = st.project_id
        sym = _resolve_symbol(st.conn, pid, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pid, qname)
        view = load_graph(st.conn, pid)
        sid = int(sym["id"])
        callers = (
            ancestors_within(view.g, sid, max_depth, min_weight=min_weight)
            if sid in view.g
            else set()
        )
        total = len(callers)
        if summary_only:
            return {
                "root": sym["qualified_name"],
                "max_depth": max_depth,
                "count": total,
            }
        meta_sorted = sorted(
            (view.sym_meta[n] for n in callers if n in view.sym_meta),
            key=lambda m: (m.get("file_path", ""), m.get("start_line", 0)),
        )
        page = meta_sorted[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(meta_sorted) else None
        return _attach_payload_warning(
            {
                "root": sym["qualified_name"],
                "max_depth": max_depth,
                "callers": page,
                "count": total,
                "next_cursor": next_cursor,
            },
            _payload_warning(total, limit=limit, summary_only=summary_only),
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def who_does_this_call(
        qname: str,
        max_depth: int = 1,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        min_weight: float = 0.6,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Symbols that `qname` calls (transitive forward cone up to max_depth).

        Forward-direction counterpart of `who_calls`. Same v0.9 P2
        pagination contract: ``limit`` / ``cursor`` / ``summary_only``.
        Same v0.9 P3 fan-out filter: ``min_weight=0.6`` by default.
        """
        st = get_state(workspace)
        pid = st.project_id
        sym = _resolve_symbol(st.conn, pid, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pid, qname)
        view = load_graph(st.conn, pid)
        sid = int(sym["id"])
        callees = (
            descendants_within(view.g, sid, max_depth, min_weight=min_weight)
            if sid in view.g
            else set()
        )
        total = len(callees)
        if summary_only:
            return {
                "root": sym["qualified_name"],
                "max_depth": max_depth,
                "count": total,
            }
        meta_sorted = sorted(
            (view.sym_meta[n] for n in callees if n in view.sym_meta),
            key=lambda m: (m.get("file_path", ""), m.get("start_line", 0)),
        )
        page = meta_sorted[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(meta_sorted) else None
        return {
            "root": sym["qualified_name"],
            "max_depth": max_depth,
            "callees": page,
            "count": total,
            "next_cursor": next_cursor,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def quick_orient(
        qname: str,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Composite snapshot — collapses 3-4 tool calls into one.

        Returns the symbol's metadata (kind, signature, file, line range),
        the first non-empty line of its docstring, the top-5 direct callers
        and top-5 direct callees ranked by PageRank, any linked Specs, and an
        `is_entry_point` flag (true when the symbol is decorated with a
        framework decorator like `@mcp.tool`, `@app.route`, `@task`, etc.) —
        so a `callers_count: 0` result is not misread as dead code.
        Designed for an agent's first contact with an unfamiliar symbol:
        instead of `find_symbol` -> `get_symbol_info` -> `analyze_impact`
        -> `get_spec_implementation`, run this once.""" + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        pid = st.project_id
        sym = _resolve_symbol(st.conn, pid, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pid, qname)
        sid = int(sym["id"])
        view = load_graph(st.conn, pid)
        ranks = page_rank(view.g) if sid in view.g else {}

        # v0.9 P3: filter out resolver fan-out (weight 0.5 — short-name
        # collisions the static analyzer couldn't disambiguate). Surfaced
        # by Django battle-test where two different `process_request`
        # methods reported identical top_callers because every callsite
        # matched both their short names.
        callers_all = (
            ancestors_within(view.g, sid, 1, min_weight=0.6)
            if sid in view.g
            else set()
        )
        callees_all = (
            descendants_within(view.g, sid, 1, min_weight=0.6)
            if sid in view.g
            else set()
        )

        def _topn(ids: set[int], n: int = 5) -> list[dict[str, Any]]:
            scored = sorted(
                (
                    (view.sym_meta[i], ranks.get(i, 0.0))
                    for i in ids
                    if i in view.sym_meta
                ),
                key=lambda x: x[1],
                reverse=True,
            )
            return [
                {**meta, "pagerank": round(score, 6)}
                for meta, score in scored[:n]
            ]

        specs = st.conn.execute(
            """SELECT r.spec_id, r.title, rs.relation, rs.confidence
               FROM spec_symbol rs JOIN spec r ON r.id=rs.spec_id WHERE rs.symbol_id=?""",
            (sid,),
        ).fetchall()

        docstring_lead = None
        ds = sym["docstring"]
        if ds:
            for line in ds.splitlines():
                stripped = line.strip()
                if stripped:
                    docstring_lead = stripped
                    break

        # v0.8 P2 session-01 fix: an `@mcp.tool`/`@app.route`/etc. with 0
        # callers in the indexed graph is an *entry point*, not dead code.
        # The matcher already detects this set (`_ENTRY_POINT_DECORATOR_LASTSEG`)
        # for `find_endpoints` / infrastructure filtering. Surface it here so
        # the agent doesn't misread the cone.
        decorators_json = sym["decorators"] if "decorators" in sym.keys() else None
        is_entry_point = _has_entry_point_decorator(decorators_json)
        framework_decorators: list[str] = []
        if decorators_json:
            try:
                all_decs = json.loads(decorators_json)
                framework_decorators = [
                    d for d in all_decs
                    if _decorator_lastseg(d) in _ENTRY_POINT_DECORATOR_LASTSEG
                ]
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "qualified_name": sym["qualified_name"],
            "kind": sym["kind"],
            "signature": sym["signature"],
            "file_path": sym["file_path"],
            "start_line": sym["start_line"],
            "end_line": sym["end_line"],
            "docstring_lead": docstring_lead,
            "is_entry_point": is_entry_point,
            "framework_decorators": framework_decorators,
            "callers_count": len(callers_all),
            "callees_count": len(callees_all),
            "top_callers": _topn(callers_all),
            "top_callees": _topn(callees_all),
            "specs": [dict(r) for r in specs],
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def analyze_impact(
        target_type: Literal["symbol", "file", "spec"],
        target: str,
        max_depth: int = 5,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        min_weight: float = 0.6,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Topological impact analysis: what changes if `target` changes.

        - symbol: backward cone of callers + Specs that touch any reached symbol.
          Set max_depth=1 to get the equivalent of a "find references".
        - file:   union of impacts from every symbol in the file.
        - spec: forward cone from every symbol implementing the Spec + their callers.

        v0.9 P2: pagination contract. ``impacted_callers`` and (for symbol
        target) ``calls_into`` are paginated by ``limit`` (default 200).
        ``cursor`` resumes; ``summary_only=True`` returns counts only.
        Surfaced by Django battle-test where ``max_depth=3`` produced
        332KB / 664 callers / 848 calls_into. Counts and the ``count``
        fields are always exact regardless of pagination.

        v0.9 P3: ``min_weight`` (default 0.6) drops the resolver
        fan-out edges (weight 0.5 — short-name collisions without
        scope match). Surfaced by Django battle-test where
        ``calls_into`` reported ~70 symbols vs ~10 actual. Pass
        ``min_weight=0.0`` for legacy unfiltered behavior.
        """
        st = get_state(workspace)
        pid = st.project_id
        view = load_graph(st.conn, pid)

        def specs_for_symbols(ids: set[int]) -> list[dict]:
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            return [
                dict(r)
                for r in st.conn.execute(
                    f"""SELECT DISTINCT r.spec_id, r.title, r.status, r.priority
                        FROM spec_symbol rs JOIN spec r ON r.id=rs.spec_id
                        WHERE rs.symbol_id IN ({placeholders})""",
                    list(ids),
                ).fetchall()
            ]

        def _paginate_meta(ids: set[int]) -> tuple[list[dict], int, int | None]:
            """Sort + slice. Returns (page, total, next_cursor)."""
            sorted_meta = sorted(
                (view.sym_meta[i] for i in ids if i in view.sym_meta),
                key=lambda m: (m.get("file_path", ""), m.get("start_line", 0)),
            )
            total = len(sorted_meta)
            page = sorted_meta[cursor : cursor + limit]
            next_c = cursor + limit if cursor + limit < total else None
            return page, total, next_c

        if target_type == "symbol":
            sym = _resolve_symbol(st.conn, pid, target)
            if not sym:
                return symbol_not_found_error(st.conn, pid, target)
            sid = int(sym["id"])
            impacted = (
                ancestors_within(view.g, sid, max_depth, min_weight=min_weight)
                if sid in view.g
                else set()
            )
            forward = (
                descendants_within(view.g, sid, max_depth, min_weight=min_weight)
                if sid in view.g
                else set()
            )
            if summary_only:
                return {
                    "root": sym["qualified_name"],
                    "counts": {
                        "impacted_callers": len(impacted),
                        "calls_into": len(forward),
                        "affected_specs": len(specs_for_symbols(impacted | {sid})),
                    },
                }
            callers_page, callers_total, callers_next = _paginate_meta(impacted)
            calls_page, calls_total, calls_next = _paginate_meta(forward)
            warn = _payload_warning(
                max(callers_total, calls_total),
                limit=limit,
                summary_only=summary_only,
            )
            return _attach_payload_warning(
                {
                    "root": sym["qualified_name"],
                    "impacted_callers": callers_page,
                    "calls_into": calls_page,
                    "affected_specs": specs_for_symbols(impacted | {sid}),
                    "counts": {
                        "impacted_callers": callers_total,
                        "calls_into": calls_total,
                    },
                    "next_cursor": callers_next if callers_next is not None else calls_next,
                },
                warn,
            )
        if target_type == "file":
            sids = [
                int(r["id"])
                for r in st.conn.execute(
                    """SELECT s.id FROM symbol s JOIN file f ON f.id=s.file_id
                       WHERE f.project_id=? AND f.path=?""",
                    (pid, target),
                )
            ]
            if not sids:
                return mcp_error(
                    f"File '{target}' not indexed",
                    hint="run `index_project()` to (re-)index the workspace",
                )
            impacted: set[int] = set()
            for sid in sids:
                if sid in view.g:
                    impacted |= ancestors_within(
                        view.g, sid, max_depth, min_weight=min_weight
                    )
            impacted -= set(sids)
            if summary_only:
                return {
                    "file": target,
                    "symbols_in_file": len(sids),
                    "counts": {
                        "impacted_callers": len(impacted),
                        "affected_specs": len(
                            specs_for_symbols(impacted | set(sids))
                        ),
                    },
                }
            callers_page, callers_total, callers_next = _paginate_meta(impacted)
            return _attach_payload_warning(
                {
                    "file": target,
                    "symbols_in_file": len(sids),
                    "impacted_callers": callers_page,
                    "affected_specs": specs_for_symbols(impacted | set(sids)),
                    "counts": {"impacted_callers": callers_total},
                    "next_cursor": callers_next,
                },
                _payload_warning(
                    callers_total, limit=limit, summary_only=summary_only
                ),
            )
        if target_type == "spec":
            spec = st.conn.execute(
                "SELECT id, spec_id FROM spec WHERE project_id=? AND spec_id=?", (pid, target)
            ).fetchone()
            if not spec:
                return mcp_error(
                    f"Spec '{target}' not found",
                    hint="check `list_specs()` for known Spec ids",
                )

            # v0.5 P2: include backward Specs in the dependency graph (Specs that
            # require / extend this one). A change to SPEC-001 ripples to SPEC-042
            # if SPEC-042 requires SPEC-001. Walk spec_dependency backward.
            dependent_spec_ids: set[int] = set()
            frontier = [int(spec["id"])]
            while frontier:
                cur_id = frontier.pop()
                for r in st.conn.execute(
                    "SELECT parent_spec_id FROM spec_dependency WHERE child_spec_id=?",
                    (cur_id,),
                ):
                    pid_dep = int(r["parent_spec_id"])
                    if pid_dep in dependent_spec_ids:
                        continue
                    dependent_spec_ids.add(pid_dep)
                    frontier.append(pid_dep)

            # All Spec ids whose impact contributes to this analysis: target +
            # the set of Specs that transitively depend on it (cascade).
            all_spec_ids = {int(spec["id"])} | dependent_spec_ids
            placeholders = ",".join("?" * len(all_spec_ids))
            sid_rows = st.conn.execute(
                f"SELECT DISTINCT symbol_id FROM spec_symbol WHERE spec_id IN ({placeholders})",
                list(all_spec_ids),
            ).fetchall()
            sids = [int(r["symbol_id"]) for r in sid_rows]

            if not sids:
                return {
                    "spec_id": spec["spec_id"],
                    "warning": "Spec (and its dependents) have no linked symbols",
                    "implementing_symbols": [],
                    "dependent_specs": [],
                }
            forward: set[int] = set()
            backward: set[int] = set()
            for sid in sids:
                if sid in view.g:
                    forward |= descendants_within(view.g, sid, max_depth)
                    backward |= ancestors_within(view.g, sid, max_depth)

            dep_spec_meta: list[dict[str, Any]] = []
            if dependent_spec_ids:
                dep_placeholders = ",".join("?" * len(dependent_spec_ids))
                dep_spec_meta = [
                    dict(r)
                    for r in st.conn.execute(
                        f"""SELECT spec_id, title, status, priority FROM spec
                            WHERE id IN ({dep_placeholders})""",
                        list(dependent_spec_ids),
                    )
                ]

            return {
                "spec_id": spec["spec_id"],
                "dependent_specs": dep_spec_meta,
                "implementing_symbols": [view.sym_meta[n] for n in sids if n in view.sym_meta],
                "downstream": [view.sym_meta[n] for n in forward if n in view.sym_meta],
                "upstream_callers": [view.sym_meta[n] for n in backward if n in view.sym_meta],
            }
        return mcp_error(
            f"Unknown target_type '{target_type}'",
            hint="target_type must be one of: 'symbol', 'file', 'spec'",
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_project_overview(
        include_infrastructure: bool = False,
        include_structural_patterns: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """High-level snapshot: languages, modules, top symbols by PageRank, Spec coverage.

        By default the top-symbols list filters out:
        - infrastructure noise (DI helpers, FastMCP `register` outer fns,
          dunders, one-line wrappers). Pass `include_infrastructure=True`
          to see the unfiltered ranking.
        - structural-pattern names (short name appearing in ≥3 distinct
          files: `.get`, `add_parser`, `run`, `__init__`, `from_dict`,
          etc.). PageRank correctly identifies them as central but they
          carry near-zero "what is this codebase about" signal. Pass
          `include_structural_patterns=True` to keep them. The names
          actually filtered come back in `structural_patterns_filtered`.""" + WORKSPACE_DOCSTRING_NOTE
        return compute_project_overview(
            get_state(workspace),
            include_infrastructure,
            include_structural_patterns,
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_dead_code(
        include_infrastructure: bool = False,
        include_public: bool = False,
        include_non_python: bool = False,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Symbols with zero callers and zero Spec links — removal candidates.

        Filters out, by default:
        - Files under `tests/`, `scripts/`, `bin/`; `__main__.py`; `manage.py`
        - Bundler/build output dirs (`_fresh/`, `dist/`, `build/`, `.next/`,
          `out/`, `node_modules/`, `.svelte-kit/`, `target/`, `__pycache__/`,
          `.turbo/`, `.vite/`, `.cache/`, `.parcel-cache/`) and minified
          artifacts (`*.min.js`, `*.bundle.js`). v0.11 P0 (bug #18).
        - Infrastructure (DI helpers, dunders, FastMCP `register` fns, ≤4-line
          wrappers). Pass `include_infrastructure=True` to keep them.
        - **Public symbols** (Rust `pub`/`pub(crate)`, TS/JS `exported`,
          Java/PHP `public`). They have potential callers from outside the
          indexed crate/package. Pass `include_public=True` to surface them.
        - **Non-Python files** (v0.9 P4). The module-level reference
          scanner is Python-only, so JS/Go/Java/Rust symbols can be
          flagged dead just because their callers are in non-Python
          callsites the scanner can't read. Surfaced by Django where
          70+ vendored xregexp.js helpers were over-reported. Pass
          `include_non_python=True` to surface them.
        - **TS framework filesystem-routing files** (v0.11 P1, bug #19).
          Functions/classes in Fresh ``islands/``, Next.js ``pages/`` /
          ``app/``, SvelteKit ``routes/``, and Remix ``app/routes/`` are
          reachable via filesystem routing, not call edges. Skipped by
          default; pass `include_infrastructure=True` to surface them.

        v0.7 (B3): paginated. `limit` (default 200) caps `dead_symbols` per
        call; `cursor` resumes from a previous call's `next_cursor`;
        `summary_only=True` returns just the count + breakdown without the
        list. The total count is always exact, regardless of pagination.

        v0.7 (B4): visibility-aware. The 23K dead-flagged symbols on the
        a large Rust monorepo Rust monorepo dropped to a manageable list once `pub` items
        were skipped — they have callers across crate boundaries that the
        in-project graph can't see.

        Useful sanity check before a refactor: anything in the result is
        unreachable from in-project callers AND not traceably implementing
        any Spec AND not exposed publicly.
        """
        st = get_state(workspace)
        pid = st.project_id
        rows = st.conn.execute(
            """SELECT s.id, s.qualified_name, s.name, s.kind, s.decorators,
                      s.visibility, s.start_line, s.end_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=?
                 AND NOT EXISTS (
                   SELECT 1 FROM symbol_edge e WHERE e.dst_symbol_id=s.id
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM spec_symbol rs WHERE rs.symbol_id=s.id
                 )
               ORDER BY f.path, s.start_line""",
            (pid,),
        ).fetchall()

        def is_entry_point_path(p: str) -> bool:
            return (
                p.startswith(("tests/", "bin/", "scripts/"))
                or "/tests/" in p
                or "/bin/" in p
                or "/scripts/" in p
                or p.endswith("/__main__.py")
                or p == "__main__.py"
                or p.endswith("/manage.py")
                or p == "manage.py"
            )

        # v0.7 B4: visibility values that imply external callers
        _PUBLIC_VIS = {"pub", "exported", "public"}
        # `pub(crate)` / `pub(super)` are NOT skipped — those symbols are
        # only callable within this indexed scope, so absence of in-project
        # callers IS a real dead-code signal.

        # v0.8 P2 sessions 02 fix (bug #6 cross-file refs): build a UNION
        # of module-level referenced names across all .py files in the
        # project. Closes the gap where a class is defined in module A
        # but registered with the framework in module B (e.g.
        # `mcp.add_middleware(AgentLogMiddleware())` in server.py vs the
        # class def in instrumentation.py). False-skip risk is bounded
        # because we only protect symbols whose SHORT name appears in
        # any module-level ref position — a cross-file collision still
        # has to share that name. Empty for projects with no .py files.
        #
        # Plus per-file: nested-def closure callbacks (`def _foo():` inside
        # a function whose name is then passed as `cb=_foo` to a
        # constructor) — needs to be per-file because nested-fn names like
        # `_do_reindex` are intentionally local and would otherwise have
        # global-name false-skip risk.
        global_module_refs: set[str] = set()
        nested_uses_by_file: dict[str, frozenset[str]] = {}
        # v0.13 P0: aliases assigned to entry-point decorator factories
        # (`agentic_tool = mcp.tool if X else _noop`). Project-wide union —
        # aliases may be imported into the file that uses them.
        decorator_aliases: set[str] = set()
        workspace_path = st.settings.workspace
        for path_row in st.conn.execute(
            "SELECT f.path FROM file f WHERE f.project_id=? AND f.path LIKE '%.py'",
            (pid,),
        ):
            try:
                abs_path = str(workspace_path / path_row["path"])
                global_module_refs |= _module_level_referenced_names(abs_path)
                decorator_aliases |= _entry_point_decorator_aliases(abs_path)
                # v0.10: explicit public-surface markers (re-exports +
                # __all__) protect library-side classes that have no
                # in-tree caller because their callers are user code.
                global_module_refs |= _publicly_exported_names(abs_path)
                # v0.11 P3: runtime-registration patterns — class/fn passed
                # to a framework method so the framework calls it later.
                # Covers Field.register_lookup(MyLookup), signal.connect(h),
                # app.add_middleware(M), etc.
                global_module_refs |= _runtime_registered_names(abs_path)
                nested_uses = _used_nested_def_names(abs_path)
                if nested_uses:
                    nested_uses_by_file[path_row["path"]] = nested_uses
            except Exception:
                # Bad file paths shouldn't kill the whole audit.
                continue

        # v0.13 P3: TS/JS runtime-registration scan. v0.14: plus the
        # closure-capture scan (nested fn referenced in parent body) for
        # TS/JS/TSX and Rust — Go has no named nested fns, nothing to scan.
        # Only pay the file reads when non-Python symbols are in scope.
        if include_non_python:
            for path_row in st.conn.execute(
                """SELECT path, language FROM file
                   WHERE project_id=? AND language IN
                     ('typescript', 'javascript', 'tsx', 'rust')""",
                (pid,),
            ):
                try:
                    abs_path = str(workspace_path / path_row["path"])
                    lang = path_row["language"]
                    if lang != "rust":
                        global_module_refs |= _ts_runtime_registered_names(abs_path, lang)
                    nested_uses = _treesitter_used_nested_def_names(abs_path, lang)
                    if nested_uses:
                        nested_uses_by_file[path_row["path"]] = nested_uses
                except Exception:
                    continue

        # v0.8 P2 sessions 02 fix (bug #6 method propagation): a class
        # whose CONSTRUCTOR is called from anywhere in the indexed code
        # has its methods reachable through duck-typing (FastMCP middleware
        # hooks, ABCs, plugin patterns). Pre-compute the set of classes
        # with at least one inbound edge — methods of those classes
        # should not be dead-flagged even if their own callers are zero.
        # Augment with classes whose name appears in `global_module_refs`
        # (covers the constructor-call-in-arg-position case the extractor
        # doesn't capture as an edge).
        protected_class_qnames = {
            r["qualified_name"]
            for r in st.conn.execute(
                """SELECT DISTINCT s.qualified_name FROM symbol s
                   JOIN file f ON f.id=s.file_id
                   WHERE f.project_id=? AND s.kind='class'
                     AND EXISTS (
                       SELECT 1 FROM symbol_edge e WHERE e.dst_symbol_id=s.id
                     )""",
                (pid,),
            )
        }

        # v0.13 P2: Angular-decorated classes. Template-bound decorators
        # (Component/Directive/Pipe) protect EVERY method — templates the
        # indexer can't parse may call any of them. Any Angular decorator
        # protects the lifecycle hooks by name.
        ng_template_classes: set[str] = set()
        ng_any_classes: set[str] = set()
        for r in st.conn.execute(
            """SELECT s.qualified_name, s.decorators FROM symbol s
               JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.kind='class'
                 AND s.decorators IS NOT NULL""",
            (pid,),
        ):
            try:
                segs = {_decorator_lastseg(d) for d in json.loads(r["decorators"])}
            except (json.JSONDecodeError, TypeError):
                continue
            if segs & _NG_TEMPLATE_DECORATOR_LASTSEGS:
                ng_template_classes.add(r["qualified_name"])
            if segs & _NG_ANY_DECORATOR_LASTSEGS:
                ng_any_classes.add(r["qualified_name"])

        alias_lastsegs = frozenset(decorator_aliases)
        filtered: list[dict[str, Any]] = []
        for r in rows:
            meta = dict(r)
            if is_entry_point_path(meta["file_path"]):
                continue
            if _is_bundler_output_path(meta["file_path"]):
                continue
            # v0.11 P1 (bug #19): TS framework filesystem-routing entry points.
            # Fresh islands, Next.js pages/app, SvelteKit routes, Remix routes
            # are reachable via path conventions — they have zero call edges
            # by design but are never dead.
            if not include_infrastructure and _is_ts_framework_entry_point(meta):
                continue
            if not include_non_python and not meta["file_path"].endswith(".py"):
                continue
            if not include_infrastructure and _is_implicit_entry_point(meta):
                continue
            if not include_infrastructure and _has_entry_point_decorator(
                meta.get("decorators"), alias_lastsegs
            ):
                continue
            # v0.13 P0: the symbol itself is decorator machinery (an alias
            # target or an IfExp branch like `_noop_decorator`).
            if not include_infrastructure and meta["name"].lower() in alias_lastsegs:
                continue
            if not include_public and (meta.get("visibility") in _PUBLIC_VIS):
                continue

            # v0.8 P2 sessions 02 fix (bugs #4 #5 #6): symbol is referenced
            # at module level somewhere in the project — covers `__main__`
            # guard calls, dispatch-table fn refs (MIGRATIONS list), and
            # cross-file framework registration like
            # `mcp.add_middleware(MyMiddleware())` in server.py vs the
            # class def in instrumentation.py. For class methods, the
            # parent class either appears in module-level refs OR has
            # inbound edges in the call graph.
            if not include_infrastructure:
                qname_parts = meta["qualified_name"].split(".")
                if meta["name"] in global_module_refs:
                    continue
                # v0.8 P2 fix #11: nested-fn closure callback. A function
                # defined inside another function whose name is referenced
                # within the parent's body (e.g. `Watcher(on_reindex=_do)`)
                # is reachable as a callback even with zero call-edges.
                # Per-file lookup so nested names don't cross-collide.
                file_nested = nested_uses_by_file.get(meta["file_path"])
                if file_nested and meta["name"] in file_nested:
                    continue
                if meta["kind"] == "method" and len(qname_parts) >= 2:
                    parent_class_short = qname_parts[-2]
                    if parent_class_short in global_module_refs:
                        continue
                    parent_class_qname = ".".join(qname_parts[:-1])
                    if parent_class_qname in protected_class_qnames:
                        continue
                    # v0.13 P2: Angular template reachability + lifecycle.
                    if parent_class_qname in ng_template_classes:
                        continue
                    if (
                        meta["name"] in _NG_LIFECYCLE_HOOKS
                        and parent_class_qname in ng_any_classes
                    ):
                        continue

            filtered.append(meta)

        total = len(filtered)
        # by_kind / by_dir breakdowns (cheap; useful for summary mode)
        by_kind: dict[str, int] = {}
        by_dir: dict[str, int] = {}
        for m in filtered:
            by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
            top_dir = m["file_path"].split("/", 1)[0]
            by_dir[top_dir] = by_dir.get(top_dir, 0) + 1

        if summary_only:
            return {
                "count": total,
                "by_kind": by_kind,
                "by_top_dir": by_dir,
            }

        page = filtered[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < total else None
        return {
            "count": total,
            "by_kind": by_kind,
            "by_top_dir": by_dir,
            "dead_symbols": [
                {
                    "qualified_name": m["qualified_name"],
                    "kind": m["kind"],
                    "file_path": m["file_path"],
                    "start_line": m["start_line"],
                    "end_line": m["end_line"],
                }
                for m in page
            ],
            "next_cursor": next_cursor,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_endpoints(
        framework: Literal[
            "flask", "fastapi", "click", "pytest", "fastmcp", "celery", "django",
            "nextjs", "fresh", "sveltekit", "remix", "spring", "angular",
            "hono",
        ] | None = None,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Symbols decorated with framework entry-point markers.

        Useful as a reverse-engineering aid: "what HTTP routes does this app
        expose?", "what CLI commands does this script support?", "which
        pytest fixtures live in this repo?".

        Pass `framework=None` (default) to surface every recognized
        entry-point decorator across the project. Pass a specific framework
        to filter to its decorator set (matched against the LAST dotted
        segment of each decorator, so aliasing like `from flask import Flask
        as App; @App().route(...)` still resolves).

        v0.11 P1: ``framework='fresh'``, ``'nextjs'``, ``'sveltekit'``,
        ``'remix'`` (or ``None``) surfaces symbols in filesystem-routing
        files: Fresh ``islands/``, Next.js ``pages/`` and ``app/``,
        SvelteKit ``routes/``, Remix ``app/routes/``. Detection is
        path-based (no decorator needed).

        v0.9 P5: when ``framework='django'`` (or ``None``), classes that
        inherit from Django class-based view bases or auth mixins
        (LoginView/LogoutView/View/TemplateView/ListView/DetailView/
        FormView/CreateView/UpdateView/DeleteView/RedirectView/
        LoginRequiredMixin/PermissionRequiredMixin/UserPassesTestMixin/
        MiddlewareMixin) are also surfaced even when they have no
        decorator. Closes session-04 bug #15.

        v0.13 P2: ``framework='spring'`` surfaces Java Spring Boot
        annotations (@RestController, @GetMapping & friends, requires the
        v8 re-extract); ``framework='angular'`` surfaces @Component /
        @Injectable / @Directive / @Pipe / @NgModule classes.

        v0.13 P3: ``framework='hono'`` scans indexed TS/JS files whose
        source mentions Hono for call-style route registrations
        (``app.get('/users', handler)``, ``app.on(...)``,
        ``app.route('/api', sub)``). Each route reports ``hono_method`` /
        ``hono_path``; ``qualified_name`` resolves to the handler symbol
        when the handler is a named identifier. Explicit-opt-in only (not
        part of the ``framework=None`` sweep — it reads files on demand).
        """
        st = get_state(workspace)

        endpoints = filter_api_endpoints(
            compute_endpoints(st, framework),
            framework,
            exclude_tests=True,
        )
        total = len(endpoints)
        if summary_only:
            return {"framework": framework, "count": total}
        page = endpoints[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < total else None
        return {
            "framework": framework,
            "endpoints": page,
            "count": total,
            "next_cursor": next_cursor,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def audit_coverage(
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Spec coverage audit: what's missing / under-confident.

        Six signals:
        - `modules_without_spec`: files whose symbols have no DIRECT `spec_symbol` link
        - `modules_implicitly_covered`: subset of `modules_without_spec` whose
          symbols are called transitively by a spec-linked symbol — covered
          indirectly through the call graph (e.g. a data layer reached via
          API handlers that carry the `@spec:` annotation)
        - `modules_truly_orphan`: subset of `modules_without_spec` with NO direct
          link AND no transitive coverage — the actually-actionable list
        - `modules_unsupported_language`: files in languages whose extractor
          does not yet read in-source `@spec:` annotations (everything outside
          Python / JS / TS today). Listed separately so they aren't reported
          as orphans — the gap is in the extractor, not the project.
        - `specs_without_implementation`: Specs with no `spec_symbol` row at all
        - `specs_low_confidence`: Specs whose avg(spec_symbol.confidence) < 0.7
          (typically means only verb-anchored matches, no `@spec:` annotation)
        - `spec_test_coverage` (v0.8 P2 fix #9): Specs that have ≥1 `relation='tests'`
          link, with the count. Use this to spot Specs implemented but not
          tested (Spec in this list with low test_count → coverage gap).
        - `spec_coverage` (v0.15): per-Spec AUTO-DERIVED test coverage from the
          call graph. Each entry is `{spec_id, title, test_coverage_ratio,
          tested_symbols, total_symbols, coverage_source}`. A Spec's
          `implements` symbol counts as tested when a test symbol reaches it
          within 3 call-graph hops (derived) OR it carries an explicit
          `relation='tests'` link (explicit); `coverage_source`
          ∈ {derived, explicit, both, none}. No hand-linking required —
          this is the differentiator over the explicit-only `spec_test_coverage`
          above. Rollups `avg_test_coverage` and `specs_with_any_test_coverage`
          (Specs with ratio>0) are also in `counts`.

        v0.7 (B3): paginated. `limit` (default 200) caps each list per
        call; `cursor` resumes; `summary_only=True` returns only the
        counts. Counts are always exact regardless of pagination.

        v0.8 P2 fix #8: package-marker files (`__init__.py`,
        `package-info.java`, `mod.rs`) are auto-excluded from
        `modules_without_spec` — `@spec:` annotations on a no-op import
        marker would never be the right place anyway.
        """
        st = get_state(workspace)

        # v0.7 B3: pagination over the shared compute helper.
        cov = compute_coverage(st)
        counts = cov["counts"]
        modules_no_spec = cov["modules_without_spec"]
        modules_implicit = cov["modules_implicitly_covered"]
        modules_truly_orphan = cov["modules_truly_orphan"]
        modules_unsupported_language = cov["modules_unsupported_language"]
        specs_no_impl = cov["specs_without_implementation"]
        specs_low_conf = cov["specs_low_confidence"]
        spec_test_coverage = cov["spec_test_coverage"]
        spec_coverage = cov["spec_coverage"]
        if summary_only:
            return {"counts": counts}

        def _page(items: list, c: int = cursor, n: int = limit) -> tuple[list, int | None]:
            sliced = items[c : c + n]
            nxt = c + n if c + n < len(items) else None
            return sliced, nxt

        mw_p, mw_next = _page(modules_no_spec)
        mi_p, mi_next = _page(modules_implicit)
        mt_p, mt_next = _page(modules_truly_orphan)
        mu_p, mu_next = _page(modules_unsupported_language)
        specn_p, specn_next = _page(specs_no_impl)
        specl_p, specl_next = _page(specs_low_conf)
        spectc_p, spectc_next = _page(spec_test_coverage)
        speccov_p, speccov_next = _page(spec_coverage)
        return {
            "counts": counts,
            "modules_without_spec": mw_p,
            "modules_implicitly_covered": mi_p,
            "modules_truly_orphan": mt_p,
            "modules_unsupported_language": mu_p,
            "specs_without_implementation": specn_p,
            "specs_low_confidence": specl_p,
            "spec_test_coverage": spectc_p,
            "spec_coverage": speccov_p,
            "avg_test_coverage": cov["avg_test_coverage"],
            "specs_with_any_test_coverage": cov["specs_with_any_test_coverage"],
            "next_cursor": {
                "modules_without_spec": mw_next,
                "modules_implicitly_covered": mi_next,
                "modules_truly_orphan": mt_next,
                "modules_unsupported_language": mu_next,
                "specs_without_implementation": specn_next,
                "specs_low_confidence": specl_next,
                "spec_test_coverage": spectc_next,
                "spec_coverage": speccov_next,
            },
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_orphan_tests(
        max_depth: int = 10,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Test functions whose descendant cone never reaches production code.

        Heuristic: any function/method in a `tests/` folder (or matching
        `*_test.*` / `test_*.*` naming) whose forward call graph contains
        zero non-test symbols. Either disconnected fixtures, helpers used only
        by other tests, or actually orphaned tests.

        v0.7 (B3): paginated. `limit`/`cursor`/`summary_only` work as in
        find_dead_code.

        v0.14: the payload carries a ``caveat`` field. The forward call
        graph is static; tests that drive production code through an
        indirection the analyzer can't see — most commonly an in-process
        MCP/RPC harness like FastMCP ``Client(mcp)`` that dispatches tool
        handlers by string name — have a descendant cone that never
        reaches production symbols and are over-reported here. Treat the
        count as an upper bound, not a verdict.
        """
        st = get_state(workspace)
        pid = st.project_id
        view = load_graph(st.conn, pid)
        _ORPHAN_CAVEAT = (
            "Count may include false positives: tests that exercise "
            "production code through an indirection the static call graph "
            "can't follow (e.g. an in-process MCP/RPC harness like FastMCP "
            "Client(mcp) that dispatches handlers by string name) reach zero "
            "production symbols in the cone and are reported as orphan. "
            "Treat this as an upper bound."
        )

        def is_test_path(p: str) -> bool:
            base = p.rsplit("/", 1)[-1]
            return (
                p.startswith("tests/")
                or "/tests/" in p
                or base.startswith("test_")
                or base.endswith("_test.py")
                or base.endswith("_test.go")
                or "_test." in base
            )

        test_rows = st.conn.execute(
            """SELECT s.id, s.qualified_name, s.kind, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.kind IN ('function', 'method')""",
            (pid,),
        ).fetchall()
        test_syms = [dict(r) for r in test_rows if is_test_path(r["file_path"])]

        orphans: list[dict[str, Any]] = []
        for r in test_syms:
            sid = int(r["id"])
            descendants = (
                descendants_within(view.g, sid, max_depth) if sid in view.g else set()
            )
            reaches_prod = False
            for did in descendants:
                meta = view.sym_meta.get(did)
                if meta and not is_test_path(meta.get("file_path", "")):
                    reaches_prod = True
                    break
            if not reaches_prod:
                orphans.append({
                    "qualified_name": r["qualified_name"],
                    "file_path": r["file_path"],
                    "kind": r["kind"],
                    "reason": (
                        "no outgoing calls" if not descendants
                        else "descendant cone never escapes test files"
                    ),
                })
        total = len(orphans)
        if summary_only:
            return {"count": total, "caveat": _ORPHAN_CAVEAT}
        page = orphans[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < total else None
        return {
            "orphan_tests": page,
            "count": total,
            "next_cursor": next_cursor,
            "caveat": _ORPHAN_CAVEAT,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def git_diff_impact(
        base_ref: str = "HEAD~1",
        head_ref: str = "HEAD",
        max_depth: int = 5,
        impacted_limit: int = 200,
        impacted_cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Topological impact of a git diff: changed files -> Specs + callers + suggested tests.

        The CI/PR-review entry point. Given a base..head git range, this tool:
        1. lists changed files via `git diff --name-only`
        2. resolves each one against the indexed symbols
        3. unions the backward cone of callers across them
        4. unions the affected Specs
        5. suggests test files: any file under `tests/` (or `*_test.*`) whose
           symbols call any impacted symbol — those are likely to break.

        Returns an empty result with `error` if either ref is unknown to git.
        Run `index_project` first if results look stale.

        v0.7 (B3): paginated. `impacted_limit` (default 200) caps the
        `impacted_callers` list — the unbounded cone was the cause of
        7M-char payloads on Rust monorepos. `summary_only=True` returns
        counts + the small lists (changed_files, affected_specs,
        suggested_tests) without `changed_symbols` or `impacted_callers`.
        """
        st = get_state(workspace)
        pid = st.project_id
        ws_root = str(st.settings.workspace)

        # v0.16 A: shared git-diff core (also feeds compute_diff_spec_impact).
        changed_paths, err = _git_diff_changed_files(ws_root, base_ref, head_ref)
        if err is not None:
            return err
        if not changed_paths:
            empty: dict[str, Any] = {
                "base_ref": base_ref,
                "head_ref": head_ref,
                "changed_files": [],
                "changed_files_indexed": [],
                "changed_files_unindexed": [],
                "affected_specs": [],
                "suggested_tests": [],
                "counts": {"impacted_callers": 0, "changed_symbols": 0},
            }
            if not summary_only:
                empty["changed_symbols"] = []
                empty["impacted_callers"] = []
                empty["next_cursor"] = None
            return empty

        view = load_graph(st.conn, pid)

        # Resolve changed files to indexed symbol ids
        changed_sym_ids: set[int] = set()
        changed_symbol_meta: list[dict[str, Any]] = []
        indexed_paths: set[str] = set()
        for path in changed_paths:
            rows = st.conn.execute(
                """SELECT s.id, s.qualified_name, s.kind, s.start_line, s.end_line
                   FROM symbol s JOIN file f ON f.id = s.file_id
                   WHERE f.project_id=? AND f.path=?""",
                (pid, path),
            ).fetchall()
            if rows:
                indexed_paths.add(path)
            for r in rows:
                sid = int(r["id"])
                changed_sym_ids.add(sid)
                changed_symbol_meta.append({
                    "id": sid,
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file_path": path,
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                })

        # Backward cone: every symbol that transitively calls a changed symbol
        impacted: set[int] = set()
        for sid in changed_sym_ids:
            if sid in view.g:
                impacted |= ancestors_within(view.g, sid, max_depth)
        impacted -= changed_sym_ids

        # Affected Specs: any spec_symbol whose symbol_id is in changed | impacted
        all_touched = changed_sym_ids | impacted
        affected_specs: list[dict[str, Any]] = []
        if all_touched:
            placeholders = ",".join("?" * len(all_touched))
            for r in st.conn.execute(
                f"""SELECT DISTINCT r.spec_id, r.title, r.status, r.priority
                    FROM spec_symbol rs JOIN spec r ON r.id = rs.spec_id
                    WHERE rs.symbol_id IN ({placeholders})""",
                list(all_touched),
            ):
                affected_specs.append(dict(r))

        # Suggested tests: files under a tests/ folder OR matching *_test.* /
        # test_*.* whose symbols are in `impacted` (i.e. test functions that call
        # something we touched). v0.8 P2 session-02 fix (bug #7): exclude
        # tests/fixtures/, tests/data/, tests/_*.py — fixtures and helpers
        # are not test runners. Require basename to match test_*.py /
        # *_test.{py,ts,tsx,js,go,rs,...} when the file is inside tests/.
        def _looks_like_test_file(path: str) -> bool:
            base = path.rsplit("/", 1)[-1]
            in_tests_tree = path.startswith("tests/") or "/tests/" in path
            # Fixtures / data directories: never test runners.
            if "/fixtures/" in path or path.startswith("fixtures/"):
                return False
            if "/__fixtures__/" in path or "/data/" in path:
                return False
            # Must be a recognizable test file by name.
            if base.startswith("test_") and "." in base:
                return True
            if "_test." in base:
                return True
            if base.startswith("test.") or ".test." in base:
                return True
            # Inside tests/ but unrecognizable name (e.g. `helpers.py`,
            # `conftest.py` — conftest is pytest infra, not a runner)
            # → not a suggested test.
            return False and in_tests_tree  # explicit: never default-true

        suggested_tests_set: set[str] = set()
        if all_touched:
            placeholders = ",".join("?" * len(all_touched))
            for r in st.conn.execute(
                f"""SELECT DISTINCT f.path FROM symbol_edge e
                    JOIN symbol s ON s.id = e.src_symbol_id
                    JOIN file f ON f.id = s.file_id
                    WHERE f.project_id=? AND e.dst_symbol_id IN ({placeholders})""",
                [pid, *list(all_touched)],
            ):
                if _looks_like_test_file(r["path"]):
                    suggested_tests_set.add(r["path"])
        suggested_tests = sorted(suggested_tests_set)

        impacted_meta = [view.sym_meta[n] for n in impacted if n in view.sym_meta]
        counts = {
            "changed_files": len(changed_paths),
            "changed_symbols": len(changed_symbol_meta),
            "impacted_callers": len(impacted_meta),
            "affected_specs": len(affected_specs),
            "suggested_tests": len(suggested_tests),
        }
        base = {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "changed_files": changed_paths,
            "changed_files_indexed": sorted(indexed_paths),
            "changed_files_unindexed": sorted(set(changed_paths) - indexed_paths),
            "affected_specs": affected_specs,
            "suggested_tests": suggested_tests,
            "counts": counts,
        }
        if summary_only:
            return base
        page = impacted_meta[impacted_cursor : impacted_cursor + impacted_limit]
        next_cursor = (
            impacted_cursor + impacted_limit
            if impacted_cursor + impacted_limit < len(impacted_meta)
            else None
        )
        base["changed_symbols"] = changed_symbol_meta
        base["impacted_callers"] = page
        base["next_cursor"] = next_cursor
        return _attach_payload_warning(
            base,
            _payload_warning(
                len(impacted_meta),
                limit=impacted_limit,
                summary_only=summary_only,
            ),
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def grep_in_indexed_files(
        pattern: str,
        path_glob: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        cursor: int = 0,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Search only indexed files (SQLite ``file`` table) on disk.

        ``pattern`` is tried as a regex first; invalid regex falls back to
        literal substring match. ``path_glob`` uses shell-style globs on the
        indexed relative path (e.g. ``src/**/*.py``). ``kind`` filters the
        indexed ``language`` column (e.g. ``python``). Results are paginated
        with ``limit`` (default 50) and ``cursor``.
        """
        st = get_state(workspace)
        return _grep_indexed_files_core(
            st, pattern, path_glob, kind, limit, cursor
        )

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def agent_scratch(
        qname: str,
        note: str,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Store or update a short agent note keyed by symbol qualified name."""
        st = get_state(workspace)
        st.conn.execute(
            """INSERT INTO agent_scratch (project_id, qname, note, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(project_id, qname) DO UPDATE SET
                 note=excluded.note,
                 updated_at=datetime('now')""",
            (st.project_id, qname, note),
        )
        st.conn.commit()
        return {"qname": qname, "note": note, "saved": True}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def agent_scratch_clear(
        qname: str | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Delete agent scratch notes for the active project.

        Pass ``qname`` to clear one note; omit to clear all notes for the project.
        """
        st = get_state(workspace)
        if qname:
            cur = st.conn.execute(
                "DELETE FROM agent_scratch WHERE project_id=? AND qname=?",
                (st.project_id, qname),
            )
        else:
            cur = st.conn.execute(
                "DELETE FROM agent_scratch WHERE project_id=?",
                (st.project_id,),
            )
        st.conn.commit()
        return {"cleared": cur.rowcount, "qname": qname}

