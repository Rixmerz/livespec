"""find_legacy_flows: server routes with no client hop (+ orphan clients)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.legacy_flows import compute_legacy_flows
from livespec_mcp.server import mcp
from livespec_mcp.state import reset_state


def _wire_group(root: Path, shared: Path) -> None:
    (root / ".livespec.toml").write_text(
        f'[workspace]\ngroup_db = "{shared}"\n', encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_find_legacy_flows_unmatched_server(tmp_path: Path):
    reset_state()
    shared = tmp_path / "group.db"
    back = tmp_path / "back"
    front = tmp_path / "front"
    back.mkdir()
    front.mkdir()
    _wire_group(back, shared)
    _wire_group(front, shared)
    (back / "api.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/search', methods=['POST'])\n"
        "def search():\n"
        "    return {}\n"
        "\n"
        "@app.route('/legacy', methods=['GET'])\n"
        "def legacy():\n"
        "    return {}\n"
        "\n"
        "@app.route('/health')\n"
        "def health():\n"
        "    return 'ok'\n"
    )
    (front / "client.py").write_text(
        "import requests\n"
        "\n"
        "def call_search():\n"
        "    return requests.post('/search')\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(back)})
        await c.call_tool("index_project", {"workspace": str(front)})
        out = (
            await c.call_tool(
                "find_legacy_flows",
                {"workspace": str(back), "summary_only": False},
            )
        ).data

    assert out["grouped"] is True
    paths = {
        (f.get("path") or f.get("norm_path"), f["flow_kind"])
        for f in out["flows"]
    }
    assert ("/legacy", "legacy_server") in paths or any(
        (f.get("norm_path") == "/legacy" or f.get("path") == "/legacy")
        and f["flow_kind"] == "legacy_server"
        for f in out["flows"]
    ), out["flows"]
    assert not any(
        (f.get("norm_path") == "/search" or f.get("path") == "/search")
        and f["flow_kind"] == "legacy_server"
        for f in out["flows"]
    ), out["flows"]
    # /health filtered by default
    assert not any(
        (f.get("norm_path") == "/health" or f.get("path") == "/health")
        for f in out["flows"]
    ), out["flows"]


@pytest.mark.asyncio
async def test_find_legacy_flows_summary_and_infra(tmp_path: Path):
    reset_state()
    shared = tmp_path / "group.db"
    back = tmp_path / "api"
    back.mkdir()
    _wire_group(back, shared)
    (back / "api.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.route('/health')\n"
        "def health():\n"
        "    return 'ok'\n"
        "@app.route('/old')\n"
        "def old():\n"
        "    return {}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(back)})
        summary = (
            await c.call_tool(
                "find_legacy_flows",
                {"workspace": str(back), "summary_only": True},
            )
        ).data
        with_infra = (
            await c.call_tool(
                "find_legacy_flows",
                {"workspace": str(back), "include_infra": True},
            )
        ).data
    assert summary["legacy_server_count"] >= 1
    assert "legacy_servers_sample" in summary
    assert "flows" not in summary
    assert any(
        f.get("norm_path") == "/health" or f.get("path") == "/health"
        for f in with_infra["flows"]
    )


def test_is_infra_route_path_docs_and_prefixes():
    from livespec_mcp.domain.legacy_flows import is_infra_route_path

    assert is_infra_route_path("/health")
    assert is_infra_route_path("/api-docs")
    assert is_infra_route_path("/v3/api-docs")
    assert is_infra_route_path("/ui")
    assert is_infra_route_path("/info")
    assert is_infra_route_path("/playground")
    assert is_infra_route_path("/metrics/cache")
    assert is_infra_route_path("/openapi.yaml")
    assert not is_infra_route_path("/search")
    assert not is_infra_route_path("/list/{}/{}/{}")


@pytest.mark.asyncio
async def test_find_legacy_flows_filters_api_docs(tmp_path: Path):
    reset_state()
    shared = tmp_path / "group.db"
    back = tmp_path / "back"
    back.mkdir()
    _wire_group(back, shared)
    (back / "api.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/search')\n"
        "def search():\n"
        "    return {}\n"
        "\n"
        "@app.route('/api-docs')\n"
        "def docs():\n"
        "    return {}\n"
        "\n"
        "@app.route('/ui')\n"
        "def ui():\n"
        "    return {}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(back)})
        out = (
            await c.call_tool(
                "find_legacy_flows",
                {"workspace": str(back), "include_infra": False},
            )
        ).data
        paths = {f.get("path") for f in out.get("flows") or []}
        assert "/api-docs" not in paths
        assert "/ui" not in paths
        assert "/search" in paths
    """Hand-built route_ref + edge — no full index."""
    from livespec_mcp.storage.db import connect, get_or_create_project

    db = tmp_path / "t.db"
    conn = connect(db)
    pid = get_or_create_project(conn, "solo", str(tmp_path))
    cur = conn.execute(
        "INSERT INTO file(project_id, path, language, content_hash, line_count, mtime) "
        "VALUES(?,?,?,?,?,?)",
        (pid, "a.py", "python", "x", 10, 1.0),
    )
    fid = int(cur.lastrowid)
    s_live = conn.execute(
        "INSERT INTO symbol(file_id, qualified_name, name, kind, start_line, end_line) "
        "VALUES(?,?,?,?,?,?)",
        (fid, "a.live", "live", "function", 1, 2),
    ).lastrowid
    s_dead = conn.execute(
        "INSERT INTO symbol(file_id, qualified_name, name, kind, start_line, end_line) "
        "VALUES(?,?,?,?,?,?)",
        (fid, "a.dead", "dead", "function", 3, 4),
    ).lastrowid
    s_client = conn.execute(
        "INSERT INTO symbol(file_id, qualified_name, name, kind, start_line, end_line) "
        "VALUES(?,?,?,?,?,?)",
        (fid, "a.client", "client", "function", 5, 6),
    ).lastrowid
    for sid, role, path in (
        (s_live, "server", "/live"),
        (s_dead, "server", "/dead"),
        (s_client, "client", "/live"),
    ):
        conn.execute(
            "INSERT INTO route_ref(symbol_id, role, method, path, norm_path, line) "
            "VALUES(?,?,?,?,?,?)",
            (sid, role, "GET", path, path, 1),
        )
    conn.execute(
        "INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight) "
        "VALUES(?,?,?,?)",
        (s_client, s_live, "invokes_route", 0.9),
    )
    conn.commit()
    out = compute_legacy_flows(conn)
    dead_paths = {r["norm_path"] for r in out["legacy_servers"]}
    assert "/dead" in dead_paths
    assert "/live" not in dead_paths
    conn.close()
