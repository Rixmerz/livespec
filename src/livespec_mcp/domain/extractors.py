"""Symbol and reference extractors per language.

Each extractor returns:
  symbols: list[ExtractedSymbol]
  refs:    list[ExtractedRef]   (raw call / reference names; resolved later)
"""

from __future__ import annotations

import ast
import re as _re_rs
from dataclasses import dataclass, field
from pathlib import Path

from livespec_mcp.domain.languages import detect_language, get_parser


@dataclass
class ExtractedSymbol:
    name: str
    qualified_name: str
    kind: str
    signature: str | None
    docstring: str | None
    body_hash_seed: str
    start_line: int
    end_line: int
    parent_qname: str | None = None
    decorators: list[str] = field(default_factory=list)  # v0.5 P1: ordered, dotted form
    visibility: str | None = None  # v0.7 B4: pub / pub(crate) / private / exported / ...


@dataclass
class ExtractedRef:
    src_qname: str
    target_name: str  # last name of the call (e.g. "foo" or "Cls.method")
    line: int
    ref_type: str = "call"
    scope_module: str | None = None  # P0.4: module name imported as the source of `target_name`


@dataclass
class ExtractedRoute:
    """v0.21 P2: one side of a cross-repo route edge.

    role='server' — an HTTP handler (`@app.get('/x')`); role='client' — a call
    site that hits a route (`fetch('/x')`, `requests.get('/x')`). The resolver
    joins client↔server by normalized path.
    """
    src_qname: str
    role: str          # 'client' | 'server'
    method: str | None
    path: str
    line: int


@dataclass
class ExtractResult:
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    refs: list[ExtractedRef] = field(default_factory=list)
    routes: list[ExtractedRoute] = field(default_factory=list)
    # P0.4: per-file imports map. local_name -> source_module (qualified name of
    # the module providing it). Used by the resolver to scope short-name lookups.
    imports: dict[str, str] = field(default_factory=dict)
    # True when the source could not be parsed (Python ast.parse SyntaxError).
    # The indexer uses this to PRESERVE the file's existing symbols instead of
    # wiping them (and their spec_symbol links) on a transient parse failure —
    # e.g. a file saved mid-edit. tree-sitter is error-recovering, so this is
    # effectively Python-only.
    parse_error: bool = False


# Compound statements whose bodies can hold conditionally-defined symbols.
# `visit()` descends into these without changing scope so a def under
# `if TYPE_CHECKING:` / a try-import shim / a version guard is still extracted.
_COMPOUND_STMTS: tuple[type, ...] = (
    ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While,
    ast.ExceptHandler,  # `except ...: def fallback()` import shims
)
if hasattr(ast, "Match"):  # py3.10+ — descend Match and its case bodies
    _COMPOUND_STMTS = (*_COMPOUND_STMTS, ast.Match, ast.match_case)
if hasattr(ast, "TryStar"):  # py3.11+
    _COMPOUND_STMTS = (*_COMPOUND_STMTS, ast.TryStar)


# ---------- Python via ast ----------


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = []
    a = node.args
    posonly = list(getattr(a, "posonlyargs", []))
    regular = list(a.args)
    for arg in posonly + regular:
        args.append(arg.arg)
    if a.vararg:
        args.append("*" + a.vararg.arg)
    for arg in a.kwonlyargs:
        args.append(arg.arg)
    if a.kwarg:
        args.append("**" + a.kwarg.arg)
    name = node.name
    return f"{name}({', '.join(args)})"


