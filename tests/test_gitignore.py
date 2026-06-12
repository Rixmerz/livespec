"""Gitignore-aware indexing (v0.14): `_iter_files` honours `.gitignore`
files in the workspace — root and nested, including `!negations` — on top
of the hardcoded DEFAULT_IGNORES baseline. Non-git workspaces (no
.gitignore anywhere) keep the exact pre-v0.14 behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.indexer import DEFAULT_IGNORES, _iter_files
from livespec_mcp.server import mcp


def _names(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in _iter_files(root, DEFAULT_IGNORES)}


def test_no_gitignore_behaviour_unchanged(workspace: Path):
    (workspace / "a.py").write_text("def a(): pass\n")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.py").write_text("def b(): pass\n")
    assert _names(workspace) == {"a.py", "sub/b.py"}


def test_root_gitignore_dir_and_glob(workspace: Path):
    (workspace / ".gitignore").write_text("generated/\n*.gen.py\n")
    (workspace / "app.py").write_text("def app(): pass\n")
    (workspace / "models.gen.py").write_text("def gen(): pass\n")
    (workspace / "generated").mkdir()
    (workspace / "generated" / "stub.py").write_text("def stub(): pass\n")
    assert _names(workspace) == {"app.py"}


def test_nested_gitignore_scoped_to_subtree(workspace: Path):
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / ".gitignore").write_text("local.py\n")
    (workspace / "local.py").write_text("def top(): pass\n")
    (workspace / "pkg" / "local.py").write_text("def nested(): pass\n")
    (workspace / "pkg" / "real.py").write_text("def real(): pass\n")
    # pkg/.gitignore only applies under pkg/
    assert _names(workspace) == {"local.py", "pkg/real.py"}


def test_negation_within_one_file(workspace: Path):
    (workspace / ".gitignore").write_text("vendor/*\n!vendor/keep.py\n")
    (workspace / "vendor").mkdir()
    (workspace / "vendor" / "junk.py").write_text("def junk(): pass\n")
    (workspace / "vendor" / "keep.py").write_text("def keep(): pass\n")
    assert _names(workspace) == {"vendor/keep.py"}


def test_deeper_gitignore_overrides_parent(workspace: Path):
    (workspace / ".gitignore").write_text("secret.py\n")
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / ".gitignore").write_text("!secret.py\n")
    (workspace / "secret.py").write_text("def top(): pass\n")
    (workspace / "pkg" / "secret.py").write_text("def nested(): pass\n")
    assert _names(workspace) == {"pkg/secret.py"}


def test_ignored_dir_is_pruned_not_descended(workspace: Path):
    # A negation INSIDE an ignored dir cannot re-include (git semantics:
    # ignored dirs are not descended into).
    (workspace / ".gitignore").write_text("out/\n")
    (workspace / "out" / "deep").mkdir(parents=True)
    (workspace / "out" / "deep" / ".gitignore").write_text("!back.py\n")
    (workspace / "out" / "deep" / "back.py").write_text("def back(): pass\n")
    (workspace / "main.py").write_text("def main(): pass\n")
    assert _names(workspace) == {"main.py"}


@pytest.mark.asyncio
async def test_index_project_respects_gitignore(workspace: Path):
    (workspace / ".gitignore").write_text("scratch/\n")
    (workspace / "core.py").write_text("def core(): pass\n")
    (workspace / "scratch").mkdir()
    (workspace / "scratch" / "wip.py").write_text("def wip(): pass\n")
    async with Client(mcp) as c:
        data = (await c.call_tool("index_project", {})).data
        assert data["files_total"] == 1
        found = (await c.call_tool("find_symbol", {"query": "wip"})).data
        assert found["matches"] == []
