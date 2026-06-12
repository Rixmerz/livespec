"""Per-repo config via `.livespec.toml` (v0.14): extra ignore patterns
(outranking .gitignore), language allow-list, max_file_bytes override.
Absent file → defaults identical to pre-v0.14 behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.config import RepoConfig, load_repo_config
from livespec_mcp.domain.indexer import DEFAULT_IGNORES, _iter_files
from livespec_mcp.server import mcp


def _names(root: Path) -> set[str]:
    cfg = load_repo_config(root)
    return {p.relative_to(root).as_posix() for p in _iter_files(root, DEFAULT_IGNORES, cfg)}


def test_absent_file_yields_defaults(workspace: Path):
    cfg = load_repo_config(workspace)
    assert cfg == RepoConfig()
    assert cfg.languages is None
    assert cfg.max_file_bytes == 2_000_000


def test_extra_ignore_patterns(workspace: Path):
    (workspace / ".livespec.toml").write_text('[index]\nignore = ["assets/", "*.min.js"]\n')
    (workspace / "app.js").write_text("function app() {}\n")
    (workspace / "lib.min.js").write_text("function lib() {}\n")
    (workspace / "assets").mkdir()
    (workspace / "assets" / "vendor.js").write_text("function v() {}\n")
    assert _names(workspace) == {"app.js"}


def test_config_outranks_gitignore(workspace: Path):
    # .gitignore ignores gen.py; config re-includes it — config wins.
    (workspace / ".gitignore").write_text("gen.py\n")
    (workspace / ".livespec.toml").write_text('[index]\nignore = ["!gen.py"]\n')
    (workspace / "gen.py").write_text("def gen(): pass\n")
    assert _names(workspace) == {"gen.py"}


def test_language_filter(workspace: Path):
    (workspace / ".livespec.toml").write_text('[index]\nlanguages = ["python"]\n')
    (workspace / "a.py").write_text("def a(): pass\n")
    (workspace / "b.ts").write_text("function b() {}\n")
    assert _names(workspace) == {"a.py"}


def test_max_file_bytes(workspace: Path):
    (workspace / ".livespec.toml").write_text("[index]\nmax_file_bytes = 10\n")
    (workspace / "small.py").write_text("def s(): 1\n"[:10])
    (workspace / "big.py").write_text("def big(): pass  # padding padding\n")
    assert _names(workspace) == {"small.py"}


@pytest.mark.parametrize(
    "content,fragment",
    [
        ("[index]\nignore = 3\n", "list of strings"),
        ("[index]\nlanguages = ['klingon']\n", "unknown languages"),
        ("[index]\nmax_file_bytes = -5\n", "positive integer"),
        ("[index]\nbogus = true\n", "unknown [index] keys"),
        ("not toml ][", ".livespec.toml"),
    ],
)
def test_malformed_config_raises(workspace: Path, content: str, fragment: str):
    (workspace / ".livespec.toml").write_text(content)
    with pytest.raises(ValueError, match="Invalid"):
        try:
            load_repo_config(workspace)
        except ValueError as e:
            assert fragment in str(e)
            raise


@pytest.mark.asyncio
async def test_index_project_echoes_config(workspace: Path):
    (workspace / ".livespec.toml").write_text('[index]\nlanguages = ["python"]\n')
    (workspace / "core.py").write_text("def core(): pass\n")
    (workspace / "skip.ts").write_text("function skip() {}\n")
    async with Client(mcp) as c:
        data = (await c.call_tool("index_project", {})).data
        assert data["files_total"] == 1
        assert data["repo_config"]["languages"] == ["python"]


@pytest.mark.asyncio
async def test_index_project_no_config_echoes_none(workspace: Path):
    (workspace / "core.py").write_text("def core(): pass\n")
    async with Client(mcp) as c:
        data = (await c.call_tool("index_project", {})).data
        assert data["repo_config"] is None
