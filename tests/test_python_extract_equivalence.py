"""The faster Python extraction walks produce exactly what the old ones did.

The statement-only walk must yield the statements in `ast.walk`'s order (the
first nested import of a name wins its binding), and `_py_children` the
nodes `ast.iter_child_nodes` does, less the `ctx` leaves no walk acts on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from livespec_mcp.domain.extractors import _py_children, _py_walk_statements

_SRC = Path(__file__).resolve().parents[1] / "src" / "livespec_mcp"
_FILES = sorted(_SRC.rglob("*.py"))
_STATEMENTISH = (ast.stmt, ast.excepthandler, ast.match_case, ast.Module)


def _trees():
    for path in _FILES:
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_statement_walk_keeps_ast_walk_order():
    for path, tree in _trees():
        want = [n for n in ast.walk(tree) if isinstance(n, _STATEMENTISH)]
        assert list(_py_walk_statements(tree)) == want, path


@pytest.mark.parametrize("path", _FILES[:40], ids=lambda p: p.name)
def test_children_are_iter_child_nodes_without_ctx(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        want = [c for c in ast.iter_child_nodes(node) if not isinstance(c, ast.expr_context)]
        assert _py_children(node) == want
