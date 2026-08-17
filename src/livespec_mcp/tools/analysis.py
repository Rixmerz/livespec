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
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.config import load_repo_config
from livespec_mcp.domain.extractors import (
    HTTP_ROUTE_DECORATOR_LASTSEGS,
    _ts_collect_imports,
    get_parser,
    infer_python_http_framework,
    parse_python_http_route,
    scan_go_routes,
    scan_hono_routes,
    ts_registered_callback_names,
)
from livespec_mcp.domain.graph import (
    GraphView,
    ancestors_within,
    descendants_within,
    graph_pagerank,
    load_graph,
)
from livespec_mcp.domain.indexer import DEFAULT_IGNORES, _hash_bytes, _iter_files
from livespec_mcp.domain.languages import detect_language
from livespec_mcp.domain.legacy_flows import compute_legacy_flows
from livespec_mcp.domain.test_coverage_reports import discover_report_coverage
from livespec_mcp.state import AppState, get_state
from livespec_mcp.tool_params import (
    Cursor,
    Limit,
    MaxDepth,
    MinWeight,
    QName,
    SummaryOnly,
    SymbolQuery,
)
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

_INFRA_NAME_SUFFIXES = ("_state", "_settings", "_config", "_session")

# v0.7 B4: visibility values that imply external callers. `pub(crate)` /
# `pub(super)` are NOT in this set — those symbols are only callable within
# this indexed scope, so absence of in-project callers IS a real dead-code
# signal.
_PUBLIC_VIS = frozenset({"pub", "exported", "public"})

_PAYLOAD_WARN_BYTES = 500 * 1024
_DEFAULT_META_BYTES = 400

# SQLite caps host parameters at 999 (older builds) / 32766 (modern). A depth-5
# caller cone on a huge repo can exceed it, raising a raw OperationalError from
# an `IN (?, ?, ...)` clause. Chunk such queries under a cap safe on every build.
_SQL_IN_CHUNK = 900

# Grep: reject catastrophic regex shapes (Sonar S5852); bound pattern length.
_GREP_PATTERN_MAX = 200
_GREP_LINE_MAX = 4000
_GREP_PER_FILE_DEFAULT = 20
_REDOS_NESTED = re.compile(
    r"\([^)]*[+*][^)]*\)[+*]|\([^)]*[+*][^)]*\|[^)]*[+*][^)]*\)[+*]"
)


def _grep_compile_pattern(pattern: str) -> tuple[re.Pattern[str] | None, str, str | None]:
    """Return (compiled|None, match_mode, error_hint).

    match_mode is ``regex`` or ``literal``. Hostile / overlong patterns fail
    closed to literal with a hint rather than hanging the MCP process.
    """
    if len(pattern) > _GREP_PATTERN_MAX:
        return None, "literal", (
            f"pattern longer than {_GREP_PATTERN_MAX} chars — using literal match"
        )
    if _REDOS_NESTED.search(pattern):
        return None, "literal", (
            "pattern looks ReDoS-prone (nested quantifiers) — using literal match"
        )
    try:
        return re.compile(pattern), "regex", None
    except re.error:
        return None, "literal", None


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards so a literal query matches literally.

    Pair with ``LIKE ? ESCAPE '\\'`` in the SQL. Escapes the backslash first.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _select_in_chunks(
    conn,
    sql_template: str,
    ids,
    *,
    prefix_params: tuple = (),
    suffix_params: tuple = (),
) -> list:
    """Run ``sql_template`` (containing one ``{in}`` marker) over ``ids`` in
    parameter-safe chunks and return all rows. Bound params are ordered
    ``prefix_params + chunk_ids + suffix_params`` for each chunk."""
    ids = list(ids)
    if not ids:
        return []
    out: list = []
    for i in range(0, len(ids), _SQL_IN_CHUNK):
        batch = ids[i : i + _SQL_IN_CHUNK]
        ph = ",".join("?" * len(batch))
        sql = sql_template.replace("{in}", ph)
        out.extend(conn.execute(sql, (*prefix_params, *batch, *suffix_params)).fetchall())
    return out


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


_STALE_SAMPLE = 20  # cap on the path lists attached to a grep result


def _grep_scope_staleness(
    st: AppState,
    indexed_paths: set[str],
    changed: list[str],
    path_glob: str | None,
    kind: str | None,
) -> dict[str, Any]:
    """Freshness verdict for the slice of the repo a grep actually covered.

    `changed` is filled by the caller for free: it already read every in-scope
    file, so re-hashing those bytes costs no extra I/O. The unindexed half is
    NOT free — it walks the workspace exactly the way `index_project` would
    (`_iter_files`: os.walk + a .gitignore read per dir + a stat per candidate),
    because a file that was never indexed cannot be seen by a hash check and is
    the case most likely to hide a match.

    Scope-bound on purpose: it says nothing about files outside `path_glob`/
    `kind`, and it doesn't need to — those can't affect this result.
    """
    ws = st.settings.workspace
    unindexed: list[str] = []
    try:
        on_disk = _iter_files(ws, DEFAULT_IGNORES, load_repo_config(ws))
    except OSError:
        on_disk = []
    for p in on_disk:
        try:
            rel = str(p.relative_to(ws))
        except ValueError:
            continue
        if rel in indexed_paths:
            continue
        if path_glob and not fnmatch.fnmatch(rel, path_glob):
            continue
        if kind and detect_language(p) != kind:
            continue
        unindexed.append(rel)

    out: dict[str, Any] = {"scope_fresh": not changed and not unindexed}
    hints: list[str] = []
    if changed:
        out["stale_files_count"] = len(changed)
        out["stale_files"] = sorted(changed)[:_STALE_SAMPLE]
        hints.append(
            f"{len(changed)} searched file(s) changed on disk since they were "
            "indexed — matches shown come from the current file contents, but "
            "the index no longer describes them"
        )
    if unindexed:
        out["unindexed_files_count"] = len(unindexed)
        out["unindexed_files"] = sorted(unindexed)[:_STALE_SAMPLE]
        hints.append(
            f"{len(unindexed)} file(s) in scope were NEVER indexed and were "
            "therefore NOT searched — matches in them are missing from this result"
        )
    if hints:
        out["hint"] = (
            " | ".join(hints)
            + " | run index_project(workspace=..., force=false) and re-grep"
        )
    return out


def _grep_indexed_files_core(
    st: AppState,
    pattern: str,
    path_glob: str | None,
    kind: str | None,
    limit: int,
    cursor: int,
    *,
    per_file_limit: int = _GREP_PER_FILE_DEFAULT,
    fts_prefilter: bool = False,
) -> dict[str, Any]:
    """Search indexed file contents on disk (substring or regex)."""
    pid = st.project_id
    sql = "SELECT path, language, content_hash FROM file WHERE project_id=?"
    params: list[Any] = [pid]
    if kind:
        sql += " AND language=?"
        params.append(kind)
    # Stable ORDER BY: the cursor slices this list, so an unordered scan
    # (rowid order churns after re-index) would skip/duplicate rows across
    # a paginated walk that spans a reindex.
    sql += " ORDER BY path"
    file_rows = st.conn.execute(sql, params).fetchall()

    regex, match_mode, mode_hint = _grep_compile_pattern(pattern)
    use_regex = regex is not None
    needle = pattern

    fts_paths: set[str] | None = None
    if fts_prefilter:
        from livespec_mcp.domain.rag import fts_search

        # Strip regex metacharacters for a loose FTS probe.
        probe = re.sub(r"[^\w\s\-_.]+", " ", pattern).strip() or pattern
        hits = fts_search(st.conn, pid, probe, limit=500, scope="all")
        fts_paths = {h[2].get("file_path") for h in hits if h[2].get("file_path")}
        # Empty FTS → fall back to full scan (don't hide literal-only matches).
        if not fts_paths:
            fts_paths = None

    matches: list[dict[str, Any]] = []
    indexed_paths: set[str] = set()
    changed_on_disk: list[str] = []
    ws = st.settings.workspace
    for row in file_rows:
        path = row["path"]
        indexed_paths.add(path)
        if path_glob and not fnmatch.fnmatch(path, path_glob):
            continue
        if fts_paths is not None and path not in fts_paths:
            continue
        fp = ws / path
        if not fp.is_file():
            continue
        try:
            raw = fp.read_bytes()
        except OSError:
            continue
        # Free staleness check: the bytes are already in hand, and the indexer
        # hashes the same raw bytes (indexer._hash_bytes on p.read_bytes()).
        # Hashing the decoded text instead would mismatch forever on any file
        # with invalid UTF-8.
        if row["content_hash"] and _hash_bytes(raw) != row["content_hash"]:
            changed_on_disk.append(path)
        lines = raw.decode("utf-8", errors="replace").splitlines()
        file_hits = 0
        for line_no, line in enumerate(lines, start=1):
            if len(line) > _GREP_LINE_MAX:
                line = line[:_GREP_LINE_MAX]
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
            file_hits += 1
            if file_hits >= per_file_limit:
                break

    total = len(matches)
    page = matches[cursor : cursor + limit]
    next_cursor = cursor + limit if cursor + limit < total else None
    out: dict[str, Any] = {
        "pattern": pattern,
        "match_mode": match_mode,
        "matches": page,
        "count": total,
        "next_cursor": next_cursor,
        "per_file_limit": per_file_limit,
        **_grep_scope_staleness(st, indexed_paths, changed_on_disk, path_glob, kind),
    }
    if mode_hint:
        out["hint"] = mode_hint
    if fts_prefilter:
        out["fts_prefilter"] = True
        out["fts_candidate_files"] = len(fts_paths) if fts_paths is not None else None
    return out

# v0.5 P1: framework decorator names that imply hidden callers (HTTP routers,
# CLI dispatchers, test frameworks, plugin systems, message brokers, MCP).
# We match on the LAST dotted segment so `app.route`, `router.get`,
# `bp.before_request`, `mcp.tool` all qualify. Keep this list short and well-
# known; users can opt out via include_infrastructure=True.
_ENTRY_POINT_DECORATOR_LASTSEG = frozenset({
    # HTTP verbs (Flask/FastAPI/Bottle/etc.)
    "route", "get", "post", "put", "delete", "patch", "head", "options",
    "api_route", "websocket",
    # FastAPI lifespan / startup hooks
    "on_event", "lifespan",
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

# Spring DI / lifecycle / messaging — protect from dead-code (via
# `_ENTRY_POINT_DECORATOR_LASTSEG`) but do NOT list as find_endpoints.
# Agents asking "what HTTP routes?" were drowning in @Bean/@Configuration.
_SPRING_DI_ONLY_LASTSEGS = frozenset({
    "service", "repository", "configuration", "bean", "autowired",
    "eventlistener", "postconstruct", "predestroy",
    "kafkalistener", "rabbitlistener", "jmslistener",
    "springbootapplication",
})

# Angular UI — protect from dead-code; list only with framework='angular'.
_ANGULAR_UI_ONLY_LASTSEGS = frozenset({
    "component", "injectable", "directive", "pipe", "ngmodule", "hostlistener",
})

# CLI / MCP / Celery — protect from dead-code; list via framework=click|fastmcp|celery.
# NOTE: ``fixture`` stays on the default *compute_endpoints* surface so the
# Spec Explorer can split fixtures into DATA.fixtures; ``find_endpoints``
# still drops them via ``filter_api_endpoints`` unless framework='pytest'.
_NON_HTTP_SURFACE_LASTSEGS = frozenset({
    "command", "group",
    "tool", "resource", "prompt",
    "task", "shared_task",
})

# Default find_endpoints ≈ HTTP(+FS routing) surface. Opt into Angular / CLI /
# MCP / Celery with framework=. Java @Component still excluded via path check.
_ENDPOINT_SURFACE_DECORATOR_LASTSEG = (
    _ENTRY_POINT_DECORATOR_LASTSEG
    - _SPRING_DI_ONLY_LASTSEGS
    - _ANGULAR_UI_ONLY_LASTSEGS
    - _NON_HTTP_SURFACE_LASTSEGS
)

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
    "fastmcp": ("tool", "resource", "prompt", "mutation_tool", "agentic_tool"),
    "celery": ("task", "shared_task"),
    "django": ("login_required", "permission_required", "staff_member_required"),
    # v0.13 P2 / Unreleased: HTTP mappings + controllers only (not @Bean/@Service).
    "spring": (
        "GetMapping", "PostMapping", "PutMapping", "DeleteMapping",
        "PatchMapping", "RequestMapping", "RestController", "Controller",
        "ExceptionHandler",
    ),
    # v0.13 P2 / Unreleased: Angular UI entry points (not in default HTTP sweep).
    "angular": (
        "Component", "Injectable", "Directive", "Pipe", "NgModule", "HostListener",
    ),
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
# Injectable services are DI-invoked from components/templates with zero
# in-project call edges — protect all their methods too (not just lifecycle).
_NG_DI_CLASS_LASTSEGS = frozenset({"injectable"})
_NG_ANY_DECORATOR_LASTSEGS = (
    _NG_TEMPLATE_DECORATOR_LASTSEGS | _NG_DI_CLASS_LASTSEGS | frozenset({"ngmodule"})
)

# Spring stereotypes: the DI container instantiates these and may invoke any
# public method via proxies / other beans. Method-level @GetMapping already
# protects mapped handlers; class-level stereotypes protect the rest.
_SPRING_STEREOTYPE_LASTSEGS = frozenset({
    "restcontroller",
    "controller",
    "service",
    "repository",
    "component",
    "configuration",
    "controlleradvice",
    "restcontrolleradvice",
})


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


def _is_endpoint_surface_decorator(name: str, file_path: str = "") -> bool:
    """True if ``name`` should appear in ``find_endpoints`` default sweep.

    Spring DI / Angular UI / Click / FastMCP / Celery stay in
    ``_ENTRY_POINT_DECORATOR_LASTSEG`` so ``find_dead_code`` still protects
    them, but they are not HTTP routes — use ``framework=…`` to list them.
    Java ``@Component`` shares a lastseg with Angular ``@Component`` — only
    the Java form is path-excluded here (Angular is already subtracted).
    """
    seg = _decorator_lastseg(name)
    if seg not in _ENDPOINT_SURFACE_DECORATOR_LASTSEG:
        return False
    fp = file_path.replace("\\", "/").lower()
    if seg == "component" and fp.endswith((".java", ".kt")):
        return False
    return True


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


@lru_cache(maxsize=4096)
def _cached_ast_parse(file_path_abs: str, mtime: float):
    """Parse a Python file ONCE per (path, mtime).

    The dead-code scan helpers below each used to read + ``ast.parse`` the same
    file independently — ~5 parses per file, ~11K parses per ``find_dead_code``
    on Django — and cached by path alone, so results went stale after an edit.
    They now share this cache. ``mtime`` (from the DB ``file.mtime``, taken at
    index time) is part of the key so an edit + re-index reparses.
    """
    try:
        source = Path(file_path_abs).read_text(encoding="utf-8", errors="replace")
        return ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None


def _used_nested_def_names(file_path_abs: str, mtime: float) -> frozenset[str]:
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
    tree = _cached_ast_parse(file_path_abs, mtime)
    if tree is None:
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


def _publicly_exported_names(file_path_abs: str, mtime: float) -> frozenset[str]:
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
    tree = _cached_ast_parse(file_path_abs, mtime)
    if tree is None:
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
    # FastAPI / Starlette
    "include_router",
    "add_api_route",
    "add_exception_handler",
    "add_event_handler",
    "mount",
    "add_websocket_route",
})

