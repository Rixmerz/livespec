"""Symbol and reference extractors per language.

Each extractor returns:
  symbols: list[ExtractedSymbol]
  refs:    list[ExtractedRef]   (raw call / reference names; resolved later)
"""

from __future__ import annotations

import ast
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


@dataclass
class ExtractedRef:
    src_qname: str
    target_name: str  # last name of the call (e.g. "foo" or "Cls.method")
    line: int
    ref_type: str = "call"
    scope_module: str | None = None  # P0.4: module name imported as the source of `target_name`


@dataclass
class ExtractResult:
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    refs: list[ExtractedRef] = field(default_factory=list)
    # P0.4: per-file imports map. local_name -> source_module (qualified name of
    # the module providing it). Used by the resolver to scope short-name lookups.
    imports: dict[str, str] = field(default_factory=dict)


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

    visit(tree, None, in_class=False)
    return out


def _collect_calls(func_node: ast.AST, src_qname: str, out: ExtractResult) -> None:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
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


# ---------- Generic tree-sitter ----------

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

    if language in ("javascript", "typescript"):
        out.imports.update(_ts_collect_imports(tree.root_node, src_bytes, current_dir))
    elif language == "go":
        out.imports.update(_go_collect_imports(tree.root_node, src_bytes))
    elif language == "ruby":
        out.imports.update(_rb_collect_imports(tree.root_node, src_bytes, current_dir))
    elif language == "php":
        out.imports.update(_php_collect_imports(tree.root_node, src_bytes))

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
        out.symbols.append(
            ExtractedSymbol(
                name=name,
                qualified_name=qname,
                kind=kind,
                signature=signature,
                docstring=None,
                body_hash_seed=text(node),
                start_line=start_line,
                end_line=end_line,
                parent_qname=parent_qname,
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


def _ts_collect_calls(def_node, src_qname: str, src_bytes: bytes, out: ExtractResult) -> None:
    def text(n) -> str:
        return src_bytes[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

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
                        receiver_text = rt.split(".")[0].split("\\")[-1]
                        break
        for field_name in ("function", "name", "method"):
            child = call_node.child_by_field_name(field_name) if hasattr(call_node, "child_by_field_name") else None
            if child is not None:
                t = text(child).split("(")[0].strip()
                if "." in t:
                    parts = [p for p in t.split(".") if p]
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

    def walk(node):
        if node.type in _CALL_NODE_TYPES:
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
        for c in node.children:
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
