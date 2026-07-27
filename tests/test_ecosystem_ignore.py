"""Bug #12: deno.json / deno.jsonc / tsconfig.json `exclude` arrays fold into
RepoConfig.ignore so the indexer skips declared build output even without a
matching .gitignore entry — no changes needed in domain/indexer.py, which
already routes RepoConfig.ignore through a GitIgnoreSpec ranked above
.gitignore.
"""

from __future__ import annotations

from pathlib import Path

from livespec_mcp.config import load_repo_config
from livespec_mcp.domain.indexer import DEFAULT_IGNORES, _iter_files


def _names(root: Path) -> set[str]:
    cfg = load_repo_config(root)
    return {p.relative_to(root).as_posix() for p in _iter_files(root, DEFAULT_IGNORES, cfg)}


def _write_source_and_build_output(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "_fresh" / "client" / "assets").mkdir(parents=True)
    (root / "_fresh" / "client" / "assets" / "bundle.js").write_text(
        "function a(){}", encoding="utf-8"
    )


def test_deno_json_exclude_keeps_build_output_out(tmp_path: Path):
    _write_source_and_build_output(tmp_path)
    (tmp_path / "deno.json").write_text(
        '{"exclude": ["**/_fresh/*", "mcp-app/**", "openspec/**"]}', encoding="utf-8"
    )
    names = _names(tmp_path)
    assert "src/main.py" in names
    assert not any("_fresh" in n for n in names)


def test_deno_jsonc_comments_and_trailing_commas_tolerated(tmp_path: Path):
    _write_source_and_build_output(tmp_path)
    (tmp_path / "deno.jsonc").write_text(
        "{\n"
        '  // build output, not source\n'
        '  "exclude": [\n'
        '    "**/_fresh/*", // Fresh build dir\n'
        "  ],\n"
        "}\n",
        encoding="utf-8",
    )
    names = _names(tmp_path)
    assert not any("_fresh" in n for n in names)


def test_deno_json_wins_over_deno_jsonc_when_both_exist(tmp_path: Path):
    """Matches Deno's own resolution order: deno.json shadows deno.jsonc."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "only_jsonc").mkdir()
    (tmp_path / "only_jsonc" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "deno.json").write_text('{"exclude": []}', encoding="utf-8")
    (tmp_path / "deno.jsonc").write_text('{"exclude": ["only_jsonc/**"]}', encoding="utf-8")
    names = _names(tmp_path)
    # deno.json (empty exclude) wins -> only_jsonc/x.py is NOT excluded.
    assert "only_jsonc/x.py" in names


def test_tsconfig_exclude_bare_name_and_trailing_slash(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.py").write_text("y = 1\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").write_text("z = 1\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"exclude": ["dist", "build/"]}', encoding="utf-8"
    )
    names = _names(tmp_path)
    assert "src/main.py" in names
    assert not any(n.startswith("dist/") for n in names)
    assert not any(n.startswith("build/") for n in names)


def test_leading_dot_slash_normalized(tmp_path: Path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "f.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text('{"exclude": ["./out"]}', encoding="utf-8")
    names = _names(tmp_path)
    assert not any(n.startswith("out/") for n in names)


def test_livespec_toml_ignore_wins_over_deno_exclude(tmp_path: Path):
    """.livespec.toml's [index].ignore is the explicit per-repo override —
    a `!re-include` there must beat an auto-discovered deno.json exclude.
    (Re-include target kept at workspace-root depth: a pattern can't
    resurrect a path inside an already-pruned directory — same limitation
    git itself documents, and pre-existing in this codebase's dir-walk.)"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "keep.gen.py").write_text("k = 1\n", encoding="utf-8")
    (tmp_path / "drop.gen.py").write_text("d = 1\n", encoding="utf-8")
    (tmp_path / "deno.json").write_text('{"exclude": ["*.gen.py"]}', encoding="utf-8")
    (tmp_path / ".livespec.toml").write_text(
        '[index]\nignore = ["!keep.gen.py"]\n', encoding="utf-8"
    )
    names = _names(tmp_path)
    assert "keep.gen.py" in names
    assert "drop.gen.py" not in names


def test_missing_or_malformed_ecosystem_config_never_breaks_indexing(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "deno.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("null", encoding="utf-8")
    names = _names(tmp_path)
    assert "src/main.py" in names


def test_no_ecosystem_files_is_a_pure_noop(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    cfg = load_repo_config(tmp_path)
    assert cfg.ignore == ()
