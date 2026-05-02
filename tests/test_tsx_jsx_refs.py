"""v0.11 P2 — JSX element references as call-graph edges (bug #20).

Tests that TSX files using <ComponentName /> produce symbol_edge rows
linking the enclosing function/component to the JSX child component.
Also verifies that HTML lowercase elements do NOT produce edges and that
find_dead_code does not flag components that are only used as JSX elements.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from livespec_mcp.config import Settings
from livespec_mcp.domain.extractors import extract
from livespec_mcp.domain.indexer import index_project
from livespec_mcp.storage.db import connect

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- helpers ----------


def _bootstrap(tmp_path: Path) -> tuple[Settings, sqlite3.Connection]:
    state = tmp_path / ".mcp-docs"
    settings = Settings(
        workspace=tmp_path,
        state_dir=state,
        db_path=state / "docs.db",
        docs_dir=state / "docs",
        models_dir=state / "models",
    )
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    return settings, conn


def _edge_exists(conn: sqlite3.Connection, src_qname: str, dst_qname: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN symbol d ON d.id = e.dst_symbol_id
           WHERE s.qualified_name = ? AND d.qualified_name = ?""",
        (src_qname, dst_qname),
    ).fetchone()
    return row is not None


def _ref_targets(result) -> set[str]:
    return {r.target_name for r in result.refs}


# ---------- extractor-level unit tests ----------


def test_jsx_self_closing_emits_ref(tmp_path: Path):
    """<Counter /> inside App() must emit a ref to 'Counter'."""
    tsx = tmp_path / "app.tsx"
    tsx.write_text(
        "function Counter() { return <div />; }\n"
        "function App() { return <Counter />; }\n"
    )
    _, result = extract(tsx, tsx.read_text(), tmp_path)
    assert "Counter" in _ref_targets(result), (
        f"Expected 'Counter' in refs, got: {_ref_targets(result)}"
    )


def test_jsx_paired_element_emits_ref(tmp_path: Path):
    """<Counter>...</Counter> (paired tag) must also emit a ref to 'Counter'."""
    tsx = tmp_path / "app.tsx"
    tsx.write_text(
        "function Counter() { return <span>hi</span>; }\n"
        "function App() { return <Counter>x</Counter>; }\n"
    )
    _, result = extract(tsx, tsx.read_text(), tmp_path)
    assert "Counter" in _ref_targets(result), (
        f"Expected 'Counter' in refs (paired tag), got: {_ref_targets(result)}"
    )


def test_jsx_member_expression_emits_leftmost(tmp_path: Path):
    """<Form.Field /> must emit a ref to 'Form' (leftmost segment)."""
    tsx = tmp_path / "app.tsx"
    tsx.write_text(
        "const Form = { Field: () => <input /> };\n"
        "function App() { return <Form.Field />; }\n"
    )
    _, result = extract(tsx, tsx.read_text(), tmp_path)
    assert "Form" in _ref_targets(result), (
        f"Expected 'Form' in refs (member_expression), got: {_ref_targets(result)}"
    )


def test_jsx_lowercase_html_no_ref(tmp_path: Path):
    """<div>, <span>, <a> must NOT produce refs (HTML elements)."""
    tsx = tmp_path / "app.tsx"
    tsx.write_text(
        'function App() { return <div><span><a href="#">link</a></span></div>; }\n'
    )
    _, result = extract(tsx, tsx.read_text(), tmp_path)
    html_tags = {"div", "span", "a"}
    leaked = html_tags & _ref_targets(result)
    assert not leaked, f"HTML tags leaked as refs: {leaked}"


def test_jsx_multiple_components_in_one_function(tmp_path: Path):
    """Multiple distinct JSX children — all uppercase ones get refs, lowercase don't."""
    tsx = tmp_path / "app.tsx"
    tsx.write_text(
        "function Header() { return <header />; }\n"
        "function Footer() { return <footer />; }\n"
        "function App() {\n"
        "  return <div><Header /><Footer /></div>;\n"
        "}\n"
    )
    _, result = extract(tsx, tsx.read_text(), tmp_path)
    targets = _ref_targets(result)
    assert "Header" in targets, f"Expected 'Header' in refs, got: {targets}"
    assert "Footer" in targets, f"Expected 'Footer' in refs, got: {targets}"
    assert "div" not in targets, f"'div' must not be a ref, got: {targets}"