def _py_extract(source: str, module_name: str) -> ExtractResult:
    out = ExtractResult()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        out.parse_error = True
        return out

    # P0.4: collect imports for resolver scoping. Maps `local_name` -> source module.
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.imports[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "*":
                    continue
                out.imports[local] = mod

    def add_func(node: ast.FunctionDef | ast.AsyncFunctionDef, parent_qname: str | None, kind: str) -> str:
        qname = f"{parent_qname}.{node.name}" if parent_qname else f"{module_name}.{node.name}"
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        doc = ast.get_docstring(node)
        sig = _py_signature(node)
        seed = f"{sig}|{ast.dump(node, annotate_fields=False, include_attributes=False)}"
        out.symbols.append(
            ExtractedSymbol(
                name=node.name,
                qualified_name=qname,
                kind=kind,
                signature=sig,
                docstring=doc,
                body_hash_seed=seed,
                start_line=start,
                end_line=end,
                parent_qname=parent_qname,
                decorators=_py_decorator_names(node),
            )
        )
        # v0.21 P2: server-side route — record every HTTP handler decorator so
        # the resolver can join it to a frontend call site by path.
        for dec in getattr(node, "decorator_list", []) or []:
            method, path = _http_route_from_decorator_call(dec)
            if path is not None:
                out.routes.append(
                    ExtractedRoute(
                        src_qname=qname,
                        role="server",
                        method=method,
                        path=path,
                        line=start,
                    )
                )
        _collect_calls(node, qname, out)
        return qname

    def add_class(node: ast.ClassDef, parent_qname: str | None) -> str:
        qname = f"{parent_qname}.{node.name}" if parent_qname else f"{module_name}.{node.name}"
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        doc = ast.get_docstring(node)
        bases = [ast.unparse(b) if hasattr(ast, "unparse") else "" for b in node.bases]
        sig = f"class {node.name}({', '.join(bases)})"
        seed = f"{sig}|{ast.dump(node, annotate_fields=False, include_attributes=False)}"
        out.symbols.append(
            ExtractedSymbol(
                name=node.name,
                qualified_name=qname,
                kind="class",
                signature=sig,
                docstring=doc,
                body_hash_seed=seed,
                start_line=start,
                end_line=end,
                parent_qname=parent_qname,
                decorators=_py_decorator_names(node),
            )
        )
        return qname

    def visit(node: ast.AST, parent_qname: str | None, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "function"
                qn = add_func(child, parent_qname, kind)
                visit(child, qn, in_class=False)
            elif isinstance(child, ast.ClassDef):
                qn = add_class(child, parent_qname)
                visit(child, qn, in_class=True)
            elif isinstance(child, _COMPOUND_STMTS):
                # Descend into if/try/with/for/while/match bodies WITHOUT
                # changing scope, so conditionally defined functions/classes
                # (``if TYPE_CHECKING:``, ``try: import X ... except: def X``,
                # version-guarded ``if sys.version_info...`` shims) are still
                # extracted. Without this they were invisible — missing symbols
                # and false dead-code positives on their callers.
                visit(child, parent_qname, in_class)

    visit(tree, None, in_class=False)
    return out


def _collect_calls(func_node: ast.AST, src_qname: str, out: ExtractResult) -> None:
    # Walk this symbol's body but STOP at nested def/class boundaries — their
    # call sites belong to the nested symbol (extracted separately), not to
    # every enclosing def. ast.walk() would descend into them and attribute an
    # inner call to each ancestor, inflating the edge table and making
    # who_calls report a class/outer that never issues the call.
    stack: list[ast.AST] = list(ast.iter_child_nodes(func_node))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Call):
            _emit_py_callback_refs(node, src_qname, out)
            client = _py_client_route(node)
            if client is not None:
                method, path = client
                out.routes.append(
                    ExtractedRoute(
                        src_qname=src_qname,
                        role="client",
                        method=method,
                        path=path,
                        line=getattr(node, "lineno", 0),
                    )
                )
            target = _call_target_name(node.func)
            if target:
                # P0.4: if the target name was imported in this file, tag the
                # ref with the originating module so the resolver can scope.
                scope = out.imports.get(target)
                # For attribute access `mod.func()`, also try the leftmost name
                # against imports (e.g. `from pkg import mod; mod.func()`).
                if scope is None and isinstance(node.func, ast.Attribute):
                    leftmost = _leftmost_name(node.func)
                    if leftmost is not None and leftmost in out.imports:
                        # The function lives inside the imported module
                        scope = out.imports[leftmost]
                out.refs.append(
                    ExtractedRef(
                        src_qname=src_qname,
                        target_name=target,
                        line=getattr(node, "lineno", 0),
                        scope_module=scope,
                    )
                )


# v0.21: Python callback-argument refs. A function passed as an argument is
# invoked later by the callee (`atexit.register(cleanup)`, `Thread(target=fn)`,
# `Watcher(on_reindex=cb)`, `sorted(xs, key=fn)`) — without a ref it looks dead.
# Mirror the TS `callback_arg` handling, but conservatively scoped so we don't
# emit a ref for every bare-Name data argument: only when the CALLEE is a known
# registration/scheduling call, or the KEYWORD name signals a callback.
_PY_CALLBACK_REG_NAMES = frozenset({
    "register", "connect", "subscribe", "signal", "add_callback",
    "add_done_callback", "add_signal_handler", "call_soon", "call_later",
    "submit", "apply_async", "add_handler", "addhandler",
})
_PY_CALLBACK_KW_NAMES = frozenset({
    "target", "key", "callback", "hook", "fn", "func", "handler",
    "default_factory", "on_reindex",
})

def _emit_py_callback_refs(node: ast.Call, src_qname: str, out: ExtractResult) -> None:
    """Emit ``callback_arg`` refs for a function passed as an argument.

    Conservative (mirrors the TS side): a bare-``Name`` argument only counts as
    a callback when the callee is a known registration/scheduling call
    (``atexit.register(fn)``, ``executor.submit(task)``) OR it is a keyword whose
    name signals a callback (``target=``, ``key=``, ``on_reindex=``, ``on_*``).
    The resolver's name index self-limits these to actually-defined symbols, and
    ambiguous multi-candidate matches land at the default-filtered weight 0.5.
    """
    callee = _call_target_name(node.func)
    callee_is_reg = callee is not None and callee.lower() in _PY_CALLBACK_REG_NAMES

    def _emit(name_node: ast.AST) -> None:
        if isinstance(name_node, ast.Name):
            out.refs.append(
                ExtractedRef(
                    src_qname=src_qname,
                    target_name=name_node.id,
                    line=getattr(name_node, "lineno", getattr(node, "lineno", 0)),
                    ref_type="callback_arg",
                    scope_module=out.imports.get(name_node.id),
                )
            )

    if callee_is_reg:
        for a in node.args:
            _emit(a)
    for kw in node.keywords:
        if kw.arg is None:  # **kwargs splat
            continue
        if callee_is_reg or kw.arg in _PY_CALLBACK_KW_NAMES or kw.arg.startswith("on_"):
            _emit(kw.value)


_HTTP_CLIENT_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
# TS/JS: object identifiers that denote an HTTP client, so `axios.get('/x')` is
# a client call but Hono's `app.get('/x', handler)` (object `app`) is not. The
# allowlist is a first filter only — the definitive server-vs-client
# discriminator is a trailing handler arg (see `_TS_HANDLER_ARG_TYPES`), since
# router variables are commonly named `api`/`client`/`request` too.
_TS_HTTP_CLIENT_OBJS = frozenset({
    "axios", "api", "http", "https", "client", "request", "httpclient", "$http", "fetch",
})
# tree-sitter node types for a route-handler argument: a function/arrow, or a
# bare identifier naming a handler. Their presence after the URL marks a SERVER
# route registration (`api.get('/x', handler)`), never a client call.
_TS_HANDLER_ARG_TYPES = frozenset({
    "arrow_function", "function_expression", "function", "generator_function",
    "identifier",
})


def _py_client_route(call: ast.Call) -> tuple[str | None, str] | None:
    """Detect a Python HTTP client call — ``requests.get('/x')`` /
    ``httpx.post('/x')`` / ``session.get('/x')`` — returning (method, path).

    Conservative: the attribute must be an HTTP verb AND the first positional
    arg a string literal that looks like a path/URL (``/...`` or ``http...``),
    so ``some_dict.get('key')`` never registers as a route."""
    fn = call.func
    if not isinstance(fn, ast.Attribute) or fn.attr.lower() not in _HTTP_CLIENT_VERBS:
        return None
    if not call.args:
        return None
    path = _ast_str_constant(call.args[0])
    if not path or not (path.startswith("/") or path.startswith("http")):
        return None
    return fn.attr.upper(), path


def _leftmost_name(node: ast.AST) -> str | None:
    """For `a.b.c`, return `a`."""
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _call_target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _py_decorator_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> list[str]:
    """Return ordered dotted names for each decorator on a Python def/class.

    Captures both bare (`@route`) and called (`@app.route("/")`) forms, plus
    deeply attribute-accessed (`@router.api.v1.get`). Falls back to
    `ast.unparse(...)` for exotic decorators (lambdas, subscripts) so the
    field is never empty for an actually-decorated symbol."""
    out: list[str] = []
    for d in getattr(node, "decorator_list", []) or []:
        target = d.func if isinstance(d, ast.Call) else d
        name = _decorator_dotted(target)
        if name is None:
            try:
                name = ast.unparse(d)
            except Exception:
                name = None
        if name:
            out.append(name)
    return out


def _decorator_dotted(node: ast.AST) -> str | None:
    """Return `a.b.c` for nested ast.Attribute on ast.Name; else None."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


# Flask / FastAPI / Starlette HTTP route decorators (last dotted segment).
_HTTP_VERB_DECORATOR_LASTSEGS = frozenset({
    "get", "post", "put", "delete", "patch", "head", "options",
})
HTTP_ROUTE_DECORATOR_LASTSEGS = _HTTP_VERB_DECORATOR_LASTSEGS | frozenset({
    "route", "api_route", "websocket",
})

# v0.21 P2: route path normalization — the join key between a frontend call
# site and a backend handler. Every framework's path-param syntax collapses to
# a single `{}` placeholder so `/users/<int:id>` (Flask), `/users/{id}`
# (FastAPI/Hono) and `/users/:id` (Express/React-router) all match, and a
# concrete client call `/users/123` matches the template too.
_ROUTE_PARAM_PATTERNS = (
    _re_rs.compile(r"<[^>]+>"),                    # Flask <int:id>, <id>
    _re_rs.compile(r"\{[^}]+\}"),                  # FastAPI / Hono {id}
    _re_rs.compile(r":[A-Za-z_][A-Za-z0-9_]*"),    # Express / React-router :id
    _re_rs.compile(r"\*+"),                        # wildcards
)
_URL_SCHEME_RE = _re_rs.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+(/.*)?$")


def normalize_route_path(raw: str | None) -> str:
    """Canonicalize a route/URL string into a match key.

    Strips scheme+host and query/fragment, collapses every path parameter (and
    bare numeric segments) to ``{}``, dedups slashes, and enforces a leading
    slash with no trailing slash (root ``/`` excepted). Returns ``""`` for an
    empty/None input so the resolver can skip it.
    """
    if not raw:
        return ""
    s = raw.strip()
    m = _URL_SCHEME_RE.match(s)
    if m:
        s = m.group(1) or "/"
    s = s.split("?", 1)[0].split("#", 1)[0]
    for pat in _ROUTE_PARAM_PATTERNS:
        s = pat.sub("{}", s)
    norm_parts = [
        "{}" if (seg and seg != "{}" and seg.isdigit()) else seg
        for seg in s.split("/")
    ]
    s = "/".join(norm_parts)
    s = _re_rs.sub(r"/{2,}", "/", s)
    if not s.startswith("/"):
        s = "/" + s
    if len(s) > 1 and s.endswith("/"):
        s = s.rstrip("/")
    return s


def _ast_str_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ast_http_methods(node: ast.AST) -> list[str]:
    """Extract HTTP method strings from ``methods=[...]`` / ``methods=(...)``."""
    if isinstance(node, (ast.List, ast.Tuple)):
        out: list[str] = []
        for elt in node.elts:
            s = _ast_str_constant(elt)
            if s:
                out.append(s.upper())
        return out
    s = _ast_str_constant(node)
    return [s.upper()] if s else []


def _http_route_from_decorator_call(dec: ast.AST) -> tuple[str | None, str | None]:
    """Parse one ``@app.get('/x')`` / ``@app.route('/x', methods=[...])`` call."""
    if not isinstance(dec, ast.Call):
        return None, None
    name = _decorator_dotted(dec.func)
    if not name:
        return None, None
    last = name.rsplit(".", 1)[-1].lower()
    if last not in HTTP_ROUTE_DECORATOR_LASTSEGS:
        return None, None
    path = _ast_str_constant(dec.args[0]) if dec.args else None
    method: str | None
    if last in _HTTP_VERB_DECORATOR_LASTSEGS:
        method = last.upper()
    elif last == "websocket":
        method = "WEBSOCKET"
    else:
        method = None
        for kw in dec.keywords:
            if kw.arg == "methods" and kw.value is not None:
                methods = _ast_http_methods(kw.value)
                if methods:
                    method = methods[0]
                    break
        if method is None:
            method = "GET"
    return method, path


def parse_python_http_route(source: str, start_line: int) -> dict[str, str | None]:
    """Return ``http_method`` / ``http_path`` for a Python handler at ``start_line``.

    Reads Flask/FastAPI/Starlette-style decorator calls on the function or
    async function whose ``lineno`` equals ``start_line``. When several HTTP
    decorators are stacked, the first with a string path wins.
    """
    empty: dict[str, str | None] = {"http_method": None, "http_path": None}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return empty
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno != start_line:
            continue
        for dec in node.decorator_list:
            method, path = _http_route_from_decorator_call(dec)
            if path is not None:
                return {"http_method": method, "http_path": path}
        return empty
    return empty


def infer_python_http_framework(source: str) -> str:
    """Guess flask vs fastapi from bounded import scan of module source."""
    head = source[:8192].lower()
    if "fastapi" in head or "starlette" in head:
        return "fastapi"
    if "flask" in head:
        return "flask"
    return "fastapi"


# ---------- Generic tree-sitter ----------


def _extract_visibility(node, src_bytes: bytes, language: str) -> str | None:
    """Best-effort visibility extraction for tree-sitter languages.

    Returns one of:
      - 'pub', 'pub(crate)', 'pub(super)' (Rust)
      - 'public', 'private', 'protected' (Java/PHP)
      - 'exported' (TS/JS, presence of `export` keyword)
      - 'private' (Rust default for items WITHOUT a visibility_modifier)
      - None (language doesn't have a known model — Go, etc.)

    Rust convention: capitalized identifiers in Go are 'public'-equivalent
    but Go's grammar doesn't tag them so we leave them unmarked.
    """
    if language == "rust":
        for c in node.children:
            if c.type == "visibility_modifier":
                txt = src_bytes[c.start_byte : c.end_byte].decode("utf-8", errors="replace")
                # Strip whitespace; preserve `pub`, `pub(crate)`, `pub(super)`, `pub(in path)`.
                return txt.strip()
        return "private"
    if language in ("javascript", "typescript", "tsx"):
        # Walk siblings — `export` typically wraps the declaration in an
        # export_statement node, so check the parent.
        parent = node.parent if hasattr(node, "parent") else None
        if parent is not None and parent.type in (
            "export_statement", "export_default_declaration"
        ):
            return "exported"
        return None
    if language == "java":
        for c in node.children:
            if c.type == "modifiers":
                txt = src_bytes[c.start_byte : c.end_byte].decode("utf-8", errors="replace")
                if "public" in txt:
                    return "public"
                if "protected" in txt:
                    return "protected"
                if "private" in txt:
                    return "private"
        return None
    if language == "php":
        for c in node.children:
            if c.type in ("visibility_modifier", "modifiers"):
                txt = src_bytes[c.start_byte : c.end_byte].decode("utf-8", errors="replace").lower()
                for vis in ("public", "private", "protected"):
                    if vis in txt:
                        return vis
        return None
    return None


def _normalize_ts_body(node, src_bytes: bytes) -> str:
    """Reformat-stable body seed for tree-sitter languages.

    Walks the syntax tree and emits only LEAF tokens joined by single spaces.
    This makes the seed invariant under:
    - any whitespace change (indent, blank lines, spacing around punctuation)
    - comment add/remove (comment nodes are skipped by type prefix)
    A real semantic change (literal, identifier, operator) still alters the
    leaf token stream and produces a different hash.

    Falls back to the raw source slice if the walk yields nothing (defensive).
    """
    parts: list[str] = []

    def visit(n) -> None:
        # Skip comment-like nodes regardless of grammar
        ntype = n.type
        if ntype == "comment" or "comment" in ntype:
            return
        if not n.children:
            raw = src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")
            # Whitespace INSIDE a string/char/template literal is semantic —
            # `"pad "` vs `"pad"` must drift the hash — so only strip tokens
            # that are not string content.
            parent_type = n.parent.type if n.parent is not None else ""
            in_literal = (
                "string" in ntype
                or "string" in parent_type
                or "template" in parent_type
                or "char" in parent_type
            )
            txt = raw if in_literal else raw.strip()
            if txt:
                parts.append(txt)
            return
        for c in n.children:
            visit(c)

    visit(node)
    if not parts:
        return src_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
    return " ".join(parts)

# Node-type heuristics: covers most C-family + Go.
_DEF_NODE_TYPES = {
    "function_declaration",
    "method_declaration",
    "function_definition",
    "method_definition",
    "class_declaration",
    "class_definition",
    "interface_declaration",
    # Rust
    "function_item",     # plain Rust functions
    "impl_item",         # walked specially to qualify methods as Type::method
    "trait_item",        # trait definitions and their default methods
    "struct_item",       # treated as classes
    "enum_item",
    # Go: structs/interfaces declared as type_spec inside type_declaration
    "type_spec",
    # Ruby
    "method",            # def foo
    "singleton_method",  # def self.foo
    "class",             # class Foo
    "module",            # module Foo
}

_CALL_NODE_TYPES = {
    "call_expression",
    "function_call",
    "method_invocation",
    "invocation_expression",
    "call",
    # PHP-specific
    "function_call_expression",
    "method_call_expression",
    "scoped_call_expression",
    "member_call_expression",
    # Ruby is just `call` (already covered)
}

# Anonymous function literals — name comes from the surrounding binding
_ANONYMOUS_FN_TYPES = {
    "arrow_function",       # JS/TS: const f = () => {}
    "function_expression",  # JS/TS: const f = function () {}
}


def _ts_node_decorators(node, src_bytes: bytes, language: str) -> list[str]:
    """Decorator / annotation names for a tree-sitter declaration node.

    v0.13 P1: fills `ExtractedSymbol.decorators` for non-Python languages
    so framework detection (Angular `@Component`, Spring `@RestController` /
    `@GetMapping`) works through the same `decorators` column the Python
    extractor populates.

    TS/JS/TSX: `decorator` children live on the declaration node itself or
    on a wrapping `export_statement` (`@Component() export class Foo`).
    Java: annotations live inside the `modifiers` child of the declaration
    (`marker_annotation` = `@Override`, `annotation` = `@GetMapping("/x")`).

    Returned in source order, dotted form without `@`, call arguments
    stripped: `@Component({...})` -> `Component`, `@app.get('/x')` ->
    `app.get`, `@org.junit.Test` -> `org.junit.Test`.
    """

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    out: list[str] = []
    if language in ("javascript", "typescript", "tsx"):
        dec_nodes = [c for c in node.children if c.type == "decorator"]
        # Method/field decorators are PRECEDING SIBLINGS of the member node
        # inside class_body, not children — walk the contiguous run.
        preceding = []
        prev = node.prev_sibling
        while prev is not None and prev.type == "decorator":
            preceding.append(prev)
            prev = prev.prev_sibling
        dec_nodes = list(reversed(preceding)) + dec_nodes
        parent = getattr(node, "parent", None)
        if parent is not None and parent.type == "export_statement":
            dec_nodes = [c for c in parent.children if c.type == "decorator"] + dec_nodes
        for d in dec_nodes:
            target = None
            for c in d.children:  # first non-'@' payload child
                if c.type in ("identifier", "member_expression", "call_expression"):
                    target = c
                    break
            if target is None:
                continue
            if target.type == "call_expression":
                fn = target.child_by_field_name("function")
                if fn is not None:
                    out.append(text(fn))
            else:
                out.append(text(target))
    elif language == "java":
        for c in node.children:
            if c.type != "modifiers":
                continue
            for m in c.children:
                if m.type in ("annotation", "marker_annotation"):
                    name = m.child_by_field_name("name")
                    if name is not None:
                        out.append(text(name))
    return out


def _ts_leading_doc_comment(node, src_bytes: bytes, language: str) -> str | None:
    """Return the JSDoc / leading doc-comment text immediately preceding `node`.

    Tree-sitter exposes comments as sibling nodes of declarations. For
    `export ...` and `export default ...`, we walk up so the comment is
    found relative to the wrapping export statement (where it actually
    sits). Supports JS/TS JSDoc (`/** ... */`), Rust outer-doc lines
    (`/// ...`), and runs of `//` line comments. The returned text is
    raw (delimiters stripped, leading `*` / `/` markers trimmed) so the
    `@spec:` annotation matcher can scan it as if it were a Python
    docstring.
    """
    # Walk up through wrapping export statements so we can see the
    # comment that sits above `export function foo()`.
    cur = node
    parent = getattr(cur, "parent", None)
    while parent is not None and parent.type in (
        "export_statement",
        "export_default_declaration",
    ):
        cur = parent
        parent = getattr(cur, "parent", None)
    if parent is None:
        return None

    # Find the immediate previous sibling by index.
    prev_idx = -1
    for i, c in enumerate(parent.children):
        if c.start_byte == cur.start_byte and c.end_byte == cur.end_byte:
            prev_idx = i - 1
            break
    if prev_idx < 0:
        return None

    # Collect a contiguous run of comment siblings (handles `//` blocks
    # and `///` Rust doc comments). Each comment is stripped according
    # to its own delimiter style BEFORE joining so a `/** ... */` block
    # adjacent to `// ...` line comments still has its `@spec:` tag at
    # column 0 of its joined line — otherwise the leading `/**` of the
    # block leaks into the joined text and defeats the matcher's
    # line-start anchor.
    cleaned: list[str] = []
    idx = prev_idx
    while idx >= 0:
        sib = parent.children[idx]
        if "comment" not in sib.type:
            break
        raw = src_bytes[sib.start_byte : sib.end_byte].decode("utf-8", errors="replace")
        piece = _strip_doc_comment(raw).strip()
        # Skip pure ASCII separator lines (`// ---`, `// ===`, `// ***`)
        # so they don't end up as `docstring_lead`. They carry no signal
        # and only crowd out the JSDoc block sitting underneath.
        if piece and not _is_separator_only(piece):
            cleaned.append(piece)
        idx -= 1
    if not cleaned:
        return None
    cleaned.reverse()
    return "\n".join(cleaned).strip() or None


_BANNER_RE = __import__("re").compile(
    r"^[-=*#_~]{2,}.+?[-=*#_~]{2,}$"
)


def _is_separator_only(text: str) -> bool:
    """True if every non-empty line is either:

    - pure ASCII separator punctuation (e.g. `---`, `===`, `***`,
      `###`), or
    - a banner with internal text wrapped in ≥2 separator chars on each
      side (e.g. `--- Token Management ---`,
      `============= Tool Execution Dispatcher =============`).

    These show up as section headers above declarations and must not be
    treated as the declaration's docstring — they would otherwise win
    `docstring_lead` over the meaningful JSDoc that follows.
    """
    sep_chars = set("-=*#_~ \t")
    saw_line = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        saw_line = True
        if all(ch in sep_chars for ch in s):
            continue
        if _BANNER_RE.match(s):
            continue
        return False
    return saw_line


def _strip_doc_comment(raw: str) -> str:
    """Strip JSDoc/`//`/`///` syntax to leave the inner text. Conservative:
    if the comment isn't a recognised doc form, return it unchanged so
    inline `@spec:` tags inside ordinary `//` comments still match."""
    s = raw.strip()
    # JSDoc / block comment
    if s.startswith("/*"):
        if s.endswith("*/"):
            s = s[:-2]
        s = s.lstrip("/*").rstrip()
        lines = []
        for line in s.splitlines():
            stripped = line.strip()
            if stripped.startswith("*"):
                stripped = stripped[1:].lstrip()
            lines.append(stripped)
        return "\n".join(lines).strip()
    # Run of `//` or `///` line comments
    out_lines = []
    for line in s.splitlines():
        stripped = line.strip()
        if stripped.startswith("///"):
            stripped = stripped[3:].lstrip()
        elif stripped.startswith("//"):
            stripped = stripped[2:].lstrip()
        out_lines.append(stripped)
    return "\n".join(out_lines).strip()


def _ts_extract(
    source: str,
    language: str,
    module_name: str,
    current_dir: tuple[str, ...] = (),
) -> ExtractResult:
    out = ExtractResult()
    try:
        parser = get_parser(language)
    except Exception:
        return out
    src_bytes = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_bytes)

    if language in ("javascript", "typescript", "tsx"):
        out.imports.update(_ts_collect_imports(tree.root_node, src_bytes, current_dir))
    elif language == "go":
        out.imports.update(_go_collect_imports(tree.root_node, src_bytes))
    elif language == "ruby":
        out.imports.update(_rb_collect_imports(tree.root_node, src_bytes, current_dir))
    elif language == "php":
        out.imports.update(_php_collect_imports(tree.root_node, src_bytes))
    elif language == "rust":
        out.imports.update(_rs_collect_imports(tree.root_node, src_bytes))

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    def find_name(n) -> str | None:
        # Try common child field names
        for field_name in ("name", "identifier"):
            child = n.child_by_field_name(field_name) if hasattr(n, "child_by_field_name") else None
            if child is not None:
                return text(child)
        # Fallback: first identifier child
        for c in n.children:
            if c.type in ("identifier", "name", "field_identifier", "type_identifier"):
                return text(c)
        return None

    def emit_symbol(node, name: str, parent_qname: str | None, kind: str, qname_sep: str = ".") -> str:
        qname = f"{parent_qname}{qname_sep}{name}" if parent_qname else f"{module_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        signature = text(node).splitlines()[0][:300] if node.start_byte != node.end_byte else None
        docstring = _ts_leading_doc_comment(node, src_bytes, language)
        out.symbols.append(
            ExtractedSymbol(
                name=name,
                qualified_name=qname,
                kind=kind,
                signature=signature,
                docstring=docstring,
                body_hash_seed=_normalize_ts_body(node, src_bytes),
                start_line=start_line,
                end_line=end_line,
                parent_qname=parent_qname,
                visibility=_extract_visibility(node, src_bytes, language),
                decorators=_ts_node_decorators(node, src_bytes, language),
            )
        )
        return qname

    def impl_target_name(impl_node) -> str | None:
        """For Rust `impl Type` or `impl Trait for Type`, return Type."""
        # tree-sitter-rust exposes a `type` field for the implementee
        child = impl_node.child_by_field_name("type") if hasattr(impl_node, "child_by_field_name") else None
        if child is not None:
            return text(child).split("<")[0].strip()
        # Fallback: first type_identifier
        for c in impl_node.children:
            if c.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                return text(c).split("<")[0].strip()
        return None

    def walk(node, parent_qname: str | None):
        # ----- Rust impl/trait: methods become Type::method -----
        if node.type == "impl_item":
            type_name = impl_target_name(node)
            if type_name:
                impl_qname = f"{parent_qname}.{type_name}" if parent_qname else f"{module_name}.{type_name}"
                # Emit the impl as a class-like aggregator if not already present
                emit_symbol(node, type_name, parent_qname, "class")
                # Walk children with Type qname as parent and Rust :: separator
                for c in node.children:
                    walk_rust_method(c, impl_qname)
                return
        if node.type == "trait_item":
            name = find_name(node)
            if name:
                trait_qname = emit_symbol(node, name, parent_qname, "interface")
                for c in node.children:
                    walk_rust_method(c, trait_qname)
                return

        # ----- Anonymous functions assigned to a binding -----
        # `const foo = () => {}` -> variable_declarator { name: foo, value: arrow_function }
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value") if hasattr(node, "child_by_field_name") else None
            if value is not None and value.type in _ANONYMOUS_FN_TYPES:
                name = find_name(node)
                if name:
                    qname = emit_symbol(value, name, parent_qname, "function")
                    _ts_collect_calls(value, qname, src_bytes, out)
                    return  # do not double-walk
            # otherwise let normal recursion continue

        # ----- Standard def nodes -----
        if node.type in _DEF_NODE_TYPES:
            name = find_name(node)
            if name:
                if (
                    "class" in node.type
                    or "interface" in node.type
                    or node.type in ("struct_item", "enum_item", "type_spec")
                ):
                    kind = "class"
                elif "method" in node.type:
                    kind = "method"
                else:
                    kind = "function"
                qname = emit_symbol(node, name, parent_qname, kind)
                _ts_collect_calls(node, qname, src_bytes, out)
                for c in node.children:
                    walk(c, qname)
                return
        for c in node.children:
            walk(c, parent_qname)

    def walk_rust_method(node, parent_qname: str):
        """Walk a Rust impl/trait body. function_item children become methods
        with `::` separator. Recurse so nested types are also captured."""
        if node.type == "function_item":
            name = find_name(node)
            if name:
                qname = emit_symbol(node, name, parent_qname, "method", qname_sep="::")
                _ts_collect_calls(node, qname, src_bytes, out)
                return
        for c in node.children:
            walk_rust_method(c, parent_qname)

    walk(tree.root_node, None)
    return out


# v0.13 P3: method names that take a callable and invoke it later (HTTP
# routers like Hono/Express, event emitters, plugin registries). Lowercase;
# compared against the call's last dotted segment. Kept tight — `add`/`set`/
# `push` are too generic.
_TS_CALLBACK_REG_VERBS = frozenset({
    # HTTP verb routing (Hono, Express, Fastify, ...)
    "get", "post", "put", "delete", "patch", "options", "all", "route",
    # middleware / event registration
    "use", "on", "once", "subscribe", "register", "connect",
    "addeventlistener", "addlistener",
})

# HTTP verbs that map a path string to a handler (subset of the above used
# for route extraction — `use`/`on` handled separately).
_HONO_ROUTE_VERBS = frozenset({
    "get", "post", "put", "delete", "patch", "options", "all",
})


def ts_registered_callback_names(source: str, language: str) -> frozenset[str]:
    """Identifier names passed as args to registration-style calls, at ANY
    nesting level including module top-level.

    v0.13 P3: the extract-time `callback_arg` refs only cover calls inside
    symbol bodies — the canonical Hono/Express pattern registers handlers at
    module level (`app.get('/users', listUsers)`), which no symbol owns.
    `find_dead_code` unions this set the same way the Python side uses
    `_runtime_registered_names`. Parse failures return empty.
    """
    try:
        parser = get_parser(language)
    except Exception:
        return frozenset()
    src_bytes = source.encode("utf-8", errors="replace")
    try:
        tree = parser.parse(src_bytes)
    except Exception:
        return frozenset()

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    out: set[str] = set()

    def walk(node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            prop = None
            if fn is not None and fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
            if prop is not None and text(prop).lower() in _TS_CALLBACK_REG_VERBS:
                args_node = node.child_by_field_name("arguments")
                if args_node is not None:
                    for a in args_node.children:
                        if a.type == "identifier":
                            name = text(a)
                            if name:
                                out.add(name)
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return frozenset(out)


def scan_hono_routes(source: str, language: str) -> list[dict]:
    """Route registrations in a Hono-style app file.

    v0.13 P3: returns ``[{method, path, handler_name, line}]`` for
    ``app.get('/users', handler)`` / ``app.on('PURGE', '/cache', h)`` /
    ``app.route('/api', subApp)`` call patterns. ``use`` is middleware, not
    a route — skipped. The caller pre-filters files (source must mention
    'hono') and resolves handler names to symbols.
    """
    try:
        parser = get_parser(language)
    except Exception:
        return []
    src_bytes = source.encode("utf-8", errors="replace")
    try:
        tree = parser.parse(src_bytes)
    except Exception:
        return []

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    def str_arg(n) -> str | None:
        if n.type in ("string", "template_string"):
            raw = text(n)
            return raw[1:-1] if len(raw) >= 2 else raw
        return None

    routes: list[dict] = []

    def walk(node) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            prop = None
            if fn is not None and fn.type == "member_expression":
                prop = fn.child_by_field_name("property")
            if prop is not None:
                pname = text(prop).lower()
                args_node = node.child_by_field_name("arguments")
                args = [
                    a
                    for a in (args_node.children if args_node is not None else [])
                    if a.type not in ("(", ")", ",", "comment")
                ]
                handler = next(
                    (text(a) for a in reversed(args) if a.type == "identifier"),
                    None,
                )
                line = node.start_point[0] + 1
                if pname in _HONO_ROUTE_VERBS and args and str_arg(args[0]) is not None:
                    routes.append({
                        "method": pname.upper(),
                        "path": str_arg(args[0]),
                        "handler_name": handler,
                        "line": line,
                    })
                elif pname == "on" and len(args) >= 2 and str_arg(args[1]) is not None:
                    routes.append({
                        "method": (str_arg(args[0]) or "ON").upper(),
                        "path": str_arg(args[1]),
                        "handler_name": handler,
                        "line": line,
                    })
                elif pname == "route" and args and str_arg(args[0]) is not None:
                    routes.append({
                        "method": "ROUTE",
                        "path": str_arg(args[0]),
                        "handler_name": handler,
                        "line": line,
                    })
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return routes


def _ts_leftmost_ident(node, text) -> str | None:
    """Walk a member/call chain down to its base identifier for import scoping.

    `promise.then(h).catch` → object `promise.then(h)` (call) → function
    `promise.then` (member) → object `promise` (identifier) ⇒ 'promise'.
    """
    cur = node
    for _ in range(64):  # guard against pathological nesting
        if cur is None:
            return None
        if cur.type in ("identifier", "shorthand_property_identifier", "type_identifier"):
            return text(cur).strip() or None
        nxt = None
        if hasattr(cur, "child_by_field_name"):
            nxt = cur.child_by_field_name("object") or cur.child_by_field_name("function")
        if nxt is None:
            nxt = cur.children[0] if getattr(cur, "children", None) else None
        cur = nxt
    return None


def _ts_collect_calls(def_node, src_qname: str, src_bytes: bytes, out: ExtractResult) -> None:
    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    import re as _re_local
    _SEP = _re_local.compile(r"\.|::")

    def call_target_and_leftmost(call_node) -> tuple[str | None, str | None]:
        # Ruby/PHP receiver-bearing calls: method/name field is the bare target,
        # and the leftmost (for scope lookup) lives in `receiver` (Ruby) or
        # `scope` (PHP scoped_call_expression).
        receiver_text: str | None = None
        if hasattr(call_node, "child_by_field_name"):
            for fname in ("receiver", "scope", "object"):
                rn = call_node.child_by_field_name(fname)
                if rn is not None:
                    rt = text(rn).strip().lstrip("$").lstrip("\\")
                    if rt:
                        # Take the leftmost segment, treating both `.` and `::`
                        # as path separators (Rust uses ::, JS/Py use .).
                        receiver_text = _SEP.split(rt)[0].split("\\")[-1] or None
                        break
        for field_name in ("function", "name", "method"):
            child = call_node.child_by_field_name(field_name) if hasattr(call_node, "child_by_field_name") else None
            if child is not None:
                # Member/property callee: `a.b.c(...)`, and crucially chained
                # calls `promise.then(h).catch(e)`. The CALLED name is the
                # outermost `property`; the old `text(child).split("(")[0]`
                # returned an INNER segment ("then") for a chain, dropping the
                # real target ("catch") entirely.
                prop = (
                    child.child_by_field_name("property")
                    if hasattr(child, "child_by_field_name")
                    else None
                )
                if prop is not None:
                    target = text(prop).strip()
                    if target:
                        leftmost = _ts_leftmost_ident(child, text) or receiver_text or target
                        return target, leftmost
                t = text(child).split("(")[0].strip()
                if "." in t or "::" in t:
                    parts = [p for p in _SEP.split(t) if p]
                    if not parts:
                        return None, None
                    return parts[-1], parts[0]
                if receiver_text:
                    return t, receiver_text
                return t, t
        for c in call_node.children:
            if c.type in ("identifier", "field_identifier"):
                t = text(c)
                return t, receiver_text or t
        return None, None

    def _callback_arg_refs(call_node, tgt: str) -> None:
        """v0.13 P3: named handlers passed to registration-style calls.

        `app.get('/users', listUsers)` / `emitter.on('x', handler)` hand a
        callable to a framework that invokes it later — without this the
        handler has zero inbound edges and looks dead. Emits a ref for each
        bare-identifier argument when the call's method name is a known
        registration verb. Non-identifier args (strings, arrows, member
        expressions) are ignored — conservative on purpose.
        """
        if tgt.lower() not in _TS_CALLBACK_REG_VERBS:
            return
        args_node = call_node.child_by_field_name("arguments") if hasattr(call_node, "child_by_field_name") else None
        if args_node is None:
            return
        for a in args_node.children:
            if a.type != "identifier":
                continue
            name = text(a)
            if not name:
                continue
            out.refs.append(
                ExtractedRef(
                    src_qname=src_qname,
                    target_name=name,
                    line=a.start_point[0] + 1,
                    ref_type="callback_arg",
                    scope_module=out.imports.get(name),
                )
            )

    # JSX node types that carry a component name (v0.11 P2, bug #20)
    _JSX_OPEN_TYPES = {"jsx_opening_element", "jsx_self_closing_element"}

    def _jsx_component_ref(jsx_node) -> None:
        """Emit a ref for a JSX element whose name is a React component (uppercase).

        Handles:
          <Counter />           -> identifier "Counter"  -> ref to "Counter"
          <Form.Field />        -> member_expression     -> ref to "Form" (leftmost)
          <div />, <span />     -> lowercase identifier  -> SKIP (HTML element)
        """
        # The element name is the first non-punctuation child of jsx_opening_element
        # or jsx_self_closing_element; tree-sitter puts it directly as a child.
        name_node = None
        for c in jsx_node.children:
            if c.type in ("identifier", "member_expression", "jsx_namespace_name"):
                name_node = c
                break
        if name_node is None:
            return

        if name_node.type == "identifier":
            raw = text(name_node)
            if not raw or not raw[0].isupper():
                return  # HTML element — skip
            tgt = raw
            leftmost = raw
        elif name_node.type == "member_expression":
            # e.g. Form.Field — leftmost object is the component namespace
            # member_expression children: object . property_identifier
            obj = name_node.child_by_field_name("object") if hasattr(name_node, "child_by_field_name") else None
            if obj is None:
                for c in name_node.children:
                    if c.type == "identifier":
                        obj = c
                        break
            if obj is None:
                return
            leftmost = text(obj)
            if not leftmost or not leftmost[0].isupper():
                return
            tgt = leftmost
        else:
            return  # jsx_namespace_name (e.g. <Foo:Bar>) — ignore for now

        scope = out.imports.get(tgt)
        if scope is None and leftmost != tgt:
            scope = out.imports.get(leftmost)
        out.refs.append(
            ExtractedRef(
                src_qname=src_qname,
                target_name=tgt,
                line=jsx_node.start_point[0] + 1,
                ref_type="jsx",
                scope_module=scope,
            )
        )

    def _client_route(call_node):
        """v0.21 P2: (method, path) for a fetch/axios client call, else None.

        Conservative: bare ``fetch(...)`` / ``axios(...)``, or ``<client>.verb(...)``
        where the object is a known HTTP-client identifier — so Hono's
        ``app.get('/x', handler)`` (server) never registers as a client call."""
        fn = call_node.child_by_field_name("function") if hasattr(call_node, "child_by_field_name") else None
        if fn is None:
            return None
        method: str | None = None
        if fn.type == "identifier":
            if text(fn) not in ("fetch", "axios"):
                return None
        elif fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            obj = fn.child_by_field_name("object")
            if prop is None or obj is None:
                return None
            verb = text(prop).lower()
            if verb not in _HTTP_CLIENT_VERBS:
                return None
            obj_name = (_ts_leftmost_ident(fn, text) or text(obj)).lower().lstrip("$")
            if obj_name not in _TS_HTTP_CLIENT_OBJS:
                return None
            method = verb.upper()
        else:
            return None
        args_node = call_node.child_by_field_name("arguments")
        if args_node is None:
            return None
        positional = [
            a for a in args_node.children
            if a.type not in ("(", ")", ",", "comment")
        ]
        if not positional or positional[0].type not in ("string", "template_string"):
            return None
        raw = text(positional[0])
        path = raw[1:-1] if len(raw) >= 2 else raw
        if not (path.startswith("/") or path.startswith("http")):
            return None
        # A later positional handler arg (named function or arrow) means this is
        # a SERVER route registration — `api.get('/x', handler)` / Express /
        # Hono — not a client call. This is the definitive discriminator: the
        # object-name allowlist alone is leaky (a router is commonly named
        # `api`/`client`/`request`), but a client call passes only a URL (and
        # maybe a config OBJECT), never a handler function/identifier. Client
        # calls with a bare-identifier config var are rare; precision wins.
        if any(a.type in _TS_HANDLER_ARG_TYPES for a in positional[1:]):
            return None
        return method, path

    def walk(node):
        if node.type in _CALL_NODE_TYPES:
            route = _client_route(node)
            if route is not None:
                method, path = route
                out.routes.append(
                    ExtractedRoute(
                        src_qname=src_qname,
                        role="client",
                        method=method,
                        path=path,
                        line=node.start_point[0] + 1,
                    )
                )
            tgt, leftmost = call_target_and_leftmost(node)
            if tgt:
                # P1.A1: scope_module from imports map. Direct hit on target name
                # (named import), else leftmost identifier (namespace/default import
                # used as `mod.func()`).
                scope = out.imports.get(tgt)
                if scope is None and leftmost and leftmost != tgt:
                    scope = out.imports.get(leftmost)
                out.refs.append(
                    ExtractedRef(
                        src_qname=src_qname,
                        target_name=tgt,
                        line=node.start_point[0] + 1,
                        scope_module=scope,
                    )
                )
                # v0.13 P3: named handlers passed to registration calls.
                _callback_arg_refs(node, tgt)
        # v0.11 P2: JSX element references as call-graph edges (bug #20).
        # Only jsx_opening_element (paired tags) and jsx_self_closing_element
        # are walked; jsx_closing_element is skipped (duplicate of opening tag).
        elif node.type in _JSX_OPEN_TYPES:
            _jsx_component_ref(node)
        for c in node.children:
            # Don't descend into a nested def/class/method — it is extracted and
            # collected as its OWN symbol, so recursing here would attribute its
            # call sites to this enclosing symbol too (a class absorbing every
            # method-body call, a function absorbing a nested function). Inline
            # anonymous arrows/callbacks (not def-typed) are still walked, so
            # their calls correctly belong to the enclosing symbol.
            if c is not def_node and c.type in _DEF_NODE_TYPES:
                continue
            walk(c)

    walk(def_node)


# ---------- TS/JS import scanner (P1.A1) ----------


def _resolve_module_path(source: str, current_dir: tuple[str, ...]) -> str:
    """Map a TS/JS import source string to a dotted module path matching the
    indexer's qname format. Relative paths ('./x', '../y') are resolved against
    `current_dir`; bare specifiers ('lodash', '@scope/pkg') are returned as-is
    (they won't match any in-project symbol, so the resolver harmlessly falls
    through to the global fallback weight=0.5)."""
    if not source.startswith(("./", "../")):
        return source
    parts = list(current_dir)
    for seg in source.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    if parts and parts[-1] in ("index", "index.ts", "index.tsx", "index.js", "index.jsx"):
        parts.pop()
    if parts:
        last = parts[-1]
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            if last.endswith(ext):
                parts[-1] = last[: -len(ext)]
                break
    return ".".join(parts)


def _ts_collect_imports(
    root_node, src_bytes: bytes, current_dir: tuple[str, ...]
) -> dict[str, str]:
    """Scan top-level ES6 imports + CommonJS requires in a JS/TS module.

    Returns local_name -> source_module mapping, where source_module is the
    dotted path of an in-project file (relative imports) or the raw bare
    specifier (external packages — harmless, never resolves)."""
    imports: dict[str, str] = {}

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    def unquote(s: str) -> str:
        if len(s) >= 2 and s[0] in ("'", '"', "`") and s[-1] == s[0]:
            return s[1:-1]
        return s

    def import_source(import_node) -> str | None:
        src = (
            import_node.child_by_field_name("source")
            if hasattr(import_node, "child_by_field_name") else None
        )
        if src is not None:
            return unquote(text(src))
        for c in import_node.children:
            if c.type == "string":
                return unquote(text(c))
        return None

    def collect_clause(clause_node, module: str) -> None:
        for c in clause_node.children:
            if c.type == "identifier":
                imports[text(c)] = module
            elif c.type == "namespace_import":
                for nc in c.children:
                    if nc.type == "identifier":
                        imports[text(nc)] = module
            elif c.type == "named_imports":
                for spec in c.children:
                    if spec.type != "import_specifier":
                        continue
                    name_n = (
                        spec.child_by_field_name("name")
                        if hasattr(spec, "child_by_field_name") else None
                    )
                    alias_n = (
                        spec.child_by_field_name("alias")
                        if hasattr(spec, "child_by_field_name") else None
                    )
                    if alias_n is not None:
                        imports[text(alias_n)] = module
                    elif name_n is not None:
                        imports[text(name_n)] = module
                    else:
                        for sc in spec.children:
                            if sc.type == "identifier":
                                imports[text(sc)] = module
                                break

    def collect_require(declarator_node) -> None:
        value = (
            declarator_node.child_by_field_name("value")
            if hasattr(declarator_node, "child_by_field_name") else None
        )
        if value is None or value.type not in ("call_expression", "call"):
            return
        fn = (
            value.child_by_field_name("function")
            if hasattr(value, "child_by_field_name") else None
        )
        if fn is None or text(fn) != "require":
            return
        args = (
            value.child_by_field_name("arguments")
            if hasattr(value, "child_by_field_name") else None
        )
        if args is None:
            return
        source: str | None = None
        for a in args.children:
            if a.type == "string":
                source = unquote(text(a))
                break
        if source is None:
            return
        module = _resolve_module_path(source, current_dir)
        name = (
            declarator_node.child_by_field_name("name")
            if hasattr(declarator_node, "child_by_field_name") else None
        )
        if name is None:
            return
        if name.type == "identifier":
            imports[text(name)] = module
        elif name.type == "object_pattern":
            for c in name.children:
                if c.type == "shorthand_property_identifier_pattern":
                    imports[text(c)] = module
                elif c.type == "pair_pattern":
                    val = (
                        c.child_by_field_name("value")
                        if hasattr(c, "child_by_field_name") else None
                    )
                    if val is not None and val.type == "identifier":
                        imports[text(val)] = module

    # Walk only top-level statements (program → child).
    for child in root_node.children:
        if child.type == "import_statement":
            source = import_source(child)
            if source is None:
                continue
            module = _resolve_module_path(source, current_dir)
            clause = (
                child.child_by_field_name("import_clause")
                if hasattr(child, "child_by_field_name") else None
            )
            if clause is not None:
                collect_clause(clause, module)
            else:
                for c in child.children:
                    if c.type == "import_clause":
                        collect_clause(c, module)
        elif child.type in ("lexical_declaration", "variable_declaration"):
            for c in child.children:
                if c.type == "variable_declarator":
                    collect_require(c)
    return imports


# ---------- Go import scanner (P1.A2) ----------


def _go_collect_imports(root_node, src_bytes: bytes) -> dict[str, str]:
    """Scan top-level Go imports.

    Returns local_name -> scope_module mapping. The scope is the last segment
    of the import path (the package name as Go conventions assume), regardless
    of any alias — matches the indexer's qname format because every symbol in
    a package gets a qname containing that package segment.

    Skips `_` (blank) and `.` (dot) imports."""
    imports: dict[str, str] = {}

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    def unquote(s: str) -> str:
        if len(s) >= 2 and s[0] in ("'", '"', "`") and s[-1] == s[0]:
            return s[1:-1]
        return s

    def add_spec(spec) -> None:
        path_n = (
            spec.child_by_field_name("path")
            if hasattr(spec, "child_by_field_name") else None
        )
        if path_n is None:
            for c in spec.children:
                if c.type in ("interpreted_string_literal", "raw_string_literal"):
                    path_n = c
                    break
        if path_n is None:
            return
        path = unquote(text(path_n)).strip()
        if not path:
            return
        scope = path.rsplit("/", 1)[-1]
        name_n = (
            spec.child_by_field_name("name")
            if hasattr(spec, "child_by_field_name") else None
        )
        if name_n is not None:
            local = text(name_n).strip()
            if local in (".", "_", ""):
                return
        else:
            local = scope
        imports[local] = scope

    for child in root_node.children:
        if child.type != "import_declaration":
            continue
        for c in child.children:
            if c.type == "import_spec_list":
                for spec in c.children:
                    if spec.type == "import_spec":
                        add_spec(spec)
            elif c.type == "import_spec":
                add_spec(c)
    return imports


# ---------- Ruby require_relative scanner (P1.A4 best-effort) ----------


def _rb_collect_imports(
    root_node, src_bytes: bytes, current_dir: tuple[str, ...]
) -> dict[str, str]:
    """Scan top-level `require_relative 'foo/bar'` calls in Ruby.

    Each relative require seeds an entry whose KEY is the basename (so a call
    like `Bar.thing` or `bar` from the requiring file matches it heuristically)
    and whose VALUE is the dotted module path under current_dir. Best-effort —
    Ruby has no static binding from require to constant names, so this only
    helps when the calling code uses the basename verbatim. `require 'foo'`
    (non-relative, library load) is ignored.
    """
    imports: dict[str, str] = {}

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    def unquote(s: str) -> str:
        if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
            return s[1:-1]
        return s

    for child in root_node.children:
        # tree-sitter-ruby parses `require_relative "x"` as a `call` node with
        # method=identifier "require_relative" and arguments=argument_list.
        if child.type not in ("call", "method_call", "command"):
            continue
        method_n = (
            child.child_by_field_name("method")
            if hasattr(child, "child_by_field_name") else None
        )
        if method_n is None:
            for c in child.children:
                if c.type == "identifier":
                    method_n = c
                    break
        if method_n is None or text(method_n) != "require_relative":
            continue
        # Find the string argument
        arg_text: str | None = None
        for c in child.children:
            if c.type in ("argument_list", "command_argument_list"):
                for ac in c.children:
                    if ac.type == "string":
                        arg_text = unquote(text(ac))
                        break
                if arg_text:
                    break
            elif c.type == "string":
                arg_text = unquote(text(c))
                break
        if not arg_text:
            continue
        path = arg_text
        if path.endswith(".rb"):
            path = path[:-3]
        parts = list(current_dir)
        for seg in path.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        if not parts:
            continue
        basename = parts[-1]
        scope = ".".join(parts)
        # Map the basename (lowercase + capitalized + camel) to the scope.
        imports[basename] = scope
        cap = basename[:1].upper() + basename[1:]
        if cap != basename:
            imports[cap] = scope
    return imports


# ---------- PHP `use` namespace scanner (P1.A4 best-effort) ----------


def _php_collect_imports(root_node, src_bytes: bytes) -> dict[str, str]:
    """Scan top-level PHP `use Some\\Namespace\\X;` and `use Foo\\Bar as Baz;`.

    Returns local_name -> scope_module where scope_module is the dotted form
    of the fully-qualified name. Resolution is heuristic: PHP qnames in this
    project are file-derived (not namespace-derived), so the scope only helps
    when the imported class lives in a file path that mirrors its namespace.
    """
    imports: dict[str, str] = {}

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    def visit(node):
        if node.type in ("namespace_use_declaration", "use_declaration"):
            # Children include namespace_use_clause(s) and `use` keyword
            for c in node.children:
                if c.type in ("namespace_use_clause", "use_clause"):
                    handle_clause(c)
            return
        for c in node.children:
            visit(c)

    def handle_clause(clause):
        # Clause shape: qualified_name [ as alias ]
        qname_n = None
        alias_n = None
        for c in clause.children:
            if c.type in ("qualified_name", "name", "namespace_name"):
                qname_n = c
            elif c.type in ("namespace_aliasing_clause", "use_alias"):
                for ac in c.children:
                    if ac.type in ("name", "identifier"):
                        alias_n = ac
        if qname_n is None:
            return
        full = text(qname_n).strip().lstrip("\\")
        if not full:
            return
        parts = full.replace("/", "\\").split("\\")
        local = text(alias_n).strip() if alias_n is not None else parts[-1]
        scope = ".".join(parts)
        imports[local] = scope

    visit(root_node)
    return imports


# ---------- Rust use-declaration scanner (P4.A3 v0.5) ----------


def _rs_collect_imports(root_node, src_bytes: bytes) -> dict[str, str]:
    """Scan top-level `use` declarations in a Rust file.

    Returns local_name -> scope_module mapping where scope is the parent
    module of the imported item (the segment before it in the use path),
    matching the qname format the indexer assigns. Examples:
        use crate::util::Greeter        -> {Greeter: util}
        use crate::util::Greeter as G   -> {G: util}
        use crate::util::{a, b}         -> {a: util, b: util}
        use crate::a::b::Item           -> {Item: b}
        use crate::Item                 -> {Item: Item}   (root-level fallback)
    `use foo::*` (wildcard) is skipped — would need module-content knowledge.
    """
    imports: dict[str, str] = {}

    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    for child in root_node.children:
        if child.type != "use_declaration":
            continue
        full = text(child)
        m = _re_rs.match(r"^\s*use\s+(.+?)\s*;?\s*$", full, _re_rs.DOTALL)
        if not m:
            continue
        _rs_parse_use_string(m.group(1).strip(), imports)
    return imports


def _rs_parse_use_string(payload: str, imports: dict[str, str]) -> None:
    payload = payload.strip().lstrip(":")
    if not payload:
        return
    # Wildcard import — can't enumerate
    if payload.endswith("::*") or payload == "*":
        return
    # Brace group: prefix::{a, b as c, sub::nested}
    brace_open = payload.find("{")
    if brace_open != -1:
        prefix = payload[:brace_open].strip().rstrip(":")
        brace_close = payload.rfind("}")
        if brace_close <= brace_open:
            return
        inner = payload[brace_open + 1 : brace_close]
        for item in _rs_split_top_level(inner, ","):
            item = item.strip()
            if not item:
                continue
            joined = f"{prefix}::{item}" if prefix else item
            _rs_parse_use_string(joined, imports)
        return
    # Plain path, optional `as` alias
    if " as " in payload:
        path, _, alias = payload.partition(" as ")
        path = path.strip()
        local = alias.strip()
    else:
        path = payload.strip()
        local = path.rsplit("::", 1)[-1].strip()
    if local == "*":
        return
    segs = [s for s in path.split("::") if s and s != "crate"]
    if len(segs) >= 2:
        scope = segs[-2]
    elif segs:
        scope = segs[0]
    else:
        scope = local
    imports[local] = scope


def _rs_split_top_level(s: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


# ---------- Dispatcher ----------


def extract(path: Path, source: str, project_root: Path) -> tuple[str, ExtractResult]:
    """Return (language, ExtractResult). Falls back to empty result for unknown langs."""
    lang = detect_language(path)
    if lang is None:
        return "unknown", ExtractResult()
    rel = path.relative_to(project_root) if path.is_absolute() and path.is_relative_to(project_root) else path
    module_name = ".".join(rel.with_suffix("").parts)
    if lang == "python":
        return lang, _py_extract(source, module_name)
    current_dir = rel.parent.parts
    return lang, _ts_extract(source, lang, module_name, current_dir)
