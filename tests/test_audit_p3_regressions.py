"""Regression tests for the audit batch P3 correctness fixes."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from livespec_mcp.domain.extractors import _py_extract, extract
from livespec_mcp.domain.graph import descendants_within
from livespec_mcp.domain.matcher import parse_annotations
from livespec_mcp.domain.md_specs import (
    UnsupportedSpecCatalogError,
    parse_openspec_markdown,
    reject_legacy_spec_catalog,
)


def test_descendants_within_is_bfs_not_dfs():
    """H1: a node reachable within max_depth via a SHORT path must be found
    even if a longer path discovers it first."""
    g = nx.DiGraph()
    g.add_edges_from([(1, 2), (2, 3), (3, 4), (1, 5), (5, 4), (4, 6)])
    got = descendants_within(g, 1, 3)
    assert 6 in got, f"node 6 (depth 3 via 1->5->4->6) missing: {got}"


def test_python_extractor_sees_conditionally_defined_symbols():
    """H7: functions under if/try blocks are extracted."""
    src = (
        "import typing\n"
        "if typing.TYPE_CHECKING:\n"
        "    def only_typing():\n"
        "        pass\n"
        "try:\n"
        "    def primary():\n"
        "        pass\n"
        "except Exception:\n"
        "    def primary_fallback():\n"
        "        pass\n"
    )
    res = _py_extract(src, "mod")
    names = {s.name for s in res.symbols}
    assert {"only_typing", "primary", "primary_fallback"} <= names, names


def test_python_nested_calls_not_double_attributed():
    """M11: a call inside an inner function is attributed to the inner symbol
    only, not to every enclosing def."""
    src = (
        "def outer():\n"
        "    def inner():\n"
        "        helper()\n"
        "    return inner\n"
    )
    res = _py_extract(src, "mod")
    helper_srcs = {r.src_qname for r in res.refs if r.target_name == "helper"}
    assert helper_srcs == {"mod.outer.inner"}, helper_srcs


def test_python_parse_error_flag_set():
    """C4: a syntax error surfaces parse_error=True with no symbols."""
    res = _py_extract("def broken(:\n    pass\n", "mod")
    assert res.parse_error is True
    assert res.symbols == []


def test_matcher_verb_boundary_rejects_prefix_words():
    """H10: @specifically / @testsuite / @seed must NOT create 1.0 links."""
    known = ["auth-user-login"]
    for text in (
        "@specifically auth-user-login is out of scope",
        "@testsuite for auth-user-login",
        "@seed auth-user-login data loader",
    ):
        hits = parse_annotations(text, known_ids=known)
        assert all(h.confidence < 1.0 for h in hits), (text, hits)


def test_matcher_real_prefix_still_matches():
    hits = parse_annotations(
        "@spec:auth-user-login", known_ids=["auth-user-login"]
    )
    assert any(h.spec_id == "auth-user-login" and h.confidence == 1.0 for h in hits)


def test_matcher_cannot_negation():
    """L14: 'cannot implement auth-user-login' is a negation."""
    hits = parse_annotations(
        "this cannot implement auth-user-login yet",
        known_ids=["auth-user-login"],
    )
    assert all(h.spec_id != "auth-user-login" for h in hits)


def test_md_specs_ignores_legacy_headers_in_code_fences():
    """M13: a SPEC header inside a ``` block does not trip the hard-cut."""
    md = (
        "### Requirement: Real\n"
        "The system SHALL work.\n\n"
        "```\n"
        "## SPEC-099: Example in a code block\n"
        "```\n"
    )
    reject_legacy_spec_catalog(md)
    specs = parse_openspec_markdown(md)
    assert {s.spec_id for s in specs} == {"real"}


def test_md_specs_rejects_native_catalog():
    """Native ## SPEC-NNN: dialect is removed."""
    with pytest.raises(UnsupportedSpecCatalogError):
        parse_openspec_markdown("## SPEC-001: Dashboard\nstatus: active users\n")


def test_ts_chained_method_calls_each_recorded(tmp_path: Path):
    """H8: promise.then(h).catch(e) records BOTH `then` and `catch`, not
    `then` twice with `catch` dropped."""
    src = (
        "function run() {\n"
        "  promise.then(handler).catch(onErr);\n"
        "}\n"
    )
    p = tmp_path / "main.ts"
    p.write_text(src, encoding="utf-8")
    _, result = extract(p, src, tmp_path)
    targets = {r.target_name for r in result.refs if r.src_qname.endswith("run")}
    assert "then" in targets, targets
    assert "catch" in targets, targets


def test_tsx_imports_and_visibility_extracted(tmp_path: Path):
    """H9: a .tsx file gets import scoping and exported visibility, same as .ts."""
    src = (
        "import { Widget } from './widget';\n"
        "export function App() {\n"
        "  return Widget();\n"
        "}\n"
    )
    p = tmp_path / "App.tsx"
    p.write_text(src, encoding="utf-8")
    _, result = extract(p, src, tmp_path)
    assert result.imports, "tsx imports not scanned"
    app = next((s for s in result.symbols if s.name == "App"), None)
    assert app is not None and app.visibility == "exported", app
