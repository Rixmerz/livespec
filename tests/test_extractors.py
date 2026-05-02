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


@pytest.mark.parametrize(
    "lang_dir,main_file",
    [
        ("typescript", "main.ts"),
        ("javascript", "main.js"),
    ],
)
def test_ts_js_scoped_resolution_imports(lang_dir: str, main_file: str):
    """P1.A1: ES6 imports / CommonJS requires populate `imports` + `scope_module`
    on call refs. Named, namespace, default and CJS destructuring/whole-module
    forms must all resolve to a dotted in-project module path that matches the
    indexer's qname format."""
    root = FIXTURES / lang_dir / "cross_module"
    p = root / main_file
    _, result = extract(p, p.read_text(encoding="utf-8"), root)

    # Imports map populated
    assert "helper" in result.imports, f"named import 'helper' missing: {result.imports}"
    assert result.imports["helper"] == "helpers"
    assert "utils" in result.imports, f"namespace/whole-module 'utils' missing: {result.imports}"
    assert result.imports["utils"] == "utils"

    # Refs carry scope_module — both for direct named call (helper())
    # and for member access on a namespace (utils.format())
    refs_by_target = {r.target_name: r.scope_module for r in result.refs}
    assert refs_by_target.get("helper") == "helpers", (
        f"helper() should be scoped to 'helpers', got: {refs_by_target}"
    )
    assert refs_by_target.get("format") == "utils", (
        f"utils.format() should be scoped to 'utils' via leftmost lookup, got: {refs_by_target}"
    )


def test_ts_jsdoc_docstring_populated(tmp_path: Path):
    """JSDoc `/** ... */` immediately preceding a TS declaration is captured
    as the symbol's docstring so the @rf: matcher can find tags. Covers the
    bug where TS symbols always had `docstring=None` even when an
    `@rf:RF-NNN` annotation lived right above the function."""
    src = """/**
 * Fetches a token.
 * @rf:BE-RF-016
 */
export async function getManyChatToken() { return 'x'; }

/** @rf:RF-001, RF-002 */
function helper() {}

// @rf:RF-009
function lineCommented() {}
"""
    p = tmp_path / "main.ts"
    p.write_text(src, encoding="utf-8")
    _, result = extract(p, src, tmp_path)
    by_name = {s.name: s for s in result.symbols}
    assert "getManyChatToken" in by_name
    assert "@rf:BE-RF-016" in (by_name["getManyChatToken"].docstring or "")
    assert "@rf:RF-001" in (by_name["helper"].docstring or "")
    # // line comments are also kept so inline `@rf:` tags still match
    assert "@rf:RF-009" in (by_name["lineCommented"].docstring or "")


def test_ts_jsdoc_wins_over_adjacent_separator_line_comment(tmp_path: Path):
    """Bug from real session: `// ---\n/** @rf:RF-001 */\nfunction foo() {}`
    used to concatenate raw text and run a single strip pass keyed on the
    leading `//`, leaving the block's `/**` mid-text and defeating the
    matcher's line-start anchor for `@rf:`. Each comment must be stripped
    individually, and pure ASCII separator lines (`// ---`) must not be
    chosen as the docstring lead.
    """
    from livespec_mcp.domain.matcher import parse_annotations

    src = """// ---
// helper section
/**
 * Resolve a token from cache.
 * @rf:BE-RF-016
 */
export async function getManyChatToken() { return 'x'; }
"""
    p = tmp_path / "main.ts"
    p.write_text(src, encoding="utf-8")
    _, result = extract(p, src, tmp_path)
    sym = next(s for s in result.symbols if s.name == "getManyChatToken")
    assert sym.docstring is not None
    # Pure separator line dropped — lead is the meaningful content.
    assert not sym.docstring.lstrip().startswith("---")
    # Block delimiters cleaned, so `@rf:` lives at line start.
    hits = parse_annotations(sym.docstring)
    rf_ids = {h.rf_id for h in hits}
    assert "RF-016" in rf_ids, f"matcher missed @rf in {sym.docstring!r}"


def test_ts_jsdoc_skips_banner_with_internal_text(tmp_path: Path):
    """Banner-style line comments with text wrapped in `---`/`===` runs
    are section dividers, not docstrings. They must be skipped so the
    JSDoc immediately underneath wins `docstring_lead`. Reproduces the
    real-world cases `// --- Token Management ---` and
    `// ============= Tool Execution Dispatcher =============`."""
    src = """// --- Token Management ---
/**
 * Resolve a token from cache.
 * @rf:BE-RF-016
 */
export async function getManyChatToken() { return 'x'; }

// ============= Tool Execution Dispatcher =============
/** @rf:BE-RF-001 */
export function dispatchTool() { return null; }
"""
    p = tmp_path / "main.ts"
    p.write_text(src, encoding="utf-8")
    _, result = extract(p, src, tmp_path)
    by_name = {s.name: s for s in result.symbols}
    a = by_name["getManyChatToken"]
    b = by_name["dispatchTool"]
    assert a.docstring is not None and "Token Management" not in a.docstring
    assert "Resolve a token" in a.docstring
    assert b.docstring is not None and "Tool Execution Dispatcher" not in b.docstring
    assert "BE-RF-001" in b.docstring
