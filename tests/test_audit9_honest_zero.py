"""Audit bug #9: a zero count must say whether it's a genuine zero or a
filter silently excluded the whole corpus ("I didn't sweep that").

Covers:
- `find_dead_code`: python-only sweep on a non-Python repo, and a
  public-only-symbols repo, must both surface `not_swept` + `hint`.
- `find_endpoints`: Hono is explicit opt-in (not part of the
  `framework=None` default sweep) — a zero must say so when Hono files
  are actually present.
- `git_diff_impact`: `summary_only=True` must not repeat the full
  changed-file lists three times over — bounded samples instead.
- Sanity: a GENUINE zero (nothing filtered) stays a plain zero, no
  `not_swept` noise attached.
"""

from __future__ import annotations

import subprocess

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_find_dead_code_ts_only_repo_auto_includes_non_python(workspace):
    """TS-only repos auto-enable include_non_python (silent Python-only zero
    was an audit false negative on a client Express hubs)."""
    (workspace / "src").mkdir()
    (workspace / "src" / "code.ts").write_text(
        "function deadFn() {\n  return 1;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        assert out["count"] >= 1
        assert "include_non_python" in (out.get("auto_enabled") or [])
        # Explicit False still forces the old Python-only path via... we can't
        # pass False to undo auto. Documented: zero-python ⇒ auto on.
        opted = (
            await c.call_tool("find_dead_code", {"include_non_python": True})
        ).data
        assert opted["count"] >= 1


@pytest.mark.asyncio
async def test_find_dead_code_public_only_reports_not_swept(workspace):
    """A repo whose only zero-caller candidates are `pub` (public/exported)
    symbols must say so instead of a bare zero."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "lib.rs").write_text(
        "pub fn never_called_but_public() -> i32 {\n    1\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "find_dead_code", {"include_non_python": True, "summary_only": True}
            )
        ).data
        assert out["count"] == 0
        assert "public" in out["not_swept"]
        assert "include_public=True" in out["hint"]


@pytest.mark.asyncio
async def test_find_dead_code_genuine_zero_has_no_not_swept(workspace):
    """A Python repo with real callers everywhere: count=0 is a real zero —
    no not_swept noise should be attached."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text(
        # Mutual recursion: each fn has an inbound edge from the other, so
        # neither is a zero-caller candidate — a real, fully-explained zero.
        "def a():\n    return b()\n\n\ndef b():\n    return a()\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {"summary_only": True})).data
        assert out["count"] == 0
        assert "not_swept" not in out
        assert "hint" not in out


HONO_APP = (
    "import { Hono } from 'hono';\n"
    "const app = new Hono();\n"
    "function listUsers(c: any) {\n  return c.json([]);\n}\n"
    "app.get('/users', listUsers);\n"
)


@pytest.mark.asyncio
async def test_find_endpoints_default_includes_hono(workspace):
    """Express/Hono call-style routes are part of the default sweep."""
    (workspace / "src").mkdir()
    (workspace / "src" / "app.ts").write_text(HONO_APP)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_endpoints", {"summary_only": True})
        ).data
        assert out["count"] >= 1
        assert "not_swept" not in out

        hono_out = (
            await c.call_tool("find_endpoints", {"framework": "hono"})
        ).data
        assert hono_out["count"] >= 1
        assert "not_swept" not in hono_out


@pytest.mark.asyncio
async def test_find_endpoints_genuine_zero_has_no_not_swept(workspace):
    """No framework markers anywhere — a real zero, no hint noise."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "code.py").write_text("def plain():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {"summary_only": True})).data
        assert out["count"] == 0
        assert "not_swept" not in out


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_git_diff_impact_summary_only_no_full_lists(workspace):
    """summary_only must not repeat the full changed-file lists 3x — only
    bounded samples + counts."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "a.py").write_text("def f():\n    return 1\n")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@t.local")
    _git(workspace, "config", "user.name", "t")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "base")
    (workspace / "pkg" / "a.py").write_text("def f():\n    return 2\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "change")

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "git_diff_impact",
                {"base_ref": "HEAD~1", "head_ref": "HEAD", "summary_only": True},
            )
        ).data
        assert "changed_files" not in out
        assert "changed_files_indexed" not in out
        assert "changed_files_unindexed" not in out
        assert "impacted_callers" not in out
        assert "changed_symbols" not in out
        assert "changed_files_sample" in out
        assert "counts" in out
