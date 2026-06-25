"""export_explorer: static RF Explorer bundle generation.

Asserts the tool writes data.json + index.html, the JSON schema is
complete, the 0-RF case still populates endpoints + coverage, and two
runs are byte-identical modulo meta.generated_at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

_TOP_KEYS = {"meta", "requirements", "rf_topology", "endpoints", "coverage"}


def _write_flask_app(workspace: Path) -> None:
    """A tiny Flask app (decorated endpoints) + a plain helper module."""
    pkg = workspace / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "routes.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/login', methods=['POST'])\n"
        "def login(user, password):\n"
        '    """Login handler.\n\n    @rf:RF-001\n    """\n'
        "    return verify(user, password)\n"
        "\n"
        "@app.route('/health')\n"
        "def health():\n"
        '    """Health check."""\n'
        "    return 'ok'\n"
    )
    (pkg / "lib.py").write_text(
        '"""Auth helpers."""\n'
        "def verify(user, password):\n"
        "    return True\n"
    )


@pytest.mark.asyncio
async def test_export_explorer_writes_both_files(workspace: Path):
    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("export_explorer", {})).data

    assert out["ok"] is True
    explorer_dir = workspace / ".mcp-docs" / "explorer"
    data_path = explorer_dir / "data.json"
    html_path = explorer_dir / "index.html"
    assert data_path.exists(), "data.json not written"
    assert html_path.exists(), "index.html not written"
    assert {str(data_path), str(html_path)} == set(out["files_written"])

    # index.html inlines the data and loads Mermaid from a single CDN script.
    html = html_path.read_text(encoding="utf-8")
    assert 'id="explorer-data"' in html
    assert "mermaid" in html.lower()


@pytest.mark.asyncio
async def test_export_explorer_data_schema(workspace: Path):
    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Make RF-001 real and link it, plus a second RF + a dependency edge.
        await c.call_tool("create_requirement", {"rf_id": "RF-001", "title": "Login"})
        await c.call_tool("create_requirement", {"rf_id": "RF-002", "title": "Auth lib"})
        await c.call_tool(
            "link_rf_symbol",
            {"rf_id": "RF-001", "symbol_qname": "app.routes.login"},
        )
        await c.call_tool(
            "link_rf_symbol",
            {"rf_id": "RF-002", "symbol_qname": "app.lib.verify"},
        )
        await c.call_tool(
            "link_rf_dependency",
            {"parent_rf_id": "RF-001", "child_rf_id": "RF-002"},
        )
        await c.call_tool("export_explorer", {})

    data = json.loads(
        (workspace / ".mcp-docs" / "explorer" / "data.json").read_text(encoding="utf-8")
    )
    assert set(data.keys()) == _TOP_KEYS

    # meta + counts shape
    assert set(data["meta"].keys()) == {"project", "generated_at", "counts"}
    assert set(data["meta"]["counts"].keys()) == {
        "requirements", "symbols", "endpoints", "files",
    }
    assert data["meta"]["counts"]["requirements"] == 2

    # RF-001 carries its implementing symbol (with signature), endpoint, dep.
    rf1 = next(r for r in data["requirements"] if r["id"] == "RF-001")
    assert any(s["qname"] == "app.routes.login" for s in rf1["symbols"])
    sym = next(s for s in rf1["symbols"] if s["qname"] == "app.routes.login")
    assert "signature" in sym and "file" in sym and "line" in sym
    assert "app.routes.login" in rf1["endpoints"]
    assert rf1["depends_on"] == ["RF-002"]
    assert rf1["coverage"] is not None

    # topology has the RF-001 -> RF-002 edge
    edges = {(e["from"], e["to"]) for e in data["rf_topology"]["edges"]}
    assert ("RF-001", "RF-002") in edges
    node_ids = {n["id"] for n in data["rf_topology"]["nodes"]}
    assert {"RF-001", "RF-002"} <= node_ids

    # endpoints surface includes the login route, tagged with RF-001
    handlers = {e["handler"] for e in data["endpoints"]}
    assert "app.routes.login" in handlers
    login_ep = next(e for e in data["endpoints"] if e["handler"] == "app.routes.login")
    assert "RF-001" in login_ep["rf_ids"]
    for ep in data["endpoints"]:
        assert set(ep.keys()) == {
            "framework", "handler", "signature", "path", "method", "rf_ids",
        }

    # coverage section shape
    assert set(data["coverage"].keys()) == {
        "orphan_modules", "orphan_endpoints", "totals",
    }


@pytest.mark.asyncio
async def test_export_explorer_zero_rf_case(workspace: Path):
    """No RFs: requirements==[] but endpoints + coverage still populated."""
    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("export_explorer", {})

    data = json.loads(
        (workspace / ".mcp-docs" / "explorer" / "data.json").read_text(encoding="utf-8")
    )
    assert data["requirements"] == []
    assert data["rf_topology"]["nodes"] == []
    assert data["rf_topology"]["edges"] == []
    assert data["meta"]["counts"]["requirements"] == 0

    # Endpoints + coverage are not gated on RFs existing.
    handlers = {e["handler"] for e in data["endpoints"]}
    assert "app.routes.login" in handlers
    assert "app.routes.health" in handlers
    # Every endpoint is orphan (no RF linked) in the 0-RF case.
    assert "app.routes.login" in data["coverage"]["orphan_endpoints"]
    assert data["meta"]["counts"]["endpoints"] >= 2
    assert isinstance(data["coverage"]["totals"], dict)
    assert data["coverage"]["totals"]


@pytest.mark.asyncio
async def test_export_explorer_idempotent(workspace: Path):
    """Two runs on an unchanged project yield identical data.json modulo
    meta.generated_at."""
    _write_flask_app(workspace)
    data_path = workspace / ".mcp-docs" / "explorer" / "data.json"
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_requirement", {"rf_id": "RF-001", "title": "Login"})
        await c.call_tool(
            "link_rf_symbol",
            {"rf_id": "RF-001", "symbol_qname": "app.routes.login"},
        )

        await c.call_tool("export_explorer", {"generated_at": "2026-01-01T00:00:00Z"})
        first = data_path.read_text(encoding="utf-8")
        await c.call_tool("export_explorer", {"generated_at": "2099-12-31T23:59:59Z"})
        second = data_path.read_text(encoding="utf-8")

    # Strip the only non-deterministic field and compare the rest verbatim.
    fd = json.loads(first)
    sd = json.loads(second)
    assert fd["meta"]["generated_at"] == "2026-01-01T00:00:00Z"
    assert sd["meta"]["generated_at"] == "2099-12-31T23:59:59Z"
    fd["meta"]["generated_at"] = None
    sd["meta"]["generated_at"] = None
    assert fd == sd

    # And with the default (None) timestamp, two runs are byte-identical.
    async with Client(mcp) as c:
        await c.call_tool("export_explorer", {})
        run_a = data_path.read_text(encoding="utf-8")
        await c.call_tool("export_explorer", {})
        run_b = data_path.read_text(encoding="utf-8")
    assert run_a == run_b