# Constructor callables whose keyword Name args are framework entry points
# (e.g. ``FastAPI(lifespan=lifespan)``).
_FRAMEWORK_CTOR_NAMES = frozenset({"FastAPI", "Flask", "APIRouter"})
_FRAMEWORK_CTOR_KW = frozenset({
    "lifespan", "on_startup", "on_shutdown", "dependencies",
})


def _runtime_registered_names(file_path_abs: str, mtime: float) -> frozenset[str]:
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
    tree = _cached_ast_parse(file_path_abs, mtime)
    if tree is None:
        return frozenset()

    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # FastAPI(lifespan=fn) / Flask(on_startup=[...]) — Name kwargs.
        ctor_name = None
        if isinstance(func, ast.Name):
            ctor_name = func.id
        elif isinstance(func, ast.Attribute):
            ctor_name = func.attr
        if ctor_name in _FRAMEWORK_CTOR_NAMES:
            for kw in node.keywords:
                if kw.arg in _FRAMEWORK_CTOR_KW and isinstance(kw.value, ast.Name):
                    out.add(kw.value.id)
                elif kw.arg in _FRAMEWORK_CTOR_KW and isinstance(kw.value, (ast.List, ast.Tuple)):
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Name):
                            out.add(elt.id)
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


def _entry_point_decorator_aliases(file_path_abs: str, mtime: float) -> frozenset[str]:
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
    tree = _cached_ast_parse(file_path_abs, mtime)
    if tree is None:
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


def _module_level_referenced_names(file_path_abs: str, mtime: float) -> frozenset[str]:
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
    tree = _cached_ast_parse(file_path_abs, mtime)
    if tree is None:
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

    # Next.js pages router: ``pages/`` or ``src/pages/``.
    # NOT ``src/app/pages/`` — that is the Angular feature-folder convention
    # (results SPA was listing 68 fake nextjs_pages endpoints).
    segs = [s for s in normalised.split("/") if s]
    for i, seg in enumerate(segs):
        if seg == "pages":
            if i > 0 and segs[i - 1] == "app":
                break
            return "nextjs_pages"

    # Next.js app router — skip Angular ``app/pages/**`` feature trees.
    # Magic files are exactly ``page.tsx`` / ``layout.ts`` / … (one stem +
    # one extension). ``error.component.ts`` must NOT match.
    if "/app/" in normalised and "/app/pages/" not in normalised:
        app_router_stems = frozenset(
            {
                "page", "layout", "loading", "error",
                "not-found", "template", "default", "route",
            }
        )
        parts = basename.split(".")
        if len(parts) == 2 and parts[0] in app_router_stems:
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


def _module_path_candidates(module: str) -> list[str]:
    """Dotted in-project module → candidate relative file paths."""
    base = module.replace(".", "/")
    out: list[str] = []
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        out.append(base + ext)
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        out.append(f"{base}/index{ext}")
    return out


def _resolve_call_style_handler(
    conn,
    project_id: int,
    route_file_id: int,
    route_rel_path: str,
    source: str,
    language: str,
    handler_name: str | None,
    handler_import: str | None,
) -> Any | None:
    """Resolve a Hono/Express handler to a symbol row.

    Prefers the file's import map so ``wrap(details)`` links to
    ``src.controllers.details`` (default export) instead of a colliding
    ``src.services.suppliers.details`` elsewhere in the project.
    """
    if not handler_name:
        return None

    import_mod: str | None = None
    if language in ("typescript", "javascript", "tsx") and handler_import:
        try:
            parent = Path(route_rel_path).parent
            current_dir = () if str(parent) in (".", "") else tuple(parent.parts)
            src_bytes = source.encode("utf-8", errors="replace")
            tree = get_parser(language).parse(src_bytes)
            imports = _ts_collect_imports(tree.root_node, src_bytes, current_dir)
            import_mod = imports.get(handler_import)
        except Exception:
            import_mod = None

    basename = (import_mod or "").rsplit(".", 1)[-1] if import_mod else ""

    # 1) Import-scoped: restrict candidates to the imported module's file.
    if import_mod and ("." in import_mod or "/" in import_mod or import_mod.startswith("src")):
        candidates = _module_path_candidates(import_mod)
        placeholders = ",".join("?" * len(candidates))
        rows = conn.execute(
            f"""SELECT s.qualified_name, s.kind, s.start_line, s.end_line, s.name,
                       f.path AS file_path
                FROM symbol s JOIN file f ON f.id=s.file_id
                WHERE f.project_id=? AND f.path IN ({placeholders})""",
            (project_id, *candidates),
        ).fetchall()
        if rows:
            def rank(r) -> tuple[int, int]:
                # Prefer exact handler name, then module basename (default export),
                # then any real function, then __module__ fallback.
                if r["name"] == handler_name:
                    return (0, r["start_line"] or 0)
                if basename and r["name"] == basename:
                    return (1, r["start_line"] or 0)
                if r["kind"] == "function" and r["name"] != "__module__":
                    return (2, r["start_line"] or 0)
                if r["name"] == "__module__":
                    return (3, r["start_line"] or 0)
                return (4, r["start_line"] or 0)

            rows = sorted(rows, key=rank)
            best = rows[0]
            if rank(best)[0] <= 3:
                return best

    # 2) Same-file then any-name fallback (previous behaviour).
    return conn.execute(
        """SELECT s.qualified_name, s.kind, s.start_line, s.end_line
           FROM symbol s JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND s.name=?
           ORDER BY (s.file_id=?) DESC LIMIT 1""",
        (project_id, handler_name, route_file_id),
    ).fetchone()


