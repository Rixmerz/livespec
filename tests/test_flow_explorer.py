"""export_flow_explorer: cross-repo Flow Explorer over group_db."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp
from livespec_mcp.state import reset_state


@pytest.mark.asyncio
async def test_export_flow_explorer_group(tmp_path: Path):
    reset_state()
    db = tmp_path / ".livespec-group" / "flow.db"
    (tmp_path / ".livespec-group").mkdir()
    a = tmp_path / "hub"
    b = tmp_path / "composer"
    for root, body in (
        (
            a,
            "from flask import Flask\napp=Flask(__name__)\n"
            "@app.route('/graphql', methods=['POST'])\n"
            "def graphql():\n"
            "    '''@spec:xrepo-search-orchestrate'''\n"
            "    return {}\n",
        ),
        (
            b,
            "from flask import Flask\napp=Flask(__name__)\n"
            "@app.route('/search', methods=['POST'])\n"
            "def search():\n"
            "    '''@spec:xrepo-search-orchestrate'''\n"
            "    return {}\n",
        ),
    ):
        root.mkdir()
        (root / "app.py").write_text(body)
        (root / ".livespec.toml").write_text(
            f'[workspace]\ngroup_db = "{db}"\n',
            encoding="utf-8",
        )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(a)})
        await c.call_tool("index_project", {"workspace": str(b)})
        out = (
            await c.call_tool(
                "export_flow_explorer", {"workspace": str(a)}
            )
        ).data

    assert out["ok"] is True
    assert out["counts"]["projects"] == 2
    html = Path(out["files_written"][1])
    data = Path(out["files_written"][0])
    assert html.is_file() and data.is_file()
    assert html.parent.name == "flow-explorer"
    assert html.parent == db.parent / "flow-explorer"
    bundle = json.loads(data.read_text())
    assert bundle["meta"]["kind"] == "flow"
    assert len(bundle["projects"]) == 2
    # Annotations alone do not create Spec rows; xrepo list may be empty in
    # this fixture. The results polyrepo seeds Specs via OpenSpec/links_seed.
    assert "xrepo_specs" in bundle
    assert bundle["meta"]["counts"]["endpoints"] >= 1
    if shutil.which("node"):
        text = html.read_text(encoding="utf-8")
        js = (
            text.split('<script id="flow-data" type="application/json">', 1)[1]
            .split("</script>", 1)[1]
            .split("<script>", 1)[1]
            .rsplit("</script>", 1)[0]
        )
        js_path = html.parent / "_check.js"
        js_path.write_text(js, encoding="utf-8")
        check = subprocess.run(
            ["node", "--check", str(js_path)], capture_output=True, text=True
        )
        js_path.unlink(missing_ok=True)
        assert check.returncode == 0, check.stderr


@pytest.mark.asyncio
async def test_export_flow_explorer_solo(tmp_path: Path):
    reset_state()
    root = tmp_path / "solo"
    root.mkdir()
    (root / "m.py").write_text("def f():\n    return 1\n")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(root)})
        out = (
            await c.call_tool(
                "export_flow_explorer", {"workspace": str(root)}
            )
        ).data
    assert out["ok"] is True
    assert str(root / ".mcp-docs" / "flow-explorer") in out["out_dir"]
