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


@dataclass
class ExtractResult:
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    refs: list[ExtractedRef] = field(default_factory=list)


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
                out.refs.append(
                    ExtractedRef(
                        src_qname=src_qname,
                        target_name=target,
                        line=getattr(node, "lineno", 0),
                    )
                )


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
}

_CALL_NODE_TYPES = {
    "call_expression",
    "function_call",
    "method_invocation",
    "invocation_expression",
    "call",
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

    def walk(node, parent_qname: str | None):
        if node.type in _DEF_NODE_TYPES:
            name = find_name(node)
            if name:
                qname = f"{parent_qname}.{name}" if parent_qname else f"{module_name}.{name}"
                kind = "class" if "class" in node.type or "interface" in node.type else (
                    "method" if "method" in node.type else "function"
                )
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                signature = text(node).splitlines()[0][:300] if node.start_byte != node.end_byte else None
                seed = text(node)
                out.symbols.append(
                    ExtractedSymbol(
                        name=name,
                        qualified_name=qname,
                        kind=kind,
                        signature=signature,
                        docstring=None,
                        body_hash_seed=seed,
                        start_line=start_line,
                        end_line=end_line,
                        parent_qname=parent_qname,
                    )
                )
                # Collect calls inside this def
                _ts_collect_calls(node, qname, src_bytes, out)
                # Recurse into children with this qname as parent
                for c in node.children:
                    walk(c, qname)
                return
        for c in node.children:
            walk(c, parent_qname)

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