def _enclosing_symbol_for_line(conn, file_id: int, line: int) -> Any | None:
    """Innermost indexed symbol whose body contains ``line``.

    An inline arrow handler (``app.get('/x', async (req, res) => …)``) never
    becomes a symbol of its own, but ``_ts_collect_calls`` attributes its call
    sites to the innermost named scope that encloses it — the file's
    ``__module__`` pseudo-symbol when the registration sits at top level. That
    scope is therefore the honest navigable target for the route: its outgoing
    edges *are* the handler's.
    """
    return conn.execute(
        """SELECT qualified_name, kind, start_line, end_line FROM symbol
           WHERE file_id=? AND start_line<=? AND end_line>=?
           ORDER BY (end_line - start_line), start_line DESC LIMIT 1""",
        (file_id, line, line),
    ).fetchone()


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

    Call-style routes (Hono/Express) also carry ``handler_resolution``:
    ``handler`` when the registered handler resolved to its own symbol,
    ``enclosing_scope`` when the handler is an inline arrow and
    ``qualified_name`` is the scope that owns its call edges (``start_line``
    keeps pointing at the registration), ``unresolved`` for the legacy
    ``file:line`` pseudo-id — only reachable when the file has no indexed
    symbol at all.
    """
    pid = st.project_id

    rows = st.conn.execute(
        """SELECT s.id AS symbol_id, s.qualified_name, s.kind, s.decorators,
                  s.start_line, s.end_line, f.path AS file_path
           FROM symbol s JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND s.decorators IS NOT NULL
           ORDER BY f.path, s.start_line""",
        (pid,),
    ).fetchall()

    if framework is not None:
        patterns = _FRAMEWORK_DECORATOR_PATTERNS.get(framework, ())
        alias_lastsegs: set[str] = set()
        # FastMCP plugin aliases (`mutation_tool = mcp.tool if …`) only matter
        # when listing MCP tools — not for flask/fastapi/spring/….
        if framework == "fastmcp":
            workspace_path = st.settings.workspace
            for path_row in st.conn.execute(
                "SELECT f.path, f.mtime FROM file f WHERE f.project_id=? AND f.path LIKE '%.py'",
                (pid,),
            ):
                try:
                    abs_path = str(workspace_path / path_row["path"])
                    alias_lastsegs |= _entry_point_decorator_aliases(
                        abs_path, float(path_row["mtime"])
                    )
                except Exception:
                    continue

        def keep(decs: list[str], file_path: str = "") -> list[str]:
            del file_path  # framework filter is decorator-only
            return [
                d
                for d in decs
                if _decorator_matches_any(d, patterns)
                or _decorator_lastseg(d) in alias_lastsegs
            ]
    else:
        # Default = HTTP-ish surface (Flask/FastAPI/Spring mappings/…).
        # Angular / Click / FastMCP / Celery / Spring DI require framework=.
        # (Alias factories for mcp.tool are intentionally omitted here.)
        def keep(decs: list[str], file_path: str = "") -> list[str]:
            return [d for d in decs if _is_endpoint_surface_decorator(d, file_path)]

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

    # Java annotations reach `decorators` as bare names (`GetMapping`) — the
    # argument holding the path is dropped there but the extractor already
    # persists it as a `route_ref` server row. Join it so Spring handlers carry
    # the same http_method/http_path contract as express/hono/python instead of
    # listing a handler with no route.
    server_routes: dict[int, tuple[str | None, str]] = {}
    for rr in st.conn.execute(
        """SELECT rr.symbol_id, rr.method, rr.path
           FROM route_ref rr
           JOIN symbol s ON s.id=rr.symbol_id
           JOIN file f ON f.id=s.file_id
           WHERE f.project_id=? AND rr.role='server'
           ORDER BY rr.line, rr.id""",
        (pid,),
    ):
        server_routes.setdefault(int(rr["symbol_id"]), (rr["method"], rr["path"]))

    def _attach_indexed_http_route(entry: dict[str, Any], symbol_id: int) -> None:
        if entry.get("http_path") is not None:
            return
        route = server_routes.get(symbol_id)
        if route is None:
            return
        method, path = route
        if method:
            entry["http_method"] = method
        entry["http_path"] = path
        if entry["file_path"].endswith(".java"):
            entry["http_framework"] = "spring"

    for r in rows:
        try:
            decs = json.loads(r["decorators"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        matching = keep(decs, r["file_path"])
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
        _attach_indexed_http_route(entry, int(r["symbol_id"]))
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

    # v0.13 P3 / Unreleased: call-style HTTP routes (Hono + Express).
    # Same AST scanner — both frameworks use `app|router.get('/path', handler)`.
    # Included in ``framework=None`` (default) as well as explicit opt-in —
    # agents calling find_endpoints() without a framework must see Express
    # routes on real polyrepos (audit 2026-07-30). Pre-filter by marker
    # in source so we don't scan every TS/JS file.
    call_style_frameworks = (
        (framework,)
        if framework in ("hono", "express")
        else (("hono", "express") if framework is None else ())
    )
    for call_fw in call_style_frameworks:
        workspace_path = st.settings.workspace
        marker = "hono" if call_fw == "hono" else "express"
        method_key = "hono_method" if call_fw == "hono" else "express_method"
        path_key = "hono_path" if call_fw == "hono" else "express_path"
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
            if marker not in src.lower():
                continue
            for rt in scan_hono_routes(src, fr["language"]):
                qname = None
                kind = "route"
                resolution = "unresolved"
                start_line = end_line = rt["line"]
                if rt["handler_name"]:
                    sym = _resolve_call_style_handler(
                        st.conn,
                        pid,
                        int(fr["id"]),
                        fr["path"],
                        src,
                        fr["language"],
                        rt.get("handler_name"),
                        rt.get("handler_import") or rt.get("handler_name"),
                    )
                    if sym is not None:
                        qname = sym["qualified_name"]
                        kind = sym["kind"]
                        start_line = sym["start_line"]
                        end_line = sym["end_line"]
                        resolution = "handler"
                if qname is None:
                    # Inline arrow (or a handler name we couldn't resolve): fall
                    # back to the scope that owns its call edges instead of a
                    # `file.js:12` pseudo-id, which every symbol-taking tool
                    # rejects with "Symbol not found" (beta sweep, 6/23 repos).
                    enclosing = _enclosing_symbol_for_line(
                        st.conn, int(fr["id"]), rt["line"]
                    )
                    if enclosing is not None:
                        qname = enclosing["qualified_name"]
                        kind = enclosing["kind"]
                        resolution = "enclosing_scope"
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
                    method_key: rt["method"],
                    path_key: rt["path"],
                    "http_method": rt["method"],
                    "http_path": rt["path"],
                    "http_framework": call_fw,
                    "handler_resolution": resolution,
                })
                seen_qnames.add(route_key)

    # Unreleased: Go call-style routes (gin / echo / chi / net/http).
    _GO_FRAMEWORKS = ("gin", "echo", "chi", "nethttp")
    go_frameworks = (
        (framework,)
        if framework in _GO_FRAMEWORKS
        else (_GO_FRAMEWORKS if framework is None else ())
    )
    if go_frameworks:
        workspace_path = st.settings.workspace
        for fr in st.conn.execute(
            """SELECT id, path, language FROM file
               WHERE project_id=? AND language='go'
               ORDER BY path""",
            (pid,),
        ).fetchall():
            try:
                src = (workspace_path / fr["path"]).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            src_l = src.lower()
            # Cheap prefilter: skip files with no HTTP-ish markers.
            if not any(
                m in src_l
                for m in (
                    "gin-gonic", "labstack/echo", "go-chi/chi",
                    "handlefunc", "net/http",
                )
            ):
                continue
            for rt in scan_go_routes(src):
                fw = rt.get("framework") or "nethttp"
                if framework is not None and fw != framework:
                    continue
                qname = None
                kind = "route"
                resolution = "unresolved"
                start_line = end_line = rt["line"]
                handler = rt.get("handler_name")
                if handler:
                    sym = st.conn.execute(
                        """SELECT qualified_name, kind, start_line, end_line
                           FROM symbol WHERE file_id=? AND name=?
                           LIMIT 1""",
                        (int(fr["id"]), handler),
                    ).fetchone()
                    if sym is None:
                        sym = st.conn.execute(
                            """SELECT s.qualified_name, s.kind, s.start_line, s.end_line
                               FROM symbol s JOIN file f ON f.id=s.file_id
                               WHERE f.project_id=? AND s.name=?
                               LIMIT 1""",
                            (pid, handler),
                        ).fetchone()
                    if sym is not None:
                        qname = sym["qualified_name"]
                        kind = sym["kind"]
                        start_line = sym["start_line"]
                        end_line = sym["end_line"]
                        resolution = "handler"
                if qname is None:
                    enclosing = _enclosing_symbol_for_line(
                        st.conn, int(fr["id"]), rt["line"]
                    )
                    if enclosing is not None:
                        qname = enclosing["qualified_name"]
                        kind = enclosing["kind"]
                        resolution = "enclosing_scope"
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
                    "http_method": rt["method"],
                    "http_path": rt["path"],
                    "http_framework": fw,
                    "handler_resolution": resolution,
                })
                seen_qnames.add(route_key)

    endpoints.sort(key=lambda e: (e["file_path"], e["start_line"]))
    return endpoints


def _is_test_scaffold_path(file_path: str) -> bool:
    """True for any test file, plus conftest.py and fixture helper dirs.

    Delegates the "is this a test file" half to ``_is_test_file_path`` instead
    of keeping a second, narrower copy. The copy only knew Python conventions,
    so ``filter_api_endpoints`` let routes declared inside ``*.test.ts`` through
    and ``find_endpoints`` listed them as real endpoints — on a Hono backend
    that surfaced `POST /login` from `auth.test.ts` next to the genuine one in
    `auth.ts`, with nothing to tell them apart.

    The pytest-specific extras below are NOT part of the general heuristic
    (a ``fixtures/`` dir is scaffolding but not a test file), so they stay here.
    """
    fp = file_path.replace("\\", "/").lstrip("/")
    if _is_test_file_path(fp):
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


# Directory segments that mark a test tree. Matched as EXACT path segments,
# never as substrings — `contest/`, `latest/`, `protest/` must not match.
# `spec` (RSpec) is in; `specs` (plural) is NOT — that's the OpenSpec/docs
# convention, not a test one.
_TEST_DIR_SEGMENTS = frozenset({"tests", "test", "__tests__", "spec"})

_TEST_CODE_EXTS = ("ts", "tsx", "js", "jsx", "mjs", "cjs")

# Basename suffixes, each ANCHORED on its separator so a near-miss can't
# match: `latest.ts` ends with `test.ts` but not `.test.ts`; likewise
# `protest.ts`. `_spec.` is anchored to known extensions rather than left as
# a bare substring — in a spec-tracking tool, `import_spec.py` is a plausible
# real source file.
_TEST_BASENAME_SUFFIXES = (
    tuple(f".{w}.{e}" for w in ("test", "spec") for e in _TEST_CODE_EXTS)
    + tuple(f"_spec.{e}" for e in _TEST_CODE_EXTS)
    + ("_spec.rb",)
)


def _is_test_file_path(path: str) -> bool:
    """True if `path` is a test file. Shared by ``find_orphan_tests``, the
    Spec-test-coverage derivation and the project-overview ranking, so all
    three agree on what counts as a test. Path is project-relative.

    Matches, by language convention:
    - a ``tests`` / ``test`` / ``__tests__`` / ``spec`` **path segment**
      (Python, Go, Java ``src/test/``, Jest ``__tests__/``, RSpec ``spec/``)
    - basename prefix ``test_`` (Python)
    - basename infix ``_test.`` (Python ``_test.py``, Go ``_test.go``, …)
    - basename suffix ``.test.<jsext>`` / ``.spec.<jsext>`` /
      ``_spec.<jsext>`` / ``_spec.rb`` (JS/TS, Ruby)
    """
    fp = path.replace("\\", "/").lstrip("/")
    segments = fp.split("/")
    base = segments[-1]
    return (
        not _TEST_DIR_SEGMENTS.isdisjoint(segments[:-1])
        or base.startswith("test_")
        or "_test." in base
        or base.endswith(_TEST_BASENAME_SUFFIXES)
    )


_NON_PRODUCT_TOP_DIRS = frozenset({"scripts", "bench", "fixtures"})


def _is_non_product_orphan_path(path: str) -> bool:
    """Paths that are not Spec-link candidates in a coverage audit.

    Test files, fixtures, and repo tooling (``scripts/``, ``bench/``) inflate
    ``modules_truly_orphan`` without being actionable "add a Spec" work —
    exclude them from the orphan KPI (counted separately as non-product).
    """
    fp = path.replace("\\", "/").lstrip("/")
    if _is_test_file_path(fp):
        return True
    top = fp.split("/", 1)[0]
    return top in _NON_PRODUCT_TOP_DIRS


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
       ``tested_symbols`` (derived), carries an explicit ``relation='tests'``
       link (explicit), or overlaps a Jest/Vitest report-covered line (report).
       ``coverage_source`` records which kinds contributed.

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
    frontier: deque[tuple[int, int]] = deque(
        (sid, 0) for sid in test_sids if sid in g
    )
    # Seed: a test symbol is itself "covered" trivially, but we only care
    # about what tests REACH — production impl symbols downstream. We still
    # add the seeds so an impl symbol that is *itself* a test symbol (rare)
    # is counted. Forward edges expand from there up to _TEST_REACH_DEPTH.
    # FIFO (popleft) = true BFS so shortest-path depth wins; a LIFO frontier
    # would pin a node at a longer path's depth and skip its within-budget
    # descendants, falsely marking reachable specs untested.
    tested_symbols.update(sid for sid, _ in frontier)
    while frontier:
        node, depth = frontier.popleft()
        if depth >= _TEST_REACH_DEPTH:
            continue
        for succ in g.successors(node):
            if succ in tested_symbols:
                continue
            tested_symbols.add(succ)
            frontier.append((succ, depth + 1))

    report_coverage = discover_report_coverage(st.settings.workspace)

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
        report_hit = False
        tested_count = 0
        uncovered_sids: list[int] = []
        # MCP / Client harness pattern: Specs often link real test *functions*
        # with relation='tests', but those tests call tools by string name so
        # there is no static edge to the `implements` symbols. If any explicit
        # tests-link points at a symbol in a test file, credit every implement
        # on this Spec (same intent as dual-linking the impl as `tests`).
        harness_tests = any(
            _is_test_file_path((view.sym_meta.get(sid) or {}).get("file_path") or "")
            for sid in explicit
        )
        for sid in impl:
            in_derived = sid in tested_symbols
            in_explicit = sid in explicit or harness_tests
            meta = view.sym_meta.get(sid, {})
            report_lines = report_coverage.get(meta.get("file_path") or "", set())
            in_report = any(
                int(meta.get("start_line") or 0) <= line <= int(meta.get("end_line") or 0)
                for line in report_lines
            )
            if in_derived:
                derived_hit = True
            if in_explicit:
                explicit_hit = True
            if in_report:
                report_hit = True
            if in_derived or in_explicit or in_report:
                tested_count += 1
            else:
                uncovered_sids.append(sid)
        ratio = (tested_count / total) if total else 0.0
        if report_hit:
            source_parts = []
            if derived_hit:
                source_parts.append("derived")
            if explicit_hit:
                source_parts.append("explicit")
            source_parts.append("report")
            source = "+".join(source_parts)
        elif derived_hit and explicit_hit:
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


def compute_coverage(st: AppState, *, record: bool = True) -> dict[str, Any]:
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
    })
    _INDEX_BASENAMES = frozenset({
        "index.ts", "index.js", "index.tsx", "index.jsx", "index.mjs",
    })

    ws_root = st.settings.workspace

    def _is_package_marker(path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        if base in _PACKAGE_MARKER_BASENAMES:
            return True
        if base in _INDEX_BASENAMES:
            return _package_marker_is_emptyish(ws_root, path)
        return False

    from pathlib import Path as _Path

    from livespec_mcp.domain.languages import (
        ANNOTATION_SUPPORTED_LANGUAGES,
        detect_language,
    )

    def _annotation_supported(path: str) -> bool:
        lang = detect_language(_Path(path))
        return lang in ANNOTATION_SUPPORTED_LANGUAGES

    all_no_spec_raw = [
        r["path"]
        for r in st.conn.execute(
            """SELECT f.path FROM file f
               WHERE f.project_id=?
                 AND EXISTS (
                   SELECT 1 FROM symbol s WHERE s.file_id=f.id
                 )
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
    modules_unsupported_language = [
        p for p in all_no_spec_raw if not _annotation_supported(p)
    ]
    # Test/fixture/script/bench noise is not a Spec-map gap — count it
    # separately so Coverage gaps stays actionable for product code.
    modules_non_product = [
        p
        for p in all_no_spec_raw
        if _annotation_supported(p) and _is_non_product_orphan_path(p)
    ]
    modules_no_spec = [
        p
        for p in all_no_spec_raw
        if _annotation_supported(p) and not _is_non_product_orphan_path(p)
    ]

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
        # Inverted traversal (v0.20 H3): a file is implicitly covered iff one
        # of its symbols sits in the FORWARD cone of a spec-linked symbol (i.e.
        # a spec-linked symbol transitively calls into it). One multi-source
        # forward BFS from all spec-linked symbols gives that reachable set in
        # O(V+E) — instead of a depth-10 BACKWARD cone per symbol per orphan
        # file (O(files x symbols x BFS), minutes on a spec-adopting Django).
        reached: set[int] = set()
        if spec_linked_sids:
            seen_r = {s for s in spec_linked_sids if s in view.g}
            frontier_r: deque[tuple[int, int]] = deque((s, 0) for s in seen_r)
            while frontier_r:
                node, d = frontier_r.popleft()
                if d >= 10:
                    continue
                for succ in view.g.successors(node):
                    if succ not in seen_r:
                        seen_r.add(succ)
                        frontier_r.append((succ, d + 1))
            reached = seen_r
        # Batch the file→symbol lookup: one scan instead of a query per file.
        no_spec_set = set(modules_no_spec)
        sids_by_path: dict[str, set[int]] = {}
        for r in st.conn.execute(
            """SELECT f.path AS path, s.id AS id FROM symbol s
               JOIN file f ON f.id=s.file_id
               WHERE f.project_id=?""",
            (pid,),
        ):
            p = r["path"]
            if p in no_spec_set:
                sids_by_path.setdefault(p, set()).add(int(r["id"]))
        for path in modules_no_spec:
            covered = bool(sids_by_path.get(path, ()) and (sids_by_path[path] & reached))
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
    # `spec_test_coverage` / `specs_with_linked_tests` above untouched.
    spec_coverage_map = compute_spec_test_coverage(st, view)
    spec_coverage = sorted(
        spec_coverage_map.values(),
        key=lambda d: (-d["test_coverage_ratio"], d["spec_id"]),
    )
    specs_with_derived_test_coverage = sum(
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
    # failure must never break the audit itself. v0.20 M19: skip recording on
    # summary_only / cursor pages / explorer regen (record=False) so a
    # readOnlyHint tool doesn't write on every paginated fetch or bundle build.
    snapshot_warning: str | None = None
    if record:
        try:
            # `datetime.UTC` is Python 3.11+; the project supports >=3.10, where
            # `from datetime import UTC` raises ImportError — silently swallowed
            # by the except below, so audit_coverage never recorded a snapshot on
            # 3.10 (empty trend). `timezone.utc` works on every supported version.
            from datetime import datetime, timezone

            from livespec_mcp.storage import trends

            trends.record_snapshot(
                st.conn,
                pid,
                per_spec={d["spec_id"]: d["test_coverage_ratio"] for d in spec_coverage},
                avg=avg_test_coverage if spec_coverage else None,
                verified_count=specs_with_derived_test_coverage,
                ts=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            snapshot_warning = f"coverage snapshot not recorded: {type(exc).__name__}: {exc}"

    counts = {
        "modules_without_spec": len(modules_no_spec),
        "modules_implicitly_covered": len(modules_implicit),
        "modules_truly_orphan": len(modules_truly_orphan),
        "modules_unsupported_language": len(modules_unsupported_language),
        "modules_non_product": len(modules_non_product),
        "specs_without_implementation": len(specs_no_impl),
        "specs_low_confidence": len(specs_low_conf),
        # Two DIFFERENT mechanisms, not two views of one number. They used to
        # be `specs_with_test_coverage` / `specs_with_any_test_coverage`, whose
        # names read as contradictory once they diverged (17 vs 0).
        #   linked  = Specs with ≥1 EXPLICIT `relation='tests'` spec_symbol row.
        #   derived = Specs whose per-symbol coverage ratio is > 0 — see
        #             compute_spec_test_coverage: a symbol counts when a test
        #             reaches it in the call graph OR carries its own explicit
        #             'tests' link, so "derived" names the mechanism (the
        #             ratio), not an explicit-free source.
        "specs_with_linked_tests": len(spec_test_coverage),
        "specs_with_derived_test_coverage": specs_with_derived_test_coverage,
        "avg_test_coverage": avg_test_coverage,
    }
    return {
        "counts": counts,
        "modules_without_spec": modules_no_spec,
        "modules_implicitly_covered": modules_implicit,
        "modules_truly_orphan": modules_truly_orphan,
        "modules_unsupported_language": modules_unsupported_language,
        "modules_non_product": modules_non_product,
        "specs_without_implementation": specs_no_impl,
        "specs_low_confidence": specs_low_conf,
        "spec_coverage": spec_coverage,
        "avg_test_coverage": avg_test_coverage,
        "specs_with_derived_test_coverage": specs_with_derived_test_coverage,
        "spec_test_coverage": spec_test_coverage,
        "snapshot_warning": snapshot_warning,
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
            # --name-status -M: detect renames so the OLD path (which holds the
            #   indexed symbols) is included alongside the new one — a plain
            #   --name-only rename shows only the new path, dropping the whole
            #   caller cone / affected specs for that file.
            # --end-of-options: a ref beginning with '-' can otherwise be
            #   parsed as a git option (e.g. --output=... → arbitrary file
            #   write). This guards the caller-supplied range token.
            [
                "git", "-C", ws_root, "diff", "--name-status", "-M",
                "--end-of-options", f"{base_ref}..{head_ref}",
            ],
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

    # Parse --name-status: "<status>\t<path>" or, for renames/copies,
    # "R<score>\t<old>\t<new>". Include both old and new for rename/copy so the
    # old path's indexed symbols still resolve.
    changed: list[str] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        paths = parts[1:]
        if status[:1] in ("R", "C") and len(paths) >= 2:
            candidates = [paths[0], paths[1]]
        else:
            candidates = paths[:1]
        for p in candidates:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                changed.append(p)
    return changed, None


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
    all_changed = sorted(changed_paths)

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
        # Still surface every path from git — Explorer lists them even when
        # none are indexed / Spec-linked.
        return {
            "base": base,
            "head": head,
            "files_changed": all_changed,
            "specs_touched": [],
        }

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
        return {
            "base": base,
            "head": head,
            "files_changed": all_changed,
            "specs_touched": [],
        }

    # Map every touched symbol id -> the Specs that link it.
    sym_to_specs: dict[int, list[str]] = {}
    spec_titles: dict[str, str] = {}
    for r in _select_in_chunks(
        st.conn,
        """SELECT rs.symbol_id, r.spec_id, r.title
            FROM spec_symbol rs JOIN spec r ON r.id = rs.spec_id
            WHERE r.project_id=? AND rs.symbol_id IN ({in})""",
        all_touched,
        prefix_params=(pid,),
    ):
        sym_to_specs.setdefault(int(r["symbol_id"]), []).append(r["spec_id"])
        spec_titles[r["spec_id"]] = r["title"]

    if not spec_titles:
        return {
            "base": base,
            "head": head,
            "files_changed": all_changed,
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
        "files_changed": all_changed,
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

    Symbols living in test files are always dropped from `top_symbols` —
    the tool answers "what is this repo's core", and test helpers are not
    it. Their qualified names come back in `test_symbols_filtered`, so
    nothing is silently discarded.

    Caveat on that field, unlike `structural_patterns_filtered` (a full DB
    query, therefore exhaustive): it is collected inside the ranking loop,
    which stops at 20 kept symbols, and is itself capped at 20. So it lists
    the top test symbols that OUTRANKED the last entry of `top_symbols` —
    not every test symbol in the project. That is the actionable set (the
    ones that would have polluted the answer), not a census.
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
    ranks = graph_pagerank(view)
    ordered = sorted(ranks.items(), key=lambda x: x[1], reverse=True)
    top_syms: list[dict[str, Any]] = []
    test_outranked: list[str] = []
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
        # Test scaffolding (createMockDb, signTestToken, fakeAuthMiddleware…)
        # ranks high by PageRank but is the opposite of "what is this repo's
        # core". Filtered, and surfaced by name so the caller can still see it.
        if _is_test_file_path(meta.get("file_path") or ""):
            test_outranked.append(meta.get("qualified_name") or meta.get("name") or "")
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
        # Every number below counts this workspace only. In a shared group DB
        # that is a real limit, not a detail: the same call from a sibling repo
        # answers differently.
        **group_fields(st),
        "languages": langs,
        "top_symbols": top_syms,
        "structural_patterns_filtered": sorted(structural_names),
        # Capped: when fewer than 20 symbols survive the filters the loop
        # never breaks and scans the whole PageRank ordering, so this would
        # otherwise carry EVERY test symbol in the project into a response
        # that has no pagination and was fixed-size by design.
        "test_symbols_filtered": test_outranked[:20],
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


def _as_project_ids(project_id: int | list[int]) -> list[int]:
    if isinstance(project_id, int):
        return [project_id]
    return [int(x) for x in project_id]


def _resolve_symbol(
    conn,
    project_id: int | list[int],
    identifier: str,
) -> dict | None:
    """Resolve a symbol by qualified_name (exact) or short name (best match).

    ``project_id`` may be a single id or a list (home-first group ids from
    ``AppState.group_project_ids()``). Exact qname prefers earlier ids in the
    list; short-name fallback only when unambiguous across the whole set.
    Returned dict includes ``project_id`` + ``project_root`` for cross-repo
    source reads and per-project ``load_graph``.
    """
    ids = _as_project_ids(project_id)
    if not ids:
        return None
    placeholders = ",".join("?" for _ in ids)
    order = " ".join(f"WHEN {pid} THEN {i}" for i, pid in enumerate(ids))
    select = f"""SELECT s.*, f.path AS file_path, f.project_id AS project_id,
                        p.root AS project_root
                 FROM symbol s
                 JOIN file f ON f.id=s.file_id
                 JOIN project p ON p.id=f.project_id
                 WHERE f.project_id IN ({placeholders})"""
    row = conn.execute(
        f"""{select} AND s.qualified_name=?
            ORDER BY CASE f.project_id {order} END LIMIT 1""",
        (*ids, identifier),
    ).fetchone()
    if row:
        return dict(row)
    rows = conn.execute(
        f"""{select} AND s.name=?
            ORDER BY CASE f.project_id {order} END LIMIT 5""",
        (*ids, identifier),
    ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    return None


def did_you_mean_symbols(
    conn,
    project_id: int | list[int],
    identifier: str,
    limit: int = 3,
) -> list[dict]:
    """Top-N symbol suggestions for a misspelled or partial identifier.

    Used by tools that raise 'Symbol not found' to surface likely intended
    targets in the error payload (P2.D3). Combines two passes:
      1. SQL substring match on name / qualified_name (catches partials,
         prefix mistypes).
      2. difflib SequenceMatcher ratio on the short name (catches typos
         where the substring path doesn't fire — e.g. 'logn' ≈ 'login').
    Ranked by ratio descending. Scoped to ``project_id`` or the whole group.
    """
    ids = _as_project_ids(project_id)
    if not ids:
        return []
    short = identifier.split(".")[-1]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT s.qualified_name, s.kind, f.path AS file_path, s.name,
                   p.name AS project_name
            FROM symbol s JOIN file f ON f.id=s.file_id
            JOIN project p ON p.id=f.project_id
            WHERE f.project_id IN ({placeholders})""",
        ids,
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
            item = {"qualified_name": qn, "kind": r["kind"], "file_path": r["file_path"]}
            if r["project_name"]:
                item["project"] = r["project_name"]
            out.append(item)
    for m in matches:
        if len(out) >= limit:
            break
        for r in name_to_rows.get(m, []):
            qn = r["qualified_name"]
            if qn in seen:
                continue
            seen.add(qn)
            item = {"qualified_name": qn, "kind": r["kind"], "file_path": r["file_path"]}
            if r["project_name"]:
                item["project"] = r["project_name"]
            out.append(item)
            if len(out) >= limit:
                break
    return out


def symbol_not_found_error(
    conn,
    project_id: int | list[int],
    identifier: str,
) -> dict:
    """Build the standard 'Symbol not found' error payload with did_you_mean."""
    return mcp_error(
        f"Symbol '{identifier}' not found",
        did_you_mean=did_you_mean_symbols(conn, project_id, identifier),
        hint=(
            "run `find_symbol(query=<short_name>)` to discover qualified names"
            " (searches the whole group_db when configured)"
        ),
    )


def _graph_project_id(sym: dict, home_pid: int) -> int:
    """Project that owns ``sym`` — used to load the correct NetworkX graph."""
    pid = sym.get("project_id")
    return int(pid) if pid is not None else home_pid


def group_fields(st: AppState) -> dict[str, Any]:
    """The `grouped` / `group_db` pair, always both, always present.

    Tools used to disagree: some emitted the pair only when a group existed,
    so an absent `grouped` could mean "not grouped" or "this tool doesn't know
    about groups", and one emitted `grouped: true` with no `group_db` at all.
    Reported together, `group_db: null` says plainly that this workspace has
    its own database.
    """
    grouped = bool(st.settings.grouped)
    return {
        "grouped": grouped,
        "group_db": str(st.settings.db_path) if grouped else None,
    }


def _symbol_source_path(st: AppState, sym: dict) -> Path:
    """Filesystem path for a symbol body — uses owning project root when set."""
    root = sym.get("project_root")
    base = Path(root) if root else st.settings.workspace
    return base / sym["file_path"]


def _route_edge_peers(conn, symbol_id: int, *, incoming: bool) -> list[dict]:
    """v0.21 P2: ``invokes_route`` peers of a symbol, via a direct symbol_edge
    query (spans a shared group DB — symbol ids are global within a database).

    incoming=True → frontend call sites that hit this symbol as an HTTP
    endpoint; incoming=False → backend endpoints this symbol calls.
    """
    if incoming:
        join_col, filter_col = "e.src_symbol_id", "e.dst_symbol_id"
    else:
        join_col, filter_col = "e.dst_symbol_id", "e.src_symbol_id"
    rows = conn.execute(
        f"""SELECT s.qualified_name, f.path, e.weight
            FROM symbol_edge e
            JOIN symbol s ON s.id = {join_col}
            JOIN file f ON f.id = s.file_id
            WHERE {filter_col} = ? AND e.edge_type = 'invokes_route'
            ORDER BY e.weight DESC, s.qualified_name""",
        (symbol_id,),
    ).fetchall()
    return [
        {
            "qualified_name": r["qualified_name"],
            "file": r["path"],
            "confidence": round(float(r["weight"]), 3),
        }
        for r in rows
    ]


def _call_style_handler_qnames(st: AppState, project_id: int) -> set[str]:
    """Qualified names of Hono/Express handlers resolved like ``find_endpoints``.

    Protects import-mapped handlers (``wrap(details)`` → controller) that the
    short-name ``ts_registered_callback_names`` scan can miss. Pseudo
    ``file:line`` entries for inline arrows are skipped.
    """
    out: set[str] = set()
    workspace_path = st.settings.workspace
    for _framework, marker in (("hono", "hono"), ("express", "express")):
        for fr in st.conn.execute(
            """SELECT id, path, language FROM file
               WHERE project_id=? AND language IN
                 ('typescript', 'javascript', 'tsx')
               ORDER BY path""",
            (project_id,),
        ).fetchall():
            try:
                src = (workspace_path / fr["path"]).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            if marker not in src.lower():
                continue
            for rt in scan_hono_routes(src, fr["language"]):
                if not rt.get("handler_name"):
                    continue
                sym = _resolve_call_style_handler(
                    st.conn,
                    project_id,
                    int(fr["id"]),
                    fr["path"],
                    src,
                    fr["language"],
                    rt.get("handler_name"),
                    rt.get("handler_import") or rt.get("handler_name"),
                )
                if sym is None:
                    continue
                qn = sym["qualified_name"]
                if qn:
                    out.add(qn)
    return out


def _is_fixture_only_path(file_path: str) -> bool:
    """conftest / fixtures / test helpers — not runners to flag as orphan tests."""
    fp = file_path.replace("\\", "/").lstrip("/")
    base = fp.rsplit("/", 1)[-1]
    if base == "conftest.py" or base.startswith("conftest_"):
        return True
    for seg in ("/fixtures/", "/__fixtures__/", "/test_utils/", "/test_helpers/"):
        if seg in f"/{fp}":
            return True
    return False


_HARNESS_MARKERS = (
    "from fastmcp",
    "import fastmcp",
    "Client(mcp)",
    "Client(mcp,",
    "supertest",
    "request(app)",
    "TestClient(",
    "AsyncClient(",
)


def _file_has_harness_indirection(abs_path: Path) -> bool:
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")[:50_000]
    except OSError:
        return False
    return any(m in text for m in _HARNESS_MARKERS)


def _package_marker_is_emptyish(ws: Path, rel_path: str) -> bool:
    """True if index.ts/js is empty or re-export-only (safe to exclude from orphans)."""
    base = rel_path.rsplit("/", 1)[-1]
    if base not in ("index.ts", "index.js", "index.tsx", "index.jsx", "index.mjs"):
        return True  # non-index markers always treated as markers
    try:
        text = (ws / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"^\s*export\s+\{[^}]*\}\s*;?\s*$", "", body, flags=re.MULTILINE)
    body = re.sub(
        r"^\s*export\s+\*\s+from\s+['\"][^'\"]+['\"]\s*;?\s*$",
        "",
        body,
        flags=re.MULTILINE,
    )
    body = re.sub(
        r"^\s*export\s+\{[^}]*\}\s+from\s+['\"][^'\"]+['\"]\s*;?\s*$",
        "",
        body,
        flags=re.MULTILINE,
    )
    return not body.strip()


#: Where Graphify writes by default. Used only to *tell* the caller a graph is
#: sitting there — never to silently consume it. An index that quietly changed
#: its answers because a file appeared on disk would be worse than one that
#: needs asking.
_DEFAULT_EXTERNAL_GRAPH = "graphify-out/graph.json"


def _resolve_corroboration_source(
    st: AppState, explicit: str | None
) -> tuple[str | None, str | None]:
    """Pick the external graph to use, and a hint when one is merely available.

    Returns ``(path_or_None, hint_or_None)``. Precedence: the explicit argument,
    then ``[graph] external`` in ``.livespec.toml``. A graph sitting at
    Graphify's default output path is reported as a hint only — corroboration
    changes what the tool reports, so it stays opt-in.
    """
    if explicit:
        return explicit, None

    from livespec_mcp.config import load_repo_config

    configured = load_repo_config(st.settings.workspace).external_graph
    if configured:
        return configured, None

    default_path = st.settings.workspace / _DEFAULT_EXTERNAL_GRAPH
    if default_path.is_file():
        return None, (
            f"An external code graph is available at {_DEFAULT_EXTERNAL_GRAPH}. "
            "Pass corroborate_with to drop candidates a second extractor still "
            'sees referenced, or set `[graph] external = '
            f'"{_DEFAULT_EXTERNAL_GRAPH}"` in .livespec.toml to use it by '
            "default."
        )
    return None, None


def _load_corroborating_graph(
    st: AppState, graph_path: str
) -> tuple[Any, dict[str, Any] | None]:
    """Shared load + sanity gate for both corroboration paths.

    Returns ``(graph, None)`` or ``(None, mcp_error)``. The overlap guard is the
    important half: a graph whose paths don't line up matches nothing, and
    "nothing matched" would otherwise be reported as "nothing to drop", which
    reads as a clean bill of health for candidates nobody actually checked.
    """
    from livespec_mcp.domain.external_graph import (
        load_external_graph,
        overlap_ratio,
    )

    resolved = Path(graph_path)
    if not resolved.is_absolute():
        resolved = st.settings.workspace / resolved

    try:
        graph = load_external_graph(resolved)
    except FileNotFoundError:
        return None, mcp_error(
            f"External graph not found: {resolved}",
            hint=(
                "Generate one with `/graphify <repo>` (writes "
                "graphify-out/graph.json), or pass an absolute path."
            ),
        )
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        return None, mcp_error(
            f"Could not read external graph {resolved}: {exc}",
            hint="Expected Graphify's NetworkX node-link graph.json.",
        )

    indexed_files = {
        r["path"]
        for r in st.conn.execute(
            "SELECT path FROM file WHERE project_id=?", (st.project_id,)
        )
    }
    overlap = overlap_ratio(graph, indexed_files)
    if overlap < 0.1:
        return None, mcp_error(
            f"External graph {resolved} shares almost no files with this index "
            f"({overlap:.0%} of its files are indexed here).",
            hint=(
                "It probably describes a different repo, or was built from a "
                "different root so its paths do not line up. Corroborating "
                "against it would vouch for nothing."
            ),
        )
    graph.file_overlap = overlap
    return graph, None


def _corroborate_orphan_tests(
    candidates: list[dict[str, Any]],
    *,
    st: AppState,
    graph_path: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop tests a second extractor sees reaching production code.

    The mirror image of dead-code corroboration. There the question is "does
    anything refer to this?" (inbound). Here it is "does this reach anything
    outside the tests?" (outbound) — which is exactly the claim
    ``find_orphan_tests`` makes and exactly what an in-process harness or a
    string-dispatched call breaks.
    """
    graph, err = _load_corroborating_graph(st, graph_path)
    if err is not None:
        return candidates, err

    def _is_production(node: Any) -> bool:
        path = node.source_file or ""
        return bool(path) and not _is_test_file_path(path)

    survivors: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    matched = 0
    by_relation: dict[str, int] = {}

    for meta in candidates:
        name = (meta.get("qualified_name") or "").split(".")[-1]
        node = graph.lookup(meta["file_path"], int(meta.get("start_line") or 0), name)
        if node is None:
            # Position is unreliable for tests (decorators, parametrize), so
            # fall back to a name lookup anywhere in the same file.
            node = graph.lookup(meta["file_path"], -1, name)
        if node is None:
            survivors.append(meta)
            continue
        matched += 1
        reached = graph.reaches(node, _is_production)
        if not reached:
            survivors.append(meta)
            continue
        for relation, _ in reached:
            by_relation[relation] = by_relation.get(relation, 0) + 1
        dropped.append(
            {
                "qualified_name": meta["qualified_name"],
                "file_path": meta["file_path"],
                "reaches": sorted(
                    {f"{rel} -> {tgt.source_file}" for rel, tgt in reached}
                )[:5],
            }
        )

    report: dict[str, Any] = {
        "source": graph.path,
        "external_nodes": graph.node_count,
        "file_overlap": round(graph.file_overlap, 3),
        "candidates_before": len(candidates),
        "candidates_matched": matched,
        "dropped_as_reaching_production": len(dropped),
        "dropped_by_relation": dict(sorted(by_relation.items())),
        "dropped_sample": dropped[:20],
        "hint": (
            "Dropped tests reach a non-test file in the external graph via an "
            "edge livespec's cone did not follow. Depth 1 only, so each rescue "
            "is individually checkable."
        ),
    }
    if graph.has_non_ast_origin:
        report["warning"] = (
            "Some external edges are not marked `_origin: ast` — this graph "
            "may include LLM-derived (semantic) edges."
        )
    return survivors, report


def _corroborate_dead_code(
    candidates: list[dict[str, Any]],
    *,
    st: AppState,
    graph_path: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop candidates a second extractor still sees referenced.

    Returns ``(surviving_candidates, report)``. On any failure the report is a
    shaped ``mcp_error`` and the caller returns it untouched — an unreadable or
    mismatched external file must never be reported as "nothing to corroborate",
    which would read as a clean bill of health.
    """
    graph, err = _load_corroborating_graph(st, graph_path)
    if err is not None:
        return candidates, err

    survivors: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    matched = 0
    by_relation: dict[str, int] = {}

    for meta in candidates:
        node = graph.lookup(
            meta["file_path"], int(meta["start_line"]), meta.get("name") or ""
        )
        if node is None:
            survivors.append(meta)
            continue
        matched += 1
        evidence = graph.evidence_for(node)
        if not evidence:
            survivors.append(meta)
            continue
        for rel in set(evidence):
            by_relation[rel] = by_relation.get(rel, 0) + 1
        dropped.append(
            {
                "qualified_name": meta["qualified_name"],
                "file_path": meta["file_path"],
                "start_line": meta["start_line"],
                "relations": sorted(set(evidence)),
            }
        )

    report: dict[str, Any] = {
        "source": graph.path,
        "external_nodes": graph.node_count,
        "external_edges": graph.edge_count,
        "file_overlap": round(graph.file_overlap, 3),
        "candidates_before": len(candidates),
        "candidates_matched": matched,
        "dropped_as_referenced": len(dropped),
        "dropped_by_relation": dict(sorted(by_relation.items())),
        "dropped_sample": dropped[:20],
        "hint": (
            "Dropped candidates are referenced in the external graph by a "
            "relation livespec does not model (inheritance, type position, "
            "unresolved cross-file call). This is corroborating evidence, not "
            "proof of production traffic — still confirm with APM before "
            "deleting."
        ),
    }
    if graph.has_non_ast_origin:
        # Graphify's code pass is pure tree-sitter; its semantic pass over
        # docs/media can involve an LLM. Say so rather than let a
        # zero-LLM guarantee quietly become a zero-LLM-ish one.
        report["warning"] = (
            "Some external edges are not marked `_origin: ast` — this graph "
            "may include LLM-derived (semantic) edges, unlike a code-only "
            "Graphify run."
        )
    return survivors, report


def _attach_dead_code_not_swept(
    payload: dict[str, Any],
    *,
    st: AppState,
    rows: list,
    total: int,
    include_non_python: bool,
    include_public: bool,
    fs_routing_skipped: int = 0,
) -> None:
    """When `find_dead_code` returns a zero count because a default filter
    excluded the whole corpus (not because the corpus is clean), say so.

    Also reports ``skipped_fs_routing_count`` whenever filesystem-routing
    symbols were filtered (even if ``count > 0``).
    """
    if fs_routing_skipped:
        payload["skipped_fs_routing_count"] = fs_routing_skipped
        payload.setdefault(
            "hint",
            "pass include_ts_framework_routes=True to include Fresh/Next/"
            "SvelteKit/Remix filesystem-routing symbols",
        )

    if total != 0:
        return
    not_swept: list[str] = []
    hints: list[str] = []

    if not include_non_python:
        lang_rows = st.conn.execute(
            "SELECT language, COUNT(*) AS c FROM file WHERE project_id=? GROUP BY language",
            (st.project_id,),
        ).fetchall()
        lang_counts = {r["language"]: r["c"] for r in lang_rows}
        py_files = lang_counts.pop("python", 0)
        non_py_total = sum(lang_counts.values())
        if non_py_total > 0:
            top_lang, top_count = max(lang_counts.items(), key=lambda kv: kv[1])
            not_swept.append("non-python")
            hints.append(
                f"pass include_non_python=True — {non_py_total} of "
                f"{non_py_total + py_files} indexed files are non-Python "
                f"(mostly {top_lang}, {top_count}); the dead-code scan is "
                "Python-only by default"
            )

    if not include_public:
        public_candidates = sum(1 for r in rows if r["visibility"] in _PUBLIC_VIS)
        if public_candidates:
            not_swept.append("public")
            hints.append(
                f"pass include_public=True — {public_candidates} zero-caller "
                "candidate(s) are public/exported symbols excluded by default"
            )

    if fs_routing_skipped:
        not_swept.append("ts_framework_routes")
        hints.append(
            f"pass include_ts_framework_routes=True — {fs_routing_skipped} "
            "filesystem-routing symbol(s) skipped by default"
        )

    if not_swept:
        payload["not_swept"] = not_swept
        existing = payload.get("hint")
        joined = " | ".join(hints)
        payload["hint"] = f"{existing} | {joined}" if existing and hints else (existing or joined)


def _attach_endpoints_not_swept(payload: dict[str, Any], *, st: AppState, framework: str | None, total: int) -> None:
    """No-op: Express/Hono are included in the ``framework=None`` sweep.

    Kept so existing call sites stay valid after the opt-in→default change.
    """
    del payload, st, framework, total


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_symbol(
        query: SymbolQuery,
        kind: str | None = None,
        limit: int = 50,
        cursor: Cursor = 0,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Search symbols by name substring or qualified name.

        Returns lightweight refs (qualified_name, file, line, signature, kind).
        Use `get_symbol_info` for full details on a single match.

        Paginated like the aggregator tools: ``count`` is the exact number of
        matches regardless of the page, ``limit`` caps the page, ``cursor``
        resumes from a prior call's ``next_cursor`` (null when exhausted). A
        `limit` with no count used to make a truncated answer look complete.

        When the workspace uses ``[workspace] group_db``, search spans every
        project in the shared DB (home project ranked first by qname length
        only — matches may include a ``project`` field).

        v0.7 (B5): separator-agnostic match. The query and the qualified_name
        are both normalized so that `Type::method`, `Type.method`, and
        `module/Type::method` all match the same symbols. Useful in Rust
        repos where qnames mix `.` (file path) and `::` (impl method)
        separators.""" + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        pids = st.group_project_ids()
        # Clamp limit: a negative value becomes SQLite `LIMIT -1` (unbounded).
        safe_limit = max(1, min(int(limit), 1000))

        # Normalize separators so `::` queries match `.`-separated stored
        # qnames and vice-versa. SQLite's LIKE doesn't support regex, so we
        # use the REPLACE() function on the column to compare normalized
        # forms. The query is normalized in Python before binding. Escape LIKE
        # wildcards (% and _) so a literal query containing them matches
        # literally instead of acting as a wildcard.
        normalized_query = query.replace("::", ".").replace("/", ".")
        raw_like = f"%{_like_escape(query)}%"
        norm_like = f"%{_like_escape(normalized_query)}%"
        placeholders = ",".join("?" for _ in pids)
        sql = [
            f"""SELECT s.id, s.name, s.qualified_name, s.kind, s.signature,
                      s.start_line, s.end_line, f.path as file_path,
                      p.name AS project, p.root AS project_root
               FROM symbol s JOIN file f ON f.id=s.file_id
               JOIN project p ON p.id=f.project_id
               WHERE f.project_id IN ({placeholders}) AND (
                   s.name LIKE ? ESCAPE '\\'
                   OR s.qualified_name LIKE ? ESCAPE '\\'
                   OR REPLACE(s.qualified_name, '::', '.') LIKE ? ESCAPE '\\'
               )"""
        ]
        args: list[Any] = [*pids, raw_like, raw_like, norm_like]
        if kind:
            sql.append("AND s.kind = ?")
            args.append(kind)
        body = " ".join(sql)
        total = int(
            st.conn.execute(
                f"SELECT COUNT(*) AS n FROM ({body})", args
            ).fetchone()["n"]
        )
        offset = max(0, int(cursor))
        rows = st.conn.execute(
            f"{body} ORDER BY length(s.qualified_name) LIMIT ? OFFSET ?",
            [*args, safe_limit, offset],
        ).fetchall()
        matches = [dict(r) for r in rows]
        if not st.settings.grouped:
            # Outside a group every match is relative to this workspace, so
            # `project_root` would repeat it on every row. Inside a group it is
            # what makes a match from another repo openable at all.
            for m in matches:
                m.pop("project_root", None)
        next_cursor = offset + safe_limit if offset + safe_limit < total else None
        out: dict[str, Any] = {
            "matches": matches,
            "count": total,
            "next_cursor": next_cursor,
            **group_fields(st),
        }
        if not rows:
            # v0.14: zero matches on the project's own fuzzy-lookup tool is
            # a dead end for an agent — surface typo-distance suggestions
            # the same way the not-found errors do. Not an error payload:
            # empty matches is a valid result, did_you_mean rides along.
            suggestions = did_you_mean_symbols(st.conn, pids, query)
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
        pids = st.group_project_ids()
        sym = _resolve_symbol(st.conn, pids, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pids, qname)
        try:
            fp = _symbol_source_path(st, sym)
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(sym["start_line"] - 1, 0)
            end = min(sym["end_line"], len(lines))
            source = "\n".join(lines[start:end])
        except OSError as e:
            return mcp_error(
                f"file unreadable: {sym['file_path']}",
                hint=str(e),
            )
        out = {
            "qualified_name": sym["qualified_name"],
            "file_path": sym["file_path"],
            "start_line": sym["start_line"],
            "end_line": sym["end_line"],
            "source": source,
            "body_hash": sym["body_hash"],
        }
        if st.settings.grouped and sym.get("project_root"):
            out["project_root"] = sym["project_root"]
        return out

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def read_unit(
        qname: str,
        depth: int = 1,
        token_budget: int = 2000,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """The contract closure for a symbol — what it takes to change it.

        Reading code by file is a human affordance. An agent asked to change
        one function needs its body, the *signatures* of what it calls, the
        *definitions* of the types in those signatures, what it can raise, and
        which tests cover it. This returns exactly that set and nothing else,
        so a well-factored repo stops costing more to read than a badly-
        factored one.

        `depth` bounds how far type resolution follows types named inside
        other type definitions; `depth=0` skips type bodies entirely.
        `token_budget` caps the rendered size — over budget, the farthest
        callees are dropped first (a call in the same file is likelier to
        matter to the edit than one in another package) and `budget.degraded`
        says it happened.

        Types this project does not define (`str`, `Promise`, `Path`) are
        reported under `external_types`, not as misses. Types that *should*
        have resolved and did not are listed in `unresolved_types` — read them
        before relying on their shape rather than assuming the closure is
        complete.
        """ + WORKSPACE_DOCSTRING_NOTE
        from livespec_mcp.domain.contract_closure import build_closure

        st = get_state(workspace)
        pids = st.group_project_ids()
        sym = _resolve_symbol(st.conn, pids, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pids, qname)
        if depth < 0:
            return mcp_error("depth must be >= 0", hint="use depth=0 to skip type bodies")
        if token_budget < 200:
            return mcp_error(
                "token_budget must be >= 200",
                hint="the body alone rarely fits under 200 tokens",
            )

        # Same rule as `_symbol_source_path`: the owning project root when the
        # symbol carries one, else the workspace. Derived directly rather than
        # by walking back up from the file path, which breaks the moment a
        # path has a different depth than the walk assumes.
        root = (
            Path(sym["project_root"]) if sym.get("project_root")
            else st.settings.workspace
        )

        closure = build_closure(
            st.conn, tuple(pids), sym, root,
            depth=depth, token_budget=token_budget,
        )
        out = closure.as_dict()
        if st.settings.grouped and sym.get("project_root"):
            out["project_root"] = sym["project_root"]
        return out

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def search_similar(
        code: str,
        threshold: float = 0.80,
        limit: int = 5,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Does this already exist? Ask before writing a helper.

        An agent rewrites a helper that already exists because it has no cheap
        way to ask. Grep answers on names, and the whole point is that the
        duplicate has a *different* name — that is why it got written.

        Two levels run here, both fast enough to sit in front of a write:

        - **level 0** hashes the body's structure with identifiers replaced by
          their binding position, so a literal copy with everything renamed
          still matches. Reported at similarity 1.0.
        - **level 1** compares winnowed k-gram fingerprints, catching a copy
          that was edited, reordered or padded after being pasted.

        Tuned to be quiet on purpose. A missed duplicate costs some redundancy;
        a wrong "this already exists" blocks work that was right, and two of
        those teach a user to switch the check off — after which it catches
        nothing. Short bodies are excluded from level 1 entirely, because every
        two-line guard clause has the same shape as every other.

        Semantic duplication (same intent, unrelated code) is deliberately not
        here: it costs seconds, and seconds in front of a write is a feature
        that gets disabled.
        """ + WORKSPACE_DOCSTRING_NOTE
        from livespec_mcp.domain.duplication import find_duplicates as _find
        from livespec_mcp.domain.duplication import fingerprint as _fp
        from livespec_mcp.domain.duplication import load_corpus as _load_corpus

        if not code or not code.strip():
            return mcp_error("code is empty", hint="pass the body you are about to write")
        if not 0.0 < threshold <= 1.0:
            return mcp_error("threshold must be in (0, 1]", hint="0.80 is the default")

        st = get_state(workspace)
        candidate = _fp(code, language="python")
        corpus = _load_corpus(
            st.conn, tuple(st.group_project_ids()), st.settings.workspace
        )

        matches = _find(candidate, corpus, threshold=threshold, limit=limit)
        return {
            "matches": [
                {
                    "qualified_name": m.qualified_name,
                    "file_path": m.file_path,
                    "level": m.level,
                    "similarity": round(m.similarity, 3),
                    "reason": m.reason,
                    "next": f'read_unit(qname="{m.qualified_name}")',
                }
                for m in matches
            ],
            "searched": len(corpus),
            "verdict": (
                "already exists — import it instead of rewriting"
                if matches else "nothing structurally similar in the index"
            ),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def resolve_location(
        path: str,
        line: int,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Which symbol owns `path:line` — the inverse of every other lookup.

        Stack traces, linter output, CI logs and coverage reports all speak
        file-and-line. Without this, an agent working through symbols has to
        fall back to opening the file the moment anything goes wrong, which is
        the one habit reading-by-symbol is meant to replace.

        Returns the innermost symbol containing the line (a method rather than
        its class), plus the enclosing symbols outward, so a line inside a
        nested function still resolves to something callable.
        """ + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        pids = st.group_project_ids()
        if line < 1:
            return mcp_error("line must be >= 1", hint="lines are 1-indexed")

        needle = path.strip().lstrip("./")
        placeholders = ",".join("?" * len(pids))
        rows = st.conn.execute(
            f"SELECT s.qualified_name, s.name, s.kind, s.signature, "
            f"       s.start_line, s.end_line, f.path "
            f"FROM symbol s JOIN file f ON f.id = s.file_id "
            f"WHERE f.project_id IN ({placeholders}) "
            f"  AND (f.path = ? OR f.path LIKE ?) "
            f"  AND s.start_line <= ? AND s.end_line >= ? "
            f"ORDER BY (s.end_line - s.start_line) ASC",
            (*pids, needle, f"%{needle}", line, line),
        ).fetchall()

        if not rows:
            return {
                "found": False,
                "path": path,
                "line": line,
                "hint": (
                    "no indexed symbol spans that line — the file may be "
                    "outside the indexed scope, or the index may be stale "
                    "(run index_project)"
                ),
            }

        innermost = rows[0]
        return {
            "found": True,
            "path": innermost["path"],
            "line": line,
            "symbol": {
                "qualified_name": innermost["qualified_name"],
                "kind": innermost["kind"],
                "signature": innermost["signature"] or "",
                "start_line": innermost["start_line"],
                "end_line": innermost["end_line"],
            },
            "enclosing": [
                {"qualified_name": r["qualified_name"], "kind": r["kind"]}
                for r in rows[1:]
            ],
            "next": f'read_unit(qname="{innermost["qualified_name"]}")',
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def who_calls(
        qname: QName,
        max_depth: MaxDepth = 1,
        limit: Limit = 200,
        cursor: Cursor = 0,
        summary_only: SummaryOnly = False,
        min_weight: MinWeight = 0.6,
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
        pids = st.group_project_ids()
        sym = _resolve_symbol(st.conn, pids, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pids, qname)
        graph_pid = _graph_project_id(sym, st.project_id)
        view = load_graph(st.conn, graph_pid)
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
        payload = {
            "root": sym["qualified_name"],
            "max_depth": max_depth,
            "callers": page,
            "count": total,
            "next_cursor": next_cursor,
        }
        # v0.21 P2: cross-repo route callers — frontend call sites that hit this
        # symbol as an HTTP endpoint (invokes_route edges). Direct symbol_edge
        # query so it spans a shared group DB without the NetworkX graph.
        route_callers = _route_edge_peers(st.conn, sid, incoming=True)
        if route_callers:
            payload["route_callers"] = route_callers
        return _attach_payload_warning(
            payload,
            _payload_warning(total, limit=limit, summary_only=summary_only),
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def who_does_this_call(
        qname: QName,
        max_depth: MaxDepth = 1,
        limit: Limit = 200,
        cursor: Cursor = 0,
        summary_only: SummaryOnly = False,
        min_weight: MinWeight = 0.6,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Symbols that `qname` calls (transitive forward cone up to max_depth).

        Forward-direction counterpart of `who_calls`. Same v0.9 P2
        pagination contract: ``limit`` / ``cursor`` / ``summary_only``.
        Same v0.9 P3 fan-out filter: ``min_weight=0.6`` by default.
        """
        st = get_state(workspace)
        pids = st.group_project_ids()
        sym = _resolve_symbol(st.conn, pids, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pids, qname)
        graph_pid = _graph_project_id(sym, st.project_id)
        view = load_graph(st.conn, graph_pid)
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
        payload = {
            "root": sym["qualified_name"],
            "max_depth": max_depth,
            "callees": page,
            "count": total,
            "next_cursor": next_cursor,
        }
        # v0.21 P2: backend endpoints this symbol invokes over HTTP
        # (invokes_route edges) — the cross-repo forward direction.
        endpoints = _route_edge_peers(st.conn, sid, incoming=False)
        if endpoints:
            payload["invokes_endpoints"] = endpoints
        return payload

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
        pids = st.group_project_ids()
        sym = _resolve_symbol(st.conn, pids, qname)
        if not sym:
            return symbol_not_found_error(st.conn, pids, qname)
        sid = int(sym["id"])
        graph_pid = _graph_project_id(sym, st.project_id)
        view = load_graph(st.conn, graph_pid)
        ranks = graph_pagerank(view) if sid in view.g else {}

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
        max_depth: MaxDepth = 5,
        limit: Limit = 200,
        cursor: Cursor = 0,
        summary_only: SummaryOnly = False,
        min_weight: MinWeight = 0.6,
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
        pids = st.group_project_ids()
        view = load_graph(st.conn, pid)

        def specs_for_symbols(ids: set[int]) -> list[dict]:
            if not ids:
                return []
            return [
                dict(r)
                for r in _select_in_chunks(
                    st.conn,
                    """SELECT DISTINCT r.spec_id, r.title, r.status, r.priority
                        FROM spec_symbol rs JOIN spec r ON r.id=rs.spec_id
                        WHERE rs.symbol_id IN ({in})""",
                    ids,
                )
            ]

        def _paginate_meta(ids: set[int], graph_view: GraphView) -> tuple[list[dict], int, int | None]:
            """Sort + slice. Returns (page, total, next_cursor)."""
            sorted_meta = sorted(
                (graph_view.sym_meta[i] for i in ids if i in graph_view.sym_meta),
                key=lambda m: (m.get("file_path", ""), m.get("start_line", 0)),
            )
            total = len(sorted_meta)
            page = sorted_meta[cursor : cursor + limit]
            next_c = cursor + limit if cursor + limit < total else None
            return page, total, next_c

        if target_type == "symbol":
            sym = _resolve_symbol(st.conn, pids, target)
            if not sym:
                return symbol_not_found_error(st.conn, pids, target)
            sid = int(sym["id"])
            graph_pid = _graph_project_id(sym, pid)
            view = load_graph(st.conn, graph_pid)
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
            callers_page, callers_total, callers_next = _paginate_meta(impacted, view)
            calls_page, calls_total, calls_next = _paginate_meta(forward, view)
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
            callers_page, callers_total, callers_next = _paginate_meta(impacted, view)
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
            sid_rows = _select_in_chunks(
                st.conn,
                "SELECT DISTINCT symbol_id FROM spec_symbol WHERE spec_id IN ({in})",
                all_spec_ids,
            )
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
                    # v0.20 H13: honor min_weight like the symbol/file branches
                    # so weight-0.5 resolver fan-out doesn't bloat the cone.
                    forward |= descendants_within(view.g, sid, max_depth, min_weight=min_weight)
                    backward |= ancestors_within(view.g, sid, max_depth, min_weight=min_weight)

            dep_spec_meta: list[dict[str, Any]] = []
            if dependent_spec_ids:
                dep_spec_meta = [
                    dict(r)
                    for r in _select_in_chunks(
                        st.conn,
                        """SELECT spec_id, title, status, priority FROM spec
                            WHERE id IN ({in})""",
                        dependent_spec_ids,
                    )
                ]

            impl_ids = {n for n in sids if n in view.sym_meta}
            # v0.20 H13: the spec branch was returning three FULL unpaginated
            # symbol lists (depth-5 cones unioned over every linked symbol AND
            # every dependent spec) — the exact 4-7M-char payload class the v0.7
            # pagination contract exists to prevent. Honor summary_only + the
            # limit/cursor page + exact counts, like the symbol/file branches.
            if summary_only:
                return {
                    "spec_id": spec["spec_id"],
                    "dependent_specs": dep_spec_meta,
                    "counts": {
                        "implementing_symbols": len(impl_ids),
                        "downstream": len([n for n in forward if n in view.sym_meta]),
                        "upstream_callers": len([n for n in backward if n in view.sym_meta]),
                    },
                }
            impl_page, impl_total, impl_next = _paginate_meta(impl_ids, view)
            down_page, down_total, down_next = _paginate_meta(forward, view)
            up_page, up_total, up_next = _paginate_meta(backward, view)
            warn = _payload_warning(
                max(impl_total, down_total, up_total),
                limit=limit,
                summary_only=summary_only,
            )
            return _attach_payload_warning(
                {
                    "spec_id": spec["spec_id"],
                    "dependent_specs": dep_spec_meta,
                    "implementing_symbols": impl_page,
                    "downstream": down_page,
                    "upstream_callers": up_page,
                    "counts": {
                        "implementing_symbols": impl_total,
                        "downstream": down_total,
                        "upstream_callers": up_total,
                    },
                    "next_cursor": next(
                        (c for c in (impl_next, down_next, up_next) if c is not None),
                        None,
                    ),
                },
                warn,
            )
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
          actually filtered come back in `structural_patterns_filtered`.
        - test-file symbols (`createMockDb`, `signTestToken`,
          `fakeAuthMiddleware`, …). Test scaffolding ranks high by PageRank
          but is the opposite of "what is this repo's core". No opt-out;
          the ones that outranked the returned top-N are listed by
          qualified name in `test_symbols_filtered`.""" + WORKSPACE_DOCSTRING_NOTE
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
        include_ts_framework_routes: bool = False,
        include_tests: bool = False,
        min_weight: float = 0.0,
        corroborate_with: str | None = None,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Symbols with zero callers and zero Spec links — removal candidates.

        Filters out, by default:
        - **Test files** (``tests/``, ``src/test/``, ``*.test.ts``,
          ``*_test.go``, ``*Test.java``, … — same heuristic as
          ``find_orphan_tests``). Tests almost never have production callers;
          flagging them as dead is noise. Pass ``include_tests=True`` to keep
          them.
        - Files under `scripts/`, `bin/`; `__main__.py`; `manage.py`
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
          `include_non_python=True` to surface them. **Unreleased:** when
          the workspace has zero Python files, this flag auto-enables
          (see ``auto_enabled`` in the payload).
        - **TS framework filesystem-routing files** (v0.11 P1, bug #19).
          Functions/classes in Fresh ``islands/``, Next.js ``pages/`` /
          ``app/``, SvelteKit ``routes/``, and Remix ``app/routes/`` are
          reachable via filesystem routing, not call edges. Skipped by
          default; pass `include_ts_framework_routes=True` to surface them
          (independent of `include_infrastructure`).
        - With `include_non_python=True`, Hono/Express named handlers resolved
          the same way as `find_endpoints` are protected (import-map aware).

        `min_weight` (default 0.0): when >0, an inbound edge below this weight
        does not count as a caller (aligns with graph impact filters; try 0.6).

        v0.7 (B3): paginated. `limit` (default 200) caps `dead_symbols` per
        call; `cursor` resumes from a previous call's `next_cursor`;
        `summary_only=True` returns just the count + breakdown without the
        list. The total count is always exact, regardless of pagination.

        Unreleased: `filtered_out` reports how many candidates each default
        filter excluded, keyed by the flag that would include them
        (`tests`, `non_python`, `public`, `infrastructure`, `fs_routing`).
        Two runs on the same repo can differ several-fold purely by flags;
        this field says which run you are looking at. Counts only, so it is
        safe in `summary_only` mode (where it is also returned).

        Unreleased: `corroborate_with=<path to a Graphify graph.json>` drops
        candidates that a second, independent extractor still sees referenced.
        livespec's blind spots are systematic — no inheritance edge, no
        type-position usage, some cross-file calls lost by the resolver — and
        each one manufactures a false "dead". Graphify's code pass is
        tree-sitter with no LLM, so this costs nothing in determinism. Results
        report `corroboration` (source, match rate, what was dropped and by
        which relation). Evidence, not proof: still confirm with APM before
        deleting.
        """ + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        pid = st.project_id
        auto_enabled: list[str] = []
        # TS/JS-only repos: Python-only default is a silent false zero. Auto-
        # enable non-Python sweep (audit: Composer reported count=0 + not_swept).
        if not include_non_python:
            py_n = int(
                st.conn.execute(
                    "SELECT COUNT(*) AS c FROM file WHERE project_id=? AND language='python'",
                    (pid,),
                ).fetchone()["c"]
            )
            if py_n == 0:
                include_non_python = True
                auto_enabled.append("include_non_python")
        edge_extra = ""
        edge_params: list[Any] = [pid]
        if min_weight > 0.0:
            edge_extra = " AND e.weight >= ?"
            edge_params = [pid, float(min_weight)]
        rows = st.conn.execute(
            f"""SELECT s.id, s.qualified_name, s.name, s.kind, s.decorators,
                      s.visibility, s.start_line, s.end_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=?
                 AND NOT EXISTS (
                   SELECT 1 FROM symbol_edge e
                   WHERE e.dst_symbol_id=s.id{edge_extra}
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM spec_symbol rs WHERE rs.symbol_id=s.id
                 )
               ORDER BY f.path, s.start_line""",
            tuple(edge_params),
        ).fetchall()

        def is_entry_point_path(p: str) -> bool:
            if not include_tests and _is_test_file_path(p):
                return True
            return (
                p.startswith(("bin/", "scripts/"))
                or "/bin/" in p
                or "/scripts/" in p
                or p.endswith("/__main__.py")
                or p == "__main__.py"
                or p.endswith("/manage.py")
                or p == "manage.py"
            )

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
            "SELECT f.path, f.mtime FROM file f WHERE f.project_id=? AND f.path LIKE '%.py'",
            (pid,),
        ):
            try:
                abs_path = str(workspace_path / path_row["path"])
                # mtime (from the DB, taken at index time) keys the shared parse
                # cache so the five scans below parse each file once, not five
                # times, and reparse after an edit + re-index.
                mtime = float(path_row["mtime"])
                global_module_refs |= _module_level_referenced_names(abs_path, mtime)
                decorator_aliases |= _entry_point_decorator_aliases(abs_path, mtime)
                # v0.10: explicit public-surface markers (re-exports +
                # __all__) protect library-side classes that have no
                # in-tree caller because their callers are user code.
                global_module_refs |= _publicly_exported_names(abs_path, mtime)
                # v0.11 P3: runtime-registration patterns — class/fn passed
                # to a framework method so the framework calls it later.
                # Covers Field.register_lookup(MyLookup), signal.connect(h),
                # app.add_middleware(M), etc.
                global_module_refs |= _runtime_registered_names(abs_path, mtime)
                nested_uses = _used_nested_def_names(abs_path, mtime)
                if nested_uses:
                    nested_uses_by_file[path_row["path"]] = nested_uses
            except Exception:
                # Bad file paths shouldn't kill the whole audit.
                continue

        # v0.13 P3: TS/JS runtime-registration scan. v0.14: plus the
        # closure-capture scan (nested fn referenced in parent body) for
        # TS/JS/TSX and Rust — Go has no named nested fns, nothing to scan.
        # Only pay the file reads when non-Python symbols are in scope.
        call_style_qnames: set[str] = set()
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
            call_style_qnames = _call_style_handler_qnames(st, pid)

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
        # indexer can't parse may call any of them. Injectable (DI) is the
        # same shape for service methods. Lifecycle hooks stay protected on
        # any Angular decorator (incl. NgModule).
        # Spring stereotypes (@Service/@Repository/@RestController/…): DI
        # container may invoke any public method with zero in-project edges.
        ng_template_classes: set[str] = set()
        ng_any_classes: set[str] = set()
        ng_di_classes: set[str] = set()
        spring_stereotype_classes: set[str] = set()
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
            if segs & _NG_DI_CLASS_LASTSEGS:
                ng_di_classes.add(r["qualified_name"])
            if segs & _NG_ANY_DECORATOR_LASTSEGS:
                ng_any_classes.add(r["qualified_name"])
            if segs & _SPRING_STEREOTYPE_LASTSEGS:
                spring_stereotype_classes.add(r["qualified_name"])

        alias_lastsegs = frozenset(decorator_aliases)
        filtered: list[dict[str, Any]] = []
        fs_routing_skipped = 0
        # Unreleased: attribute every flag-flippable skip to the flag that would
        # include it. Two legitimate runs of this tool on the same repo can
        # differ several-fold purely by flags; without this the caller cannot
        # tell which number it is looking at. Counts only (never name lists) so
        # the payload stays bounded.
        filtered_out: dict[str, int] = {}

        def _drop(reason: str) -> None:
            filtered_out[reason] = filtered_out.get(reason, 0) + 1

        for r in rows:
            meta = dict(r)
            # Pulled ahead of is_entry_point_path (which also returns True for
            # test paths when include_tests is False) purely to attribute the
            # skip; the set of skipped symbols is unchanged.
            if not include_tests and _is_test_file_path(meta["file_path"]):
                _drop("tests")
                continue
            if is_entry_point_path(meta["file_path"]):
                continue
            if _is_bundler_output_path(meta["file_path"]):
                continue
            # v0.11 P1 (bug #19): TS framework filesystem-routing entry points.
            if not include_ts_framework_routes and _is_ts_framework_entry_point(meta):
                fs_routing_skipped += 1
                _drop("fs_routing")
                continue
            if not include_non_python and not meta["file_path"].endswith(".py"):
                _drop("non_python")
                continue
            if not include_infrastructure and _is_implicit_entry_point(meta):
                _drop("infrastructure")
                continue
            if not include_infrastructure and _has_entry_point_decorator(
                meta.get("decorators"), alias_lastsegs
            ):
                _drop("infrastructure")
                continue
            # v0.13 P0: the symbol itself is decorator machinery (an alias
            # target or an IfExp branch like `_noop_decorator`).
            if not include_infrastructure and meta["name"].lower() in alias_lastsegs:
                _drop("infrastructure")
                continue
            if not include_public and (meta.get("visibility") in _PUBLIC_VIS):
                _drop("public")
                continue
            if meta["qualified_name"] in call_style_qnames:
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
                    _drop("infrastructure")
                    continue
                # v0.8 P2 fix #11: nested-fn closure callback. A function
                # defined inside another function whose name is referenced
                # within the parent's body (e.g. `Watcher(on_reindex=_do)`)
                # is reachable as a callback even with zero call-edges.
                # Per-file lookup so nested names don't cross-collide.
                file_nested = nested_uses_by_file.get(meta["file_path"])
                if file_nested and meta["name"] in file_nested:
                    _drop("infrastructure")
                    continue
                if meta["kind"] == "method" and len(qname_parts) >= 2:
                    parent_class_short = qname_parts[-2]
                    if parent_class_short in global_module_refs:
                        _drop("infrastructure")
                        continue
                    parent_class_qname = ".".join(qname_parts[:-1])
                    if parent_class_qname in protected_class_qnames:
                        _drop("infrastructure")
                        continue
                    # v0.13 P2: Angular template reachability + lifecycle.
                    if parent_class_qname in ng_template_classes:
                        _drop("infrastructure")
                        continue
                    if parent_class_qname in ng_di_classes:
                        _drop("infrastructure")
                        continue
                    if parent_class_qname in spring_stereotype_classes:
                        _drop("infrastructure")
                        continue
                    if (
                        meta["name"] in _NG_LIFECYCLE_HOOKS
                        and parent_class_qname in ng_any_classes
                    ):
                        _drop("infrastructure")
                        continue

            filtered.append(meta)

        corroboration: dict[str, Any] | None = None
        graph_path, corroboration_hint = _resolve_corroboration_source(
            st, corroborate_with
        )
        if graph_path:
            filtered, corroboration = _corroborate_dead_code(
                filtered, st=st, graph_path=graph_path
            )
            if corroboration.get("isError"):
                return corroboration

        total = len(filtered)
        # by_kind / by_dir breakdowns (cheap; useful for summary mode)
        by_kind: dict[str, int] = {}
        by_dir: dict[str, int] = {}
        for m in filtered:
            by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
            top_dir = m["file_path"].split("/", 1)[0]
            by_dir[top_dir] = by_dir.get(top_dir, 0) + 1

        payload: dict[str, Any] = {
            "count": total,
            "by_kind": by_kind,
            "by_top_dir": by_dir,
        }
        if auto_enabled:
            payload["auto_enabled"] = auto_enabled
        if corroboration is not None:
            payload["corroboration"] = corroboration
        elif corroboration_hint:
            payload["corroboration_available"] = corroboration_hint
        if filtered_out:
            # Present in summary_only too: this is precisely the field that
            # explains a surprising count, so stripping it in the cheap mode
            # would hide the answer from the caller who needs it most.
            payload["filtered_out"] = dict(sorted(filtered_out.items()))
            payload["filtered_out_hint"] = (
                "Candidates excluded by default filters, per flag that would "
                "include them: tests=include_tests, non_python="
                "include_non_python, public=include_public, infrastructure="
                "include_infrastructure, fs_routing="
                "include_ts_framework_routes. Counts are not mutually "
                "exclusive across runs — a symbol is attributed to the first "
                "filter that excluded it."
            )
        _attach_dead_code_not_swept(
            payload,
            st=st,
            rows=rows,
            total=total,
            include_non_python=include_non_python,
            include_public=include_public,
            fs_routing_skipped=fs_routing_skipped,
        )

        if summary_only:
            return payload

        page = filtered[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < total else None
        payload["dead_symbols"] = [
            {
                "qualified_name": m["qualified_name"],
                "kind": m["kind"],
                "file_path": m["file_path"],
                "start_line": m["start_line"],
                "end_line": m["end_line"],
            }
            for m in page
        ]
        payload["next_cursor"] = next_cursor
        return payload

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_endpoints(
        framework: Literal[
            "flask", "fastapi", "click", "pytest", "fastmcp", "celery", "django",
            "nextjs", "fresh", "sveltekit", "remix", "spring", "angular",
            "hono", "express", "gin", "echo", "chi", "nethttp",
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

        Pass `framework=None` (default) for the **HTTP-ish** surface: Flask /
        FastAPI / Spring mappings / Express / Hono / Go (gin·echo·chi·net/http)
        / FS-routing frameworks. Angular UI, Click CLI, FastMCP tools, Celery
        tasks, and Spring DI beans require an explicit ``framework=`` filter.

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

        v0.13 P2: ``framework='spring'`` surfaces Java Spring Boot *HTTP*
        annotations (@RestController, @GetMapping & friends — not
        @Bean/@Service/@Configuration; those stay protected in
        ``find_dead_code`` but are not listed as routes). Requires the v8
        re-extract. ``framework='angular'`` surfaces @Component /
        @Injectable / @Directive / @Pipe / @NgModule / @HostListener.

        Default sweep (``framework=None``) omits Spring DI stereotypes,
        Java ``@Component``, Angular UI, Click, FastMCP, and Celery.

        Unreleased: ``framework='gin'|'echo'|'chi'|'nethttp'`` (and
        ``None``) scan ``.go`` files for call-style routes
        (``r.GET("/x", h)``, ``http.HandleFunc(...)``).

        v0.13 P3: ``framework='hono'`` / ``'express'`` (and ``None``) scan
        indexed TS/JS files for call-style route registrations
        (``app.get('/users', handler)``, ``router.post(...)``). Each route
        reports ``http_method`` / ``http_path`` (plus ``hono_*`` /
        ``express_*``). Handlers resolve via the route file's import map.
        """
        st = get_state(workspace)

        endpoints = filter_api_endpoints(
            compute_endpoints(st, framework),
            framework,
            exclude_tests=True,
        )
        total = len(endpoints)
        payload: dict[str, Any] = {"framework": framework, "count": total}
        _attach_endpoints_not_swept(payload, st=st, framework=framework, total=total)
        # Spring (and other decorator frameworks) stay project-scoped. In a
        # group_db, agents often call from the hub repo — hint sibling roots.
        if (
            framework == "spring"
            and total == 0
            and st.settings.grouped
        ):
            java_elsewhere = st.conn.execute(
                """SELECT p.name AS project, COUNT(*) AS files
                   FROM file f JOIN project p ON p.id=f.project_id
                   WHERE f.project_id != ? AND f.language='java'
                   GROUP BY p.name ORDER BY files DESC LIMIT 5""",
                (st.project_id,),
            ).fetchall()
            if java_elsewhere:
                names = ", ".join(
                    f"{r['project']} ({r['files']} java files)" for r in java_elsewhere
                )
                payload["hint"] = (
                    f"no Spring endpoints in this project; Java is indexed in "
                    f"group siblings — call find_endpoints(workspace=<sibling>, "
                    f"framework='spring'). Found: {names}"
                )
                payload["group_java_projects"] = [
                    {"project": r["project"], "java_files": int(r["files"])}
                    for r in java_elsewhere
                ]
        if summary_only:
            return payload
        page = endpoints[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < total else None
        payload["endpoints"] = page
        payload["next_cursor"] = next_cursor
        return payload

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_legacy_flows(
        project: str | None = None,
        include_infra_routes: bool = False,
        include_orphan_clients: bool = True,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Likely-unused HTTP flows from indexed ``route_ref`` + ``invokes_route``.

        **Server legacy:** server routes with zero incoming cross-repo (or
        same-DB) client hops — nothing in this index calls that path.
        **Client orphans:** client calls with no matched server hop (dead
        front call or a SA missing from ``group_db``).

        Best with ``[workspace] group_db`` (polyrepo). Solo repos still work
        when front+back share one DB. This is **graph evidence**, not
        production traffic — confirm with APM/logs before deleting.

        ``project`` filters by project name/basename. Infra paths
        (``/health``, …) are dropped unless ``include_infra_routes=True``.
        Paginated like other aggregators (`limit`/`cursor`/`summary_only`).
        """ + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        raw = compute_legacy_flows(
            st.conn,
            project=project,
            include_infra_routes=include_infra_routes,
            include_orphan_clients=include_orphan_clients,
        )
        servers = raw["legacy_servers"]
        clients = raw["orphan_clients"]
        # Unified list for simple pagination (servers first).
        flows = [{**s, "flow_kind": "legacy_server"} for s in servers] + [
            {**c, "flow_kind": "orphan_client"} for c in clients
        ]
        total = len(flows)
        payload: dict[str, Any] = {
            **group_fields(st),
            "count": total,
            "legacy_server_count": raw["legacy_server_count"],
            "orphan_client_count": raw["orphan_client_count"],
            "server_route_count": raw["server_route_count"],
            "client_route_count": raw["client_route_count"],
            "live_server_count": raw["live_server_count"],
            "live_client_count": raw["live_client_count"],
            "hint": raw["hint"],
        }
        if summary_only:
            payload["legacy_servers_sample"] = servers[:10]
            payload["orphan_clients_sample"] = clients[:10]
            return payload
        page = flows[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < total else None
        payload["flows"] = page
        payload["next_cursor"] = next_cursor
        return payload

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def audit_coverage(
        limit: int = 200,
        cursor: int = 0,
        cursors: dict[str, int] | None = None,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Spec coverage audit: what's missing / under-confident.

        Six signals:
        - `modules_without_spec`: product files whose symbols have no DIRECT
          `spec_symbol` link (tests / ``scripts/`` / ``bench/`` excluded —
          see `modules_non_product`)
        - `modules_implicitly_covered`: subset of `modules_without_spec` whose
          symbols are called transitively by a spec-linked symbol — covered
          indirectly through the call graph (e.g. a data layer reached via
          API handlers that carry the `@spec:` annotation)
        - `modules_truly_orphan`: subset of `modules_without_spec` with NO direct
          link AND no transitive coverage — the actually-actionable list
        - `modules_non_product`: test/fixture/``scripts``/``bench`` files with
          no Spec link — counted separately so they do not inflate orphans
        - `modules_unsupported_language`: files in languages whose extractor
          does not yet read in-source `@spec:` annotations (everything outside
          Python / JS / TS / Java today). Listed separately so they aren't reported
          as orphans — the gap is in the extractor, not the project.
        - `specs_without_implementation`: Specs with no `spec_symbol` row at all
        - `specs_low_confidence`: Specs whose avg(spec_symbol.confidence) < 0.7
          (typically means only verb-anchored matches, no `@spec:` annotation)
        - `spec_test_coverage` (v0.8 P2 fix #9): Specs that have ≥1 `relation='tests'`
          link, with the count. Use this to spot Specs implemented but not
          tested (Spec in this list with low test_count → coverage gap).
          Counted as `counts.specs_with_linked_tests`.
        - `spec_coverage` (v0.15): per-Spec AUTO-DERIVED test coverage from the
          call graph. Each entry is `{spec_id, title, test_coverage_ratio,
          tested_symbols, total_symbols, coverage_source}`. A Spec's
          `implements` symbol counts as tested when a test symbol reaches it
          within 3 call-graph hops (derived), carries an explicit
          `relation='tests'` link (explicit), or overlaps a Jest/Vitest
          report-covered line (report); `coverage_source` records the
          contributing signals. No hand-linking required —
          this is the differentiator over the explicit-only `spec_test_coverage`
          above. Rollups `avg_test_coverage` and
          `specs_with_derived_test_coverage` (Specs with ratio>0) are also
          in `counts`.

        The two test-coverage counts measure DIFFERENT mechanisms and can
        legitimately disagree — they are not two views of one number:
        `specs_with_linked_tests` counts EXPLICIT `relation='tests'` links;
        `specs_with_derived_test_coverage` counts Specs whose derived ratio
        is > 0.

        Pagination: `limit` caps each list. Pass ``cursors`` (keys matching
        list names → int offset) to page lists independently — preferred.
        Legacy ``cursor`` applies the same offset to every list when
        ``cursors`` is omitted. ``summary_only=True`` returns counts plus a
        sample of up to 10 ``modules_truly_orphan`` paths.
        """ + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)

        # v0.7 B3: pagination over the shared compute helper. v0.20 M19: only
        # record a trend snapshot on the primary (first-page, full) fetch —
        # not on summary_only or cursor pages.
        primary = cursor == 0 and not cursors and not summary_only
        cov = compute_coverage(st, record=primary)
        counts = cov["counts"]
        modules_no_spec = cov["modules_without_spec"]
        modules_implicit = cov["modules_implicitly_covered"]
        modules_truly_orphan = cov["modules_truly_orphan"]
        modules_unsupported_language = cov["modules_unsupported_language"]
        modules_non_product = cov["modules_non_product"]
        specs_no_impl = cov["specs_without_implementation"]
        specs_low_conf = cov["specs_low_confidence"]
        spec_test_coverage = cov["spec_test_coverage"]
        spec_coverage = cov["spec_coverage"]
        if summary_only:
            out: dict[str, Any] = {
                "counts": counts,
                "modules_truly_orphan_sample": modules_truly_orphan[:10],
            }
            if cov.get("snapshot_warning"):
                out["warning"] = cov["snapshot_warning"]
            return out

        cur_map = cursors or {}

        def _page(items: list, key: str) -> tuple[list, int | None]:
            c = int(cur_map.get(key, cursor))
            sliced = items[c : c + limit]
            nxt = c + limit if c + limit < len(items) else None
            return sliced, nxt

        mw_p, mw_next = _page(modules_no_spec, "modules_without_spec")
        mi_p, mi_next = _page(modules_implicit, "modules_implicitly_covered")
        mt_p, mt_next = _page(modules_truly_orphan, "modules_truly_orphan")
        mu_p, mu_next = _page(modules_unsupported_language, "modules_unsupported_language")
        mn_p, mn_next = _page(modules_non_product, "modules_non_product")
        specn_p, specn_next = _page(specs_no_impl, "specs_without_implementation")
        specl_p, specl_next = _page(specs_low_conf, "specs_low_confidence")
        spectc_p, spectc_next = _page(spec_test_coverage, "spec_test_coverage")
        speccov_p, speccov_next = _page(spec_coverage, "spec_coverage")
        payload: dict[str, Any] = {
            "counts": counts,
            "modules_without_spec": mw_p,
            "modules_implicitly_covered": mi_p,
            "modules_truly_orphan": mt_p,
            "modules_unsupported_language": mu_p,
            "modules_non_product": mn_p,
            "specs_without_implementation": specn_p,
            "specs_low_confidence": specl_p,
            "spec_test_coverage": spectc_p,
            "spec_coverage": speccov_p,
            "avg_test_coverage": cov["avg_test_coverage"],
            "specs_with_derived_test_coverage": cov["specs_with_derived_test_coverage"],
            "next_cursor": {
                "modules_without_spec": mw_next,
                "modules_implicitly_covered": mi_next,
                "modules_truly_orphan": mt_next,
                "modules_unsupported_language": mu_next,
                "modules_non_product": mn_next,
                "specs_without_implementation": specn_next,
                "specs_low_confidence": specl_next,
                "spec_test_coverage": spectc_next,
                "spec_coverage": speccov_next,
            },
        }
        if cov.get("snapshot_warning"):
            payload["warning"] = cov["snapshot_warning"]
        return payload

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def find_orphan_tests(
        max_depth: int = 10,
        min_weight: float = 0.0,
        include_harness: bool = False,
        include_fixtures: bool = False,
        corroborate_with: str | None = None,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Test functions whose descendant cone never reaches production code.

        Heuristic: any function/method in a test path whose forward call graph
        contains zero non-test symbols. Either disconnected fixtures, helpers
        used only by other tests, or actually orphaned tests.

        - ``min_weight`` (try 0.6): skip ambiguous resolver edges in the cone.
        - ``include_fixtures=False`` (default): skip conftest/fixtures/helpers.
        - ``include_harness=False`` (default): skip tests whose file imports an
          in-process harness (FastMCP Client, TestClient, supertest, …) —
          those are systematic false positives for the static graph.
        Each row carries ``confidence`` (0-1) and ``reasons``.

        v0.7 (B3): paginated. Payload always includes ``caveat``.

        Unreleased: ``corroborate_with=<path to a Graphify graph.json>`` drops
        candidates that a second extractor sees reaching production code. The
        caveat above names the exact failure this addresses — a test that
        exercises production through an indirection this call graph can't
        follow. A different extractor often can.
        """ + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        pid = st.project_id
        view = load_graph(st.conn, pid)
        _ORPHAN_CAVEAT = (
            "Count may include false positives: tests that exercise "
            "production code through an indirection the static call graph "
            "can't follow (e.g. an in-process MCP/RPC harness like FastMCP "
            "Client(mcp) that dispatches handlers by string name) reach zero "
            "production symbols in the cone and are reported as orphan. "
            "Treat this as an upper bound. Default filters exclude harness "
            "files and fixture-only paths — pass include_harness=True / "
            "include_fixtures=True to see them."
        )

        is_test_path = _is_test_file_path
        ws = st.settings.workspace

        # Count test files even when Jest/vitest only leave `kind=module`
        # (anonymous `test("…", () => {})` callbacks are not function symbols).
        all_file_paths = [
            r["path"]
            for r in st.conn.execute(
                "SELECT path FROM file WHERE project_id=?", (pid,)
            )
        ]
        test_file_paths = [p for p in all_file_paths if is_test_path(p)]
        test_files_count = len(test_file_paths)

        test_rows = st.conn.execute(
            """SELECT s.id, s.qualified_name, s.kind, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.kind IN ('function', 'method')
               ORDER BY f.path, s.start_line, s.qualified_name""",
            (pid,),
        ).fetchall()
        test_syms = [dict(r) for r in test_rows if is_test_path(r["file_path"])]

        # A test file the extractor saw no function/method in is invisible to
        # the orphan scan (Jest/vitest write anonymous `it()` callbacks). Track
        # it per file: a project-wide "no test symbols at all" check goes quiet
        # as soon as one other language contributes a named test.
        files_with_test_syms = {r["file_path"] for r in test_syms}
        blind_test_files = [
            p
            for p in test_file_paths
            if p not in files_with_test_syms
            and (include_fixtures or not _is_fixture_only_path(p))
            # A package marker holds no tests by design, so reporting it as a
            # file the extractor couldn't read sends the reader after nothing.
            and Path(p).name != "__init__.py"
        ]

        harness_cache: dict[str, bool] = {}
        orphans: list[dict[str, Any]] = []
        harness_skipped = 0
        fixture_skipped = 0
        for r in test_syms:
            fp = r["file_path"]
            if not include_fixtures and _is_fixture_only_path(fp):
                fixture_skipped += 1
                continue
            if fp not in harness_cache:
                harness_cache[fp] = _file_has_harness_indirection(ws / fp)
            is_harness = harness_cache[fp]
            if is_harness and not include_harness:
                harness_skipped += 1
                continue

            sid = int(r["id"])
            descendants = (
                descendants_within(view.g, sid, max_depth, min_weight)
                if sid in view.g
                else set()
            )
            reaches_prod = False
            for did in descendants:
                meta = view.sym_meta.get(did)
                if meta and not is_test_path(meta.get("file_path", "")):
                    reaches_prod = True
                    break
            if reaches_prod:
                continue

            reasons: list[str] = []
            if not descendants:
                reasons.append("no_outgoing_calls")
                confidence = 0.85
            else:
                reasons.append("cone_stays_in_tests")
                confidence = 0.55
            if is_harness:
                reasons.append("harness_indirection")
                confidence = min(confidence, 0.3)

            orphans.append({
                "qualified_name": r["qualified_name"],
                "file_path": fp,
                "kind": r["kind"],
                "reason": reasons[0],
                "reasons": reasons,
                "confidence": confidence,
            })
        corroboration: dict[str, Any] | None = None
        graph_path, corroboration_hint = _resolve_corroboration_source(
            st, corroborate_with
        )
        if graph_path:
            orphans, corroboration = _corroborate_orphan_tests(
                orphans, st=st, graph_path=graph_path
            )
            if corroboration.get("isError"):
                return corroboration

        total = len(orphans)
        payload: dict[str, Any] = {
            "count": total,
            "caveat": _ORPHAN_CAVEAT,
            "harness_skipped_count": harness_skipped,
            "fixture_skipped_count": fixture_skipped,
            "test_files_count": test_files_count,
            "test_function_symbols": len(test_syms),
            "test_files_without_symbols": len(blind_test_files),
            "test_files_without_symbols_sample": sorted(blind_test_files)[:10],
        }
        if corroboration is not None:
            payload["corroboration"] = corroboration
        elif corroboration_hint:
            payload["corroboration_available"] = corroboration_hint
        if blind_test_files:
            payload["hint"] = (
                f"{len(blind_test_files)} of {test_files_count} indexed test "
                "files contributed no function/method symbol (common with "
                "Jest/vitest anonymous `test()`/`it()` callbacks). Those files "
                "were not scanned for orphans, so this count reflects "
                "extractor coverage, not proof their suites have none."
            )
        if summary_only:
            return payload
        page = orphans[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < total else None
        payload["orphan_tests"] = page
        payload["next_cursor"] = next_cursor
        return payload

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
                "affected_specs": [],
                "suggested_tests": [],
                "counts": {"impacted_callers": 0, "changed_symbols": 0},
            }
            if summary_only:
                empty["changed_files_sample"] = []
                empty["changed_files_indexed_sample"] = []
                empty["changed_files_unindexed_sample"] = []
                return empty
            empty["changed_files"] = []
            empty["changed_files_indexed"] = []
            empty["changed_files_unindexed"] = []
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
            for r in _select_in_chunks(
                st.conn,
                """SELECT DISTINCT r.spec_id, r.title, r.status, r.priority
                    FROM spec_symbol rs JOIN spec r ON r.id = rs.spec_id
                    WHERE rs.symbol_id IN ({in})""",
                all_touched,
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
            for r in _select_in_chunks(
                st.conn,
                """SELECT DISTINCT f.path FROM symbol_edge e
                    JOIN symbol s ON s.id = e.src_symbol_id
                    JOIN file f ON f.id = s.file_id
                    WHERE f.project_id=? AND e.dst_symbol_id IN ({in})""",
                all_touched,
                prefix_params=(pid,),
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
        unindexed_paths = sorted(set(changed_paths) - indexed_paths)
        base: dict[str, Any] = {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "affected_specs": affected_specs,
            "suggested_tests": suggested_tests,
            "counts": counts,
        }
        if unindexed_paths and not changed_symbol_meta:
            base["hint"] = (
                "Diff touches files with no indexed symbols "
                f"({len(unindexed_paths)} unindexed). Non-code paths "
                "(.dockerignore, *.properties, markdown, …) never yield "
                "symbols. For source files: run `index_project` then retry."
            )
        if summary_only:
            # v0.16 fix: honor summary_only for real — the full path lists
            # (changed_files / _indexed / _unindexed) were previously
            # returned in full here regardless of the flag, defeating its
            # purpose (a 133-file diff repeated the same list 3x). A small
            # bounded sample is enough to sanity-check which files matched.
            _SAMPLE = 20
            base["changed_files_sample"] = changed_paths[:_SAMPLE]
            base["changed_files_indexed_sample"] = sorted(indexed_paths)[:_SAMPLE]
            base["changed_files_unindexed_sample"] = unindexed_paths[:_SAMPLE]
            return base
        base["changed_files"] = changed_paths
        base["changed_files_indexed"] = sorted(indexed_paths)
        base["changed_files_unindexed"] = unindexed_paths
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
        per_file_limit: int = 20,
        fts_prefilter: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Search only indexed files (SQLite ``file`` table) on disk.

        ``pattern`` is tried as a regex first; invalid, overlong, or
        ReDoS-prone patterns fall back to literal substring match — see
        ``match_mode`` in the response. ``path_glob`` uses shell-style globs
        on the indexed relative path (e.g. ``src/**/*.py``). ``kind`` filters
        the indexed ``language`` column. ``per_file_limit`` caps hits per
        file before the global ``limit``. ``fts_prefilter=True`` narrows to
        files that hit FTS5 first (faster on large repos; falls back to full
        scan if FTS finds nothing).

        Only files in the index are searched, so a stale index can make a real
        match invisible. Every response therefore carries ``scope_fresh``:
        ``True`` means the files this call covered are byte-identical to what
        was indexed AND no unindexed file falls in scope — an empty ``matches``
        really means "no matches". ``False`` adds ``stale_files`` /
        ``unindexed_files`` (+ ``_count``) and a ``hint``.
        """ + WORKSPACE_DOCSTRING_NOTE
        st = get_state(workspace)
        return _grep_indexed_files_core(
            st,
            pattern,
            path_glob,
            kind,
            limit,
            cursor,
            per_file_limit=per_file_limit,
            fts_prefilter=fts_prefilter,
        )

