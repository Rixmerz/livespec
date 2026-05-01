"""Tests for git_diff_impact (P1.1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(sample_repo: Path) -> Path:
    """Initialize a git repo on sample_repo and create two commits so HEAD~1
    differs from HEAD. The diff touches pkg/auth.py."""
    _git(sample_repo, "init", "-q")
    _git(sample_repo, "config", "user.email", "test@example.com")
    _git(sample_repo, "config", "user.name", "test")
    _git(sample_repo, "add", ".")
    _git(sample_repo, "commit", "-q", "-m", "initial")
    # Mutate auth.py
    auth = sample_repo / "pkg" / "auth.py"
    auth.write_text(auth.read_text() + "\n\ndef extra_helper():\n    return 42\n")
    _git(sample_repo, "add", ".")
    _git(sample_repo, "commit", "-q", "-m", "add extra_helper")
    return sample_repo


@pytest.mark.asyncio
async def test_git_diff_impact_basic(git_repo):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "git_diff_impact",
                {"base_ref": "HEAD~1", "head_ref": "HEAD"},
            )
        ).data
        assert "pkg/auth.py" in result["changed_files"]
        assert "pkg/auth.py" in result["changed_files_indexed"]
        # Changed symbols include the new extra_helper at minimum
        names = {s["qualified_name"] for s in result["changed_symbols"]}
        assert any("extra_helper" in n for n in names) or any(
            "auth.login" in n or "auth.verify" in n for n in names
        )


@pytest.mark.asyncio
async def test_git_diff_impact_no_changes(git_repo):
    """Diffing HEAD against itself yields no impact."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "git_diff_impact",
                {"base_ref": "HEAD", "head_ref": "HEAD"},
            )
        ).data
        assert result["changed_files"] == []
        assert result["changed_symbols"] == []
        assert result["impacted_callers"] == []


@pytest.mark.asyncio
async def test_git_diff_impact_unknown_ref(git_repo):
    """Unknown ref returns isError=True with a short, agent-friendly message
    (P0.A2 v0.5: no `git diff --help` dump)."""
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "git_diff_impact",
                {"base_ref": "definitely-not-a-ref", "head_ref": "HEAD"},
            )
        ).data
        assert result.get("isError") is True
        # Must be short — no multi-line --help dump
        assert "\n" not in result["error"], (
            f"error must be a single line, got: {result['error']!r}"
        )
        assert len(result["error"]) < 250
        # Must mention the bad ref so the user knows what to fix
        assert "definitely-not-a-ref" in result["error"] or "unknown" in result["error"].lower()


@pytest.mark.asyncio
async def test_git_diff_impact_not_a_git_repo(workspace, sample_repo):
    """P0.A2 v0.5: workspace without git history -> short message, not the
    `git --help` dump we used to surface."""
    # sample_repo is on tmp without `git init`
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (await c.call_tool("git_diff_impact", {})).data
        assert result.get("isError") is True
        assert "\n" not in result["error"]
        assert "not a git repository" in result["error"].lower()


@pytest.mark.asyncio
async def test_git_diff_impact_excludes_test_fixtures(workspace):
    """v0.8 P2 fix #7: suggested_tests must filter out test fixtures and
    helper files. Only files matching test_*.py / *_test.{py,...} count
    as test runners."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def widget():\n"
        "    return 1\n"
    )

    tests = workspace / "tests"
    tests.mkdir()
    # Real test runner — should appear in suggested_tests
    (tests / "test_feature.py").write_text(
        "from pkg.feature import widget\n"
        "\n"
        "def test_widget():\n"
        "    assert widget() == 1\n"
    )
    # Fixture file under tests/fixtures/ — should NOT appear
    fixtures = tests / "fixtures"
    fixtures.mkdir()
    (fixtures / "data_widget.py").write_text(
        "from pkg.feature import widget\n"
        "\n"
        "def make_data():\n"
        "    return widget()\n"
    )
    # Helper file inside tests/ but not a test runner — should NOT appear
    (tests / "helpers.py").write_text(
        "from pkg.feature import widget\n"
        "\n"
        "def helper():\n"
        "    return widget()\n"
    )

    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "initial")
    # Touch feature.py so it's a "changed file" against HEAD~0/HEAD
    (pkg / "feature.py").write_text(
        (pkg / "feature.py").read_text() + "\ndef extra():\n    return 2\n"
    )
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "extra")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        result = (
            await c.call_tool(
                "git_diff_impact",
                {"base_ref": "HEAD~1", "head_ref": "HEAD"},
            )
        ).data

    assert "tests/test_feature.py" in result["suggested_tests"], (
        f"real test runner missing: {result['suggested_tests']}"
    )
    assert not any("fixtures/" in p for p in result["suggested_tests"]), (
        f"fixtures/ leaked into suggested_tests: {result['suggested_tests']}"
    )
    assert "tests/helpers.py" not in result["suggested_tests"], (
        f"helpers.py is not a test runner: {result['suggested_tests']}"
    )
