"""P0.D2 v0.5: body_hash should not drift on reformat-only edits.

Python uses ast.dump (already stable across whitespace/comments).
Tree-sitter languages (JS/TS/Go/Java/Rust/Ruby/PHP) use the raw source slice;
we normalize internal whitespace + blank lines so an autoformat run on an
unchanged-logic function does not look like a body change.

A real semantic change (different literal, different identifier, different
operator) MUST still produce a different hash.
"""

from __future__ import annotations

from pathlib import Path

import xxhash

from livespec_mcp.domain.extractors import extract


def _body_hash(source: str, fixture_path: Path) -> dict[str, str]:
    """Index `source` as `fixture_path` and return {qname: body_hash} map."""
    fixture_path.write_text(source, encoding="utf-8")
    _, result = extract(fixture_path, source, fixture_path.parent)
    return {
        s.qualified_name: xxhash.xxh3_128_hexdigest(
            s.body_hash_seed.encode("utf-8", errors="replace")
        )
        for s in result.symbols
    }


def test_python_body_hash_stable_against_whitespace(tmp_path: Path):
    fixture = tmp_path / "mod.py"

    a = "def f(x):\n    y = x + 1\n    return y\n"
    b = "def f(x):\n\n    y = x + 1\n\n    return y\n"  # blank lines
    c = "def f(x):\n    y =  x  +  1\n    return  y\n"  # extra spaces
    d = "def f(x):\n    # leading comment\n    y = x + 1  # trailing\n    return y\n"

    h_a = _body_hash(a, fixture)
    h_b = _body_hash(b, fixture)
    h_c = _body_hash(c, fixture)
    h_d = _body_hash(d, fixture)

    assert h_a == h_b == h_c == h_d, (
        f"Python body_hash should be whitespace/comment-stable: {h_a, h_b, h_c, h_d}"
    )

    # Real change must drift
    e = "def f(x):\n    y = x + 2\n    return y\n"
    h_e = _body_hash(e, fixture)
    assert h_e != h_a


def test_typescript_body_hash_stable_against_reformat(tmp_path: Path):
    fixture = tmp_path / "mod.ts"

    a = "function f(x: number): number {\n  return x * 2;\n}\n"
    b = "function f(x: number): number {\n\n  return x * 2;\n\n}\n"
    c = "function   f(x: number)  :  number   {\n    return   x   *   2;\n}\n"
    d_with_comment = "function f(x: number): number {\n  // comment added\n  return x * 2;\n}\n"

    h_a = _body_hash(a, fixture)
    h_b = _body_hash(b, fixture)
    h_c = _body_hash(c, fixture)
    h_d = _body_hash(d_with_comment, fixture)

    assert h_a == h_b == h_c == h_d, (
        f"TS body_hash should be whitespace+comment-stable post P0.D2: {h_a, h_b, h_c, h_d}"
    )

    real_change = "function f(x: number): number {\n  return x * 3;\n}\n"
    assert _body_hash(real_change, fixture) != h_a


def test_go_body_hash_stable_against_reformat(tmp_path: Path):
    fixture = tmp_path / "mod.go"

    a = "package mod\n\nfunc F(x int) int {\n\treturn x + 1\n}\n"
    b = "package mod\n\nfunc F(x int) int {\n\n\treturn x + 1\n\n}\n"
    c = "package mod\n\nfunc F(x int) int {\n    return x + 1\n}\n"  # spaces vs tabs
    d_with_comment = "package mod\n\nfunc F(x int) int {\n\t// added\n\treturn x + 1\n}\n"

    h_a = _body_hash(a, fixture)
    h_b = _body_hash(b, fixture)
    h_c = _body_hash(c, fixture)
    h_d = _body_hash(d_with_comment, fixture)

    assert h_a == h_b == h_c == h_d, (
        f"Go body_hash should be whitespace+comment-stable post P0.D2: {h_a, h_b, h_c, h_d}"
    )

    real_change = "package mod\n\nfunc F(x int) int {\n\treturn x + 2\n}\n"
    assert _body_hash(real_change, fixture) != h_a
