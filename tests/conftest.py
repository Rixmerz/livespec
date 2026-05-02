"""Pytest fixtures: isolated workspace + fresh server state per test."""

from __future__ import annotations

import os
from pathlib import Path

# v0.8 P3.4/P3.5: force every plugin to register so tests see the full
# mutation surface (RF CRUD, doc generation). Plugins normally auto-load
# only when their backing tables have rows; tests on fresh workspaces
# would otherwise miss those tools. Set BEFORE any livespec import so
# the value is in os.environ when server.py runs its plugin auto-detect.
os.environ.setdefault("LIVESPEC_PLUGINS", "all")

import pytest

from livespec_mcp import state as state_module
from livespec_mcp.domain.graph import invalidate_graph_cache


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("LIVESPEC_WORKSPACE", str(tmp_path))
    state_module.reset_state()
    invalidate_graph_cache()
    yield tmp_path
    state_module.reset_state()
    invalidate_graph_cache()


@pytest.fixture
def sample_repo(workspace: Path) -> Path:
    """Tiny multi-file Python project with cross-file calls."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "pkg" / "auth.py").write_text(
        '"""Auth module."""\n'
        "def login(user, password):\n"
        '    """Login a user.\n\n    @rf:RF-001\n    """\n'
        "    return verify(user, password)\n"
        "\n"
        "def verify(user, password):\n"
        "    return True\n"
    )
    (workspace / "pkg" / "api.py").write_text(
        '"""API endpoints."""\n'
        "from pkg.auth import login\n"
        "\n"
        "class API:\n"
        "    def handle(self, req):\n"
        '        """Implements RF-002."""\n'
        "        return login(req['user'], req['pw'])\n"
    )
    return workspace
