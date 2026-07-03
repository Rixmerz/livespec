"""Feature E — index_project keeps the Spec Explorer bundle fresh.

`index_project` auto-regenerates the static Spec Explorer bundle when it
already exists (so it never goes stale after a re-index), and builds it on
demand via `explorer=True`. A bundle build failure must never break
indexing. The payload reports `explorer_regenerated`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _write_flask_app(workspace: Path) -> None:
    """A tiny Flask app with a decorated endpoint + a helper module."""
    pkg = workspace / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "routes.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/login', methods=['POST'])\n"
        "def login(user, password):\n"
        '    """Login handler."""\n'
        "    return verify(user, password)\n"
    )
    (pkg / "lib.py").write_text(
        '"""Auth helpers."""\n'
        "def verify(user, password):\n"
        "    return True\n"
    )


@pytest.mark.asyncio
async def test_existing_bundle_is_refreshed_on_reindex(workspace: Path):
    """Once a bundle exists, a plain re-index regenerates it automatically:
    explorer_regenerated is True and the files still exist."""
    _write_flask_app(workspace)
    explorer_dir = workspace / ".mcp-docs" / "explorer"
    data_path = explorer_dir / "data.json"
    html_path = explorer_dir / "index.html"

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Create the bundle once via the explorer tool.
        await c.call_tool("export_explorer", {})
        assert data_path.exists() and html_path.exists()

        # Re-run index_project WITHOUT the explorer flag and without editing
        # any source — the existing bundle must be refreshed regardless.
        result = (await c.call_tool("index_project", {})).data

    assert result["explorer_regenerated"] is True
    assert data_path.exists(), "data.json removed by re-index"
    assert html_path.exists(), "index.html removed by re-index"


@pytest.mark.asyncio
async def test_no_bundle_no_flag_does_not_create_one(workspace: Path):
    """Fresh workspace, no existing bundle, no explorer flag:
    explorer_regenerated is False and nothing is written."""
    _write_flask_app(workspace)
    explorer_dir = workspace / ".mcp-docs" / "explorer"

    async with Client(mcp) as c:
        result = (await c.call_tool("index_project", {})).data

    assert result["explorer_regenerated"] is False
    assert not explorer_dir.exists(), "bundle written despite no flag / no prior bundle"


@pytest.mark.asyncio
async def test_fastapi_workspace_autobuilds_explorer_on_first_index(workspace: Path):
    """FastAPI entry autodetect: first index_project builds the bundle without
    explorer=True (same path as export_explorer autowire)."""
    (workspace / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n"
    )
    explorer_dir = workspace / ".mcp-docs" / "explorer"
    data_path = explorer_dir / "data.json"
    html_path = explorer_dir / "index.html"

    async with Client(mcp) as c:
        result = (await c.call_tool("index_project", {})).data

    assert result["explorer_regenerated"] is True
    assert data_path.exists(), "FastAPI autodetect should create data.json"
    assert html_path.exists(), "FastAPI autodetect should create index.html"


@pytest.mark.asyncio
async def test_explorer_flag_creates_bundle(workspace: Path):
    """index_project(explorer=True) builds the bundle on demand:
    explorer_regenerated is True and both files land."""
    _write_flask_app(workspace)
    explorer_dir = workspace / ".mcp-docs" / "explorer"
    data_path = explorer_dir / "data.json"
    html_path = explorer_dir / "index.html"

    async with Client(mcp) as c:
        result = (await c.call_tool("index_project", {"explorer": True})).data

    assert result["explorer_regenerated"] is True
    assert data_path.exists(), "data.json not written with explorer=True"
    assert html_path.exists(), "index.html not written with explorer=True"