# ---------- integration tests (index_project + symbol_edge) ----------


def test_jsx_edge_in_call_graph(tmp_path: Path):
    """After index_project, App -> Counter edge exists in symbol_edge."""
    (tmp_path / "app.tsx").write_text(
        "export function Counter() { return <span>0</span>; }\n"
        "export function App() { return <Counter />; }\n"
    )
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)

    assert _edge_exists(conn, "app.App", "app.Counter"), (
        "Expected symbol_edge App -> Counter after JSX usage"
    )
    conn.close()


def test_jsx_no_html_edge_in_call_graph(tmp_path: Path):
    """<div>, <span> must not create symbol_edge rows."""
    (tmp_path / "app.tsx").write_text(
        "export function App() { return <div><span>hello</span></div>; }\n"
    )
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)

    # No symbol for "div" or "span" should exist, so no edge is possible.
    rows = conn.execute(
        "SELECT qualified_name FROM symbol WHERE name IN ('div', 'span', 'a')"
    ).fetchall()
    assert not rows, f"HTML tag symbols must not be created: {rows}"
    conn.close()


def test_jsx_member_expression_ref_emitted(tmp_path: Path):
    """<Form.Field /> — extractor emits a ref to 'Form' (leftmost segment).

    The edge resolves only when a symbol named 'Form' exists in the project.
    Here we confirm the ref is present at extraction time; the integration
    edge test uses an extractable symbol so the resolver can create the row.
    """
    tsx = tmp_path / "app.tsx"
    tsx.write_text(
        "function Form() { return <div />; }\n"
        "function App() { return <Form.Field />; }\n"
    )
    _, result = extract(tsx, tsx.read_text(), tmp_path)
    assert "Form" in _ref_targets(result), (
        f"Expected 'Form' in refs for <Form.Field />, got: {_ref_targets(result)}"
    )


def test_jsx_member_expression_edge_to_existing_symbol(tmp_path: Path):
    """<Form.Field /> creates an edge to 'Form' when Form is an extractable function."""
    (tmp_path / "forms.tsx").write_text(
        "export function Form() { return <div />; }\n"
    )
    (tmp_path / "app.tsx").write_text(
        "import { Form } from './forms';\n"
        "export function App() { return <Form.Field />; }\n"
    )
    settings, conn = _bootstrap(tmp_path)
    index_project(settings, conn)

    # Edge from App to Form must exist (leftmost of Form.Field)
    assert _edge_exists(conn, "app.App", "forms.Form"), (
        "Expected edge App -> Form for <Form.Field /> usage"
    )
    conn.close()


def test_find_dead_code_not_flagged_when_jsx_used(tmp_path: Path):
    """find_dead_code must NOT report Counter as dead when it's only used as <Counter />."""
    from fastmcp import Client
    from livespec_mcp.server import mcp
    from livespec_mcp import state as state_module
    from livespec_mcp.domain.graph import invalidate_graph_cache
    import os, asyncio

    os.environ["LIVESPEC_WORKSPACE"] = str(tmp_path)
    state_module.reset_state()
    invalidate_graph_cache()

    (tmp_path / "counter.tsx").write_text(
        "export function Counter(props: { count: number }) {\n"
        "  return <span>{props.count}</span>;\n"
        "}\n"
    )
    (tmp_path / "app.tsx").write_text(
        "import { Counter } from './counter';\n"
        "export default function App() {\n"
        "  return <Counter count={0} />;\n"
        "}\n"
    )

    async def _run():
        async with Client(mcp) as c:
            await c.call_tool("index_project", {})
            result = (await c.call_tool("find_dead_code", {})).data
            dead_names = {s["name"] for s in result.get("dead_symbols", [])}
            assert "Counter" not in dead_names, (
                f"Counter should not be dead when used as JSX element, "
                f"but find_dead_code reported: {dead_names}"
            )

    asyncio.run(_run())

    state_module.reset_state()
    invalidate_graph_cache()
