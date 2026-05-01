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


def _ts_extract(source: str, language: str, module_name: str) -> ExtractResult:
    out = ExtractResult()
    try:
        parser = get_parser(language)
    except Exception:
        return out
    src_bytes = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_bytes)

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

    def call_target(call_node) -> str | None:
        # Field "function" or "object" varies; try common ones
        for field_name in ("function", "name", "method"):
            child = call_node.child_by_field_name(field_name) if hasattr(call_node, "child_by_field_name") else None
            if child is not None:
                # If it's an attribute access, take the rightmost identifier
                t = text(child)
                if "." in t:
                    return t.rsplit(".", 1)[-1]
                return t.split("(")[0].strip()
        # Fallback: first identifier child
        for c in call_node.children:
            if c.type in ("identifier", "field_identifier"):
                return text(c)
        return None

    def walk(node):
        if node.type in _CALL_NODE_TYPES:
            tgt = call_target(node)
            if tgt:
                out.refs.append(
                    ExtractedRef(
                        src_qname=src_qname,
                        target_name=tgt,
                        line=node.start_point[0] + 1,
                    )
                )
        for c in node.children:
            walk(c)

    walk(def_node)


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
    return lang, _ts_extract(source, lang, module_name)
