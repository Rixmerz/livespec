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
def test_extractor_basic(
    fixture_path: Path, must_have_symbols: set[str], must_have_calls: set[str]
):
    source = fixture_path.read_text(encoding="utf-8")
    _, result = extract(fixture_path, source, fixture_path.parent)
    names = _names(result)
    missing = must_have_symbols - names
    assert not missing, f"Missing symbols in {fixture_path.name}: {missing}. Got: {names}"
    targets = _call_targets(result)
    missing_calls = must_have_calls - targets
    assert not missing_calls, (
        f"Missing calls in {fixture_path.name}: {missing_calls}. Got: {targets}"
    )


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
    as the symbol's docstring so the @spec: matcher can find tags. Covers the
    bug where TS symbols always had `docstring=None` even when an
    `@spec:Spec-NNN` annotation lived right above the function."""
    src = """/**
 * Fetches a token.
 * @spec:BE-SPEC-016
 */
export async function getManyChatToken() { return 'x'; }

/** @spec:auth-user-login, auth-session */
function helper() {}

// @spec:untested-feature
function lineCommented() {}
"""
    p = tmp_path / "main.ts"
    p.write_text(src, encoding="utf-8")
    _, result = extract(p, src, tmp_path)
    by_name = {s.name: s for s in result.symbols}
    assert "getManyChatToken" in by_name
    assert "@spec:BE-SPEC-016" in (by_name["getManyChatToken"].docstring or "")
    assert "@spec:auth-user-login" in (by_name["helper"].docstring or "")
    # // line comments are also kept so inline `@spec:` tags still match
    assert "@spec:untested-feature" in (by_name["lineCommented"].docstring or "")


def test_ts_jsdoc_wins_over_adjacent_separator_line_comment(tmp_path: Path):
    """Bug from real session: `// ---\n/** @spec:auth-user-login */\nfunction foo() {}`
    used to concatenate raw text and run a single strip pass keyed on the
    leading `//`, leaving the block's `/**` mid-text and defeating the
    matcher's line-start anchor for `@spec:`. Each comment must be stripped
    individually, and pure ASCII separator lines (`// ---`) must not be
    chosen as the docstring lead.
    """
    from livespec_mcp.domain.matcher import parse_annotations

    src = """// ---
// helper section
/**
 * Resolve a token from cache.
 * @spec:BE-SPEC-016
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
    # Block delimiters cleaned, so `@spec:` lives at line start.
    hits = parse_annotations(sym.docstring, prefixes=("BE-SPEC",))
    spec_ids = {h.spec_id for h in hits}
    assert "BE-SPEC-016" in spec_ids, f"matcher missed @spec in {sym.docstring!r}"


def test_ts_jsdoc_skips_banner_with_internal_text(tmp_path: Path):
    """Banner-style line comments with text wrapped in `---`/`===` runs
    are section dividers, not docstrings. They must be skipped so the
    JSDoc immediately underneath wins `docstring_lead`. Reproduces the
    real-world cases `// --- Token Management ---` and
    `// ============= Tool Execution Dispatcher =============`."""
    src = """// --- Token Management ---
/**
 * Resolve a token from cache.
 * @spec:BE-SPEC-016
 */
export async function getManyChatToken() { return 'x'; }

// ============= Tool Execution Dispatcher =============
/** @spec:BE-SPEC-001 */
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
    assert "BE-SPEC-001" in b.docstring


# ---------- const-bound functions and framework callbacks (Fresh 2) ----------

FRESH_CONST = FIXTURES / "typescript" / "fresh_const"


def _extract_fixture(rel: str):
    p = FRESH_CONST / rel
    _, result = extract(p, p.read_text(encoding="utf-8"), FRESH_CONST)
    return result


def _by_qname(result):
    return {s.qualified_name: s for s in result.symbols}


def test_ts_const_arrow_keeps_its_jsdoc():
    """`/** @spec:x */ const f = () => {}` (and `export const`): the comment
    sits above the declaration, not next to the arrow node the symbol spans.
    Looking for it beside the arrow made every such docstring None, so the
    @spec: annotation was silently ignored."""
    syms = _by_qname(_extract_fixture("main.ts"))
    assert "@spec:arrow-plain" in (syms["main.formatName"].docstring or "")
    assert "@spec:arrow-exported" in (syms["main.greet"].docstring or "")
    # `export const` is exported, like `export function` always was.
    assert syms["main.greet"].visibility == "exported"
    assert syms["main.formatName"].visibility is None


