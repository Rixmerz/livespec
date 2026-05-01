"""Per-language extractor tests.

For each supported language, verify the extractor finds:
  - top-level functions
  - class/struct/method definitions
  - cross-symbol calls (helper() invoked from another fn)
  - language-specific quirks (arrow fn JS/TS, impl Rust, struct method Go)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from livespec_mcp.domain.extractors import extract

FIXTURES = Path(__file__).parent / "fixtures"


def _names(result):
    return {s.name for s in result.symbols}


def _call_targets(result):
    return {r.target_name for r in result.refs}


@pytest.mark.parametrize(
    "fixture_path,must_have_symbols,must_have_calls",
    [
        # Python: ast-based, ground truth
        (
            FIXTURES / "python" / "sample.py",
            {"top_level_one", "top_level_two", "helper", "Greeter", "__init__", "greet"},
            {"helper"},
        ),
        # Go: standard tree-sitter grammar
        (
            FIXTURES / "go" / "sample.go",
            {"Helper", "TopLevelOne", "Greeter", "Greet"},
            {"Helper"},
        ),
        # Java
        (
            FIXTURES / "java" / "Sample.java",
            {"helper", "topLevelOne", "Sample", "Greeter", "greet"},
            {"helper"},
        ),
        # Ruby (P2.2)
        (
            FIXTURES / "ruby" / "sample.rb",
            {"helper", "top_level_one", "Greeter", "initialize", "greet"},
            {"helper"},
        ),
        # PHP (P2.2)
        (
            FIXTURES / "php" / "sample.php",
            {"helper", "topLevelOne", "Greeter", "__construct", "greet"},
            {"helper"},
        ),
    ],
)
def test_extractor_basic(fixture_path: Path, must_have_symbols: set[str], must_have_calls: set[str]):
    source = fixture_path.read_text(encoding="utf-8")
    _, result = extract(fixture_path, source, fixture_path.parent)
    names = _names(result)
    missing = must_have_symbols - names
    assert not missing, f"Missing symbols in {fixture_path.name}: {missing}. Got: {names}"
    targets = _call_targets(result)
    missing_calls = must_have_calls - targets
    assert not missing_calls, f"Missing calls in {fixture_path.name}: {missing_calls}. Got: {targets}"


@pytest.mark.parametrize(
    "fixture_path",
    [FIXTURES / "javascript" / "sample.js", FIXTURES / "typescript" / "sample.ts"],
)
def test_arrow_functions_extracted(fixture_path: Path):
    """Regression: P0.1 — arrow functions assigned to const/let must be captured."""
    source = fixture_path.read_text(encoding="utf-8")
    _, result = extract(fixture_path, source, fixture_path.parent)
    names = _names(result)
    # function declarations
    assert "helper" in names, f"function declaration missing in {fixture_path.name}: {names}"
    assert "topLevelOne" in names, f"missing topLevelOne in {fixture_path.name}: {names}"
    # arrow functions assigned to const must surface by their binding name
    assert "arrowFn" in names, f"arrow fn missing in {fixture_path.name}: {names}"
    assert "arrowFnBlock" in names, f"block arrow fn missing in {fixture_path.name}: {names}"
    # class + method
    assert "Greeter" in names
    assert "greet" in names


def test_rust_impl_block_methods():
    """Regression: P0.2 — Rust impl blocks must yield methods with Type::name qname."""
    fixture_path = FIXTURES / "rust" / "sample.rs"
    source = fixture_path.read_text(encoding="utf-8")
    _, result = extract(fixture_path, source, fixture_path.parent)
    names = _names(result)
    qnames = {s.qualified_name for s in result.symbols}

    # Free functions
    assert "helper" in names
    assert "top_level_one" in names
    # Struct
    assert "Greeter" in names
    # Methods inside impl block — must use :: separator, not .
    assert "new" in names, f"impl method `new` not extracted: {names}"
    assert "greet" in names, f"impl method `greet` not extracted: {names}"
    assert any("Greeter::new" in q for q in qnames), f"Greeter::new qname not found: {qnames}"
    assert any("Greeter::greet" in q for q in qnames), f"Greeter::greet qname not found: {qnames}"
    # Calls
    assert "helper" in _call_targets(result)