def test_ts_const_initialised_with_a_wrapping_call_is_a_function():
    """`export const session = define.middleware(async (ctx) => …)` produced
    no symbol at all — the initializer is a call, not a function. It is the
    function the framework calls, so it is one, named after the binding."""
    result = _extract_fixture("main.ts")
    syms = _by_qname(result)
    session = syms["main.session"]
    assert session.kind == "function"
    assert session.visibility == "exported"
    assert "@spec:auth-session" in (session.docstring or "")
    hdrs = syms["main.securityHeaders"]
    assert hdrs.kind == "function"
    assert "@spec:platform-headers" in (hdrs.docstring or "")
    # Calls inside the inline callback belong to the binding, not the module.
    owners = {(r.src_qname, r.target_name) for r in result.refs}
    assert ("main.session", "loadUser") in owners
    assert ("main.securityHeaders", "setHeaders") in owners
    assert ("main", "loadUser") not in owners
    # A call over plain values is not a function: no symbol.
    assert "main.LIMIT" not in syms
    assert "main.app" not in syms  # define.app([session, …]): no fn literal


def test_fresh_define_handlers_and_define_page():
    """`define.handlers({ GET() {…}, POST: handle(async …) })` and
    `export default define.page(function Health() {…})`."""
    result = _extract_fixture("routes/healthz.ts")
    syms = _by_qname(result)
    handler = syms["routes.healthz.handler"]
    assert handler.kind == "function"
    assert "@spec:platform-health" in (handler.docstring or "")
    # The method keeps the module-level qname it has in Fresh 1's plain
    # `handler = { GET() {} }` object, and its own JSDoc.
    get = syms["routes.healthz.GET"]
    assert get.kind == "method"
    assert "@spec:platform-health-get" in (get.docstring or "")
    page = syms["routes.healthz.Health"]
    assert page.kind == "function"
    assert "@spec:platform-health-page" in (page.docstring or "")
    owners = {(r.src_qname, r.target_name) for r in result.refs}
    assert ("routes.healthz.GET", "getSettings") in owners  # not double-owned
    assert ("routes.healthz.handler", "getSettings") not in owners
    assert ("routes.healthz.handler", "createThing") in owners  # inline POST
    assert ("routes.healthz.Health", "renderHealth") in owners


def test_ts_object_literal_methods_are_qualified_by_their_name_property():
    """`TOOLS = [{ name: "assign_ticket", run() {} }, …]`: every `run` got the
    same qname. The object's `name` string disambiguates deterministically;
    an object without one keeps the plain qname."""
    syms = _by_qname(_extract_fixture("main.ts"))
    assert syms["main.assign_ticket.run"].name == "run"
    assert syms["main.get_capacity.run"].name == "run"
    assert "main.run" in syms  # the anonymous third entry
    runs = [s for s in _extract_fixture("main.ts").symbols if s.name == "run"]
    assert len({s.qualified_name for s in runs}) == 3


def test_ts_const_symbol_body_hash_ignores_comment_and_format(tmp_path: Path):
    """The new symbols keep body_hash invariance: a JSDoc edit or reformat
    does not drift the seed; a real change does."""
    a = "/** @spec:a */\nexport const s = define.middleware(async (ctx) => { return ctx.next(); });\n"
    b = (
        "/**\n * Changed comment.\n * @spec:b\n */\nexport const s = define.middleware(\n"
        "  async (ctx) => {\n    return ctx.next();\n  },\n);\n"
    )
    c = "export const s = define.middleware(async (ctx) => { return ctx.other(); });\n"

    def seed(src: str) -> str:
        p = tmp_path / "m.ts"
        p.write_text(src, encoding="utf-8")
        _, r = extract(p, src, tmp_path)
        return next(x.body_hash_seed for x in r.symbols if x.name == "s")

    assert seed(a) == seed(b)
    assert seed(a) != seed(c)


def test_ts_wrapped_call_inside_a_function_is_not_a_symbol(tmp_path: Path):
    """Module level only: inside a body, `useMemo(() => …)` is a value."""
    src = (
        "export function Comp() {\n"
        "  const total = useMemo(() => sum(items), [items]);\n"
        "  return total;\n"
        "}\n"
    )
    p = tmp_path / "c.tsx"
    p.write_text(src, encoding="utf-8")
    _, r = extract(p, src, tmp_path)
    assert "total" not in {s.name for s in r.symbols}
    assert ("c.Comp", "sum") in {(x.src_qname, x.target_name) for x in r.refs}


@pytest.mark.asyncio
async def test_spec_annotation_on_a_const_binding_links_end_to_end(workspace: Path):
    """The point of the docstring fix: `@spec:` above `export const x =
    define.middleware(…)` must reach `get_spec_implementation`."""
    from fastmcp import Client

    from livespec_mcp.server import mcp

    (workspace / "main.ts").write_text(
        (FRESH_CONST / "main.ts").read_text(encoding="utf-8"), encoding="utf-8"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        for sid in ("auth-session", "arrow-exported"):
            await c.call_tool("create_spec", {"title": sid, "spec_id": sid})
        scan = (await c.call_tool("scan_spec_annotations", {})).data
        assert scan["links_created"] >= 2
        for sid, qname in (("auth-session", "main.session"), ("arrow-exported", "main.greet")):
            impl = (await c.call_tool("get_spec_implementation", {"spec_id": sid})).data
            assert qname in {s["qualified_name"] for s in impl["symbols"]}, (sid, impl)
