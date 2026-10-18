"""`livespec ui`: the user-level registry, the read-only server, and the removal
of the in-repo explorers it replaces.

Fixture: two repos sharing one `[workspace] group_db` (a polyrepo group) plus
one standalone repo with an OpenSpec tree, all indexed through the real MCP
`index_project` tool — which is what writes the registry entries.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp import registry
from livespec_mcp.server import mcp
from livespec_mcp.ui import data as ui_data
from livespec_mcp.ui.server import make_server, serve_in_thread, server_url

OPENSPEC = """# Billing Specification

## Purpose

Charging customers.

## Requirements

### Requirement: Charge a card

The system SHALL charge a customer's card for an order.

#### Scenario: Successful charge

- **WHEN** a valid card is charged
- **THEN** a receipt is returned
"""


def _repo(root: Path, module: str, body: str, *, group_db: Path | None = None) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / f"{module}.py").write_text(body)
    if group_db is not None:
        (root / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{group_db}"\n')
    return root


def _call(result):
    return result.data if hasattr(result, "data") else result.structured_content


@pytest.fixture
async def indexed(tmp_path: Path):
    """back + front in one group DB, billing standalone. Returns their paths."""
    shared = tmp_path / "grp" / "group.db"
    back = _repo(
        tmp_path / "back",
        "api",
        "def handler(x):\n    return helper(x)\n\ndef helper(x):\n    return x\n",
        group_db=shared,
    )
    front = _repo(
        tmp_path / "front",
        "ui",
        "def caller(x):\n    return x\n",
        group_db=shared,
    )
    billing = _repo(
        tmp_path / "billing",
        "pay",
        "def charge(card):\n    return validate(card)\n\ndef validate(card):\n    return True\n",
    )
    (billing / "openspec" / "specs" / "billing").mkdir(parents=True)
    (billing / "openspec" / "specs" / "billing" / "spec.md").write_text(OPENSPEC)

    async with Client(mcp) as c:
        for ws in (back, front, billing):
            out = _call(await c.call_tool("index_project", {"workspace": str(ws)}))
            assert "error" not in out, out
        for ws in (back, front):
            await c.call_tool(
                "create_spec",
                {"workspace": str(ws), "spec_id": "xrepo-login", "title": "Login flow"},
            )
        await c.call_tool(
            "link_spec_symbol",
            {"workspace": str(back), "spec_id": "xrepo-login", "symbol_qname": "pkg.api.handler"},
        )
        await c.call_tool(
            "link_spec_symbol",
            {"workspace": str(front), "spec_id": "xrepo-login", "symbol_qname": "pkg.ui.caller"},
        )
        await c.call_tool("sync_openspec", {"workspace": str(billing)})
        specs = _call(await c.call_tool("list_specs", {"workspace": str(billing)}))
        billing_spec = specs["specs"][0]["spec_id"]
        await c.call_tool(
            "link_spec_symbol",
            {"workspace": str(billing), "spec_id": billing_spec, "symbol_qname": "pkg.pay.charge"},
        )
    # Release every cached read-write connection so the files on disk are the
    # steady state the read-only server will see.
    from livespec_mcp import state as state_module

    state_module.reset_state()
    return {
        "back": back.resolve(),
        "front": front.resolve(),
        "billing": billing.resolve(),
        "group_db": shared.resolve(),
        "billing_spec": billing_spec,
    }


@pytest.fixture
def server():
    srv = make_server("127.0.0.1", 0, quiet=True)
    serve_in_thread(srv)
    try:
        yield server_url(srv)
    finally:
        srv.shutdown()
        srv.server_close()


def _get(base: str, path: str, *, headers: dict[str, str] | None = None):
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _json(base: str, path: str):
    status, ctype, body = _get(base, path)
    assert status == 200, (path, status, body[:300])
    assert ctype.startswith("application/json")
    return json.loads(body)


def _post(base: str, path: str, payload: dict, *, header: bool = True):
    headers = {"Content-Type": "application/json"}
    if header:
        headers["X-Livespec-UI"] = "1"
    req = urllib.request.Request(
        base.rstrip("/") + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_registry_home_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVESPEC_HOME", str(tmp_path / "lh"))
    assert registry.registry_path() == (tmp_path / "lh" / "registry.json").resolve()
    monkeypatch.delenv("LIVESPEC_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert registry.registry_path() == (tmp_path / "xdg" / "livespec" / "registry.json").resolve()
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert registry.registry_path() == (tmp_path / "home" / ".livespec" / "registry.json").resolve()


def test_registry_write_read_forget(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    db = ws / ".mcp-docs" / "docs.db"
    entry = registry.record_workspace(
        ws, db, grouped=False, languages={"python": 2}, counts={"symbols": 3}
    )
    assert entry["path"] == str(ws.resolve())
    assert entry["group"] is None
    raw = json.loads(registry.registry_path().read_text())
    assert raw["version"] == registry.REGISTRY_VERSION
    assert raw["projects"][str(ws.resolve())]["languages"] == {"python": 2}
    # The registry is outside the repository.
    assert not str(registry.registry_path()).startswith(str(ws.resolve()))

    # No DB on disk → reported missing, never dropped silently.
    [listed] = registry.list_projects()
    assert listed["status"] == "missing_db"
    assert registry.list_projects()[0]["path"] == str(ws.resolve())

    assert registry.forget(str(ws)) is True
    assert registry.list_projects() == []
    assert registry.forget(str(ws)) is False


def test_unreadable_registry_reads_as_empty():
    registry.registry_path().parent.mkdir(parents=True, exist_ok=True)
    registry.registry_path().write_text("{not json")
    assert registry.load_registry() == {"version": 1, "projects": {}}


@pytest.mark.asyncio
async def test_index_project_records_every_workspace(indexed):
    entries = {e["path"]: e for e in registry.list_projects()}
    assert set(entries) == {str(indexed[k]) for k in ("back", "front", "billing")}
    assert entries[str(indexed["back"])]["group"] == str(indexed["group_db"])
    assert entries[str(indexed["front"])]["group"] == str(indexed["group_db"])
    assert entries[str(indexed["billing"])]["group"] is None
    billing = entries[str(indexed["billing"])]
    assert billing["status"] == "ok"
    assert billing["languages"] == {"python": 2}
    assert billing["counts"]["symbols"] >= 2
    assert billing["last_indexed"]


def test_cli_index_records_and_registry_failure_never_breaks_indexing(
    sample_repo, monkeypatch, tmp_path
):
    from livespec_mcp import cli

    assert cli.main(["index", str(sample_repo)]) == 0
    assert [e["path"] for e in registry.list_projects()] == [str(sample_repo.resolve())]

    # A registry home that cannot be created (a file in the way) must not cost
    # the user their index run.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("LIVESPEC_HOME", str(blocker / "sub"))
    from livespec_mcp import state as state_module

    state_module.reset_state()
    assert cli.main(["index", str(sample_repo), "--force"]) == 0


def test_missing_db_is_shown_not_pruned(indexed):
    db = indexed["billing"] / ".mcp-docs" / "docs.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    by_path = {e["path"]: e for e in registry.list_projects()}
    assert by_path[str(indexed["billing"])]["status"] == "missing_db"
    home = ui_data.home()
    p = next(x for x in home["projects"] if x["path"] == str(indexed["billing"]))
    assert p["status"] == "missing_db"
    assert p["counts"]["symbols"] >= 2  # last known snapshot still shown


def test_cli_ui_add_registers_an_existing_index(indexed, capsys):
    from livespec_mcp import cli

    registry.forget(str(indexed["billing"]))
    assert str(indexed["billing"]) not in {e["path"] for e in registry.list_projects()}
    assert cli.main(["ui", "--add", str(indexed["billing"]), "--list"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert str(indexed["billing"]) in {e["path"] for e in out["projects"]}


def test_cli_ui_add_refuses_an_unindexed_folder(tmp_path, capsys):
    from livespec_mcp import cli

    empty = tmp_path / "nothing"
    empty.mkdir()
    assert cli.main(["ui", "--add", str(empty), "--list"]) == 1
    assert "no livespec index" in capsys.readouterr().err
    assert not (empty / ".mcp-docs").exists()


# --------------------------------------------------------------------------
# Server: JSON endpoints + HTML routes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_groups_projects_by_group_db(indexed, server):
    home = _json(server, "/api/projects")
    by_path = {p["path"]: p for p in home["projects"]}
    assert set(by_path) == {str(indexed[k]) for k in ("back", "front", "billing")}
    [group] = home["groups"]
    assert group["db_path"] == str(indexed["group_db"])
    assert set(group["projects"]) == {by_path[str(indexed[k])]["id"] for k in ("back", "front")}
    assert home["ungrouped"] == [by_path[str(indexed["billing"])]["id"]]
    assert by_path[str(indexed["billing"])]["languages"] == {"python": 2}


@pytest.mark.asyncio
async def test_project_endpoints(indexed, server):
    pid = ui_data.stable_id(str(indexed["billing"]))
    ov = _json(server, f"/api/p/{pid}")
    assert ov["name"] == "billing"
    assert ov["counts"]["specs"] == 1

    cov = _json(server, f"/api/p/{pid}/coverage")
    assert cov["specs"] == 1
    assert cov["implemented_pct"] == 100.0

    specs = _json(server, f"/api/p/{pid}/specs")
    [cap] = specs["capabilities"]
    assert cap["name"] == "billing"
    [spec] = cap["specs"]
    assert spec["spec_id"] == indexed["billing_spec"]
    assert spec["links"] == 1 and spec["scenarios"] == 1

    detail = _json(server, f"/api/p/{pid}/specs/{indexed['billing_spec']}")
    assert detail["scenarios"][0]["name"] == "Successful charge"
    [sym] = detail["symbols"]
    assert (sym["qname"], sym["file"], sym["line"]) == ("pkg.pay.charge", "pkg/pay.py", 1)
    assert detail["coverage"]["implementing_symbols"] == 1

    found = _json(server, f"/api/p/{pid}/symbols?q=valid")
    assert [r["qname"] for r in found["results"]] == ["pkg.pay.validate"]

    nb = _json(server, f"/api/p/{pid}/symbol?qname=pkg.pay.validate")
    assert [c["qname"] for c in nb["callers"]] == ["pkg.pay.charge"]
    nb2 = _json(server, f"/api/p/{pid}/symbol?qname=pkg.pay.charge")
    assert [c["qname"] for c in nb2["callees"]] == ["pkg.pay.validate"]
    assert [s["spec_id"] for s in nb2["specs"]] == [indexed["billing_spec"]]

    eps = _json(server, f"/api/p/{pid}/endpoints")
    assert eps["total"] == len(eps["endpoints"])


@pytest.mark.asyncio
async def test_group_view_shows_cross_repo_specs(indexed, server):
    gid = ui_data.stable_id(str(indexed["group_db"]))
    g = _json(server, f"/api/g/{gid}")
    assert {p["name"] for p in g["projects"]} == {"back", "front"}
    [shared] = g["shared_specs"]
    assert shared["spec_id"] == "xrepo-login"
    assert {(p["project"], p["links"]) for p in shared["projects"]} == {("back", 1), ("front", 1)}

    # A group member's spec detail points at its mirror in the other repo.
    back_id = ui_data.stable_id(str(indexed["back"]))
    detail = _json(server, f"/api/p/{back_id}/specs/xrepo-login")
    assert [m["project"] for m in detail["mirrors"]] == ["front"]


@pytest.mark.asyncio
async def test_html_routes_and_static_assets(indexed, server):
    pid = ui_data.stable_id(str(indexed["billing"]))
    gid = ui_data.stable_id(str(indexed["group_db"]))
    for path in ("/", f"/p/{pid}", f"/p/{pid}/symbols", f"/p/{pid}/spec/x", f"/g/{gid}"):
        status, ctype, body = _get(server, path)
        assert status == 200, path
        assert ctype.startswith("text/html")
        assert b'<main id="main"' in body and b'lang="en"' in body
    status, ctype, body = _get(server, "/static/app.js")
    assert status == 200 and "javascript" in ctype
    assert b"innerHTML" not in body  # index data is never parsed as HTML
    status, ctype, _ = _get(server, "/static/app.css")
    assert status == 200 and ctype.startswith("text/css")
    status, _, body = _get(server, "/")
    assert b"prefers-color-scheme" not in body  # theming lives in the CSS
    status, _, css = _get(server, "/static/app.css")
    assert b"prefers-color-scheme: dark" in css


@pytest.mark.asyncio
async def test_errors_and_guards(indexed, server):
    assert _get(server, "/api/p/nope")[0] == 404
    assert _get(server, "/api/g/nope")[0] == 404
    pid = ui_data.stable_id(str(indexed["billing"]))
    assert _get(server, f"/api/p/{pid}/specs/missing")[0] == 404
    assert _get(server, f"/api/p/{pid}/symbol?qname=missing")[0] == 404
    assert _get(server, "/static/../../etc/passwd")[0] == 404
    assert _get(server, "/nowhere")[0] == 404
    # DNS-rebinding guard: a foreign Host is refused.
    assert _get(server, "/api/projects", headers={"Host": "evil.example:80"})[0] == 403
    # The one write needs the custom header a cross-site form cannot send.
    status, _ = _post(server, "/api/forget", {"path": str(indexed["billing"])}, header=False)
    assert status == 403
    assert str(indexed["billing"]) in {e["path"] for e in registry.list_projects()}


@pytest.mark.asyncio
async def test_forget_action_removes_only_the_registry_entry(indexed, server):
    status, body = _post(server, "/api/forget", {"path": str(indexed["billing"])})
    assert status == 200 and body["forgotten"] == str(indexed["billing"])
    assert str(indexed["billing"]) not in {e["path"] for e in registry.list_projects()}
    assert (indexed["billing"] / ".mcp-docs" / "docs.db").is_file()  # nothing deleted on disk
    status, _ = _post(server, "/api/forget", {"path": str(indexed["billing"])})
    assert status == 404


# --------------------------------------------------------------------------
# Read-only guarantee
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serving_never_writes_into_any_project(indexed, server):
    roots = [indexed["back"], indexed["front"], indexed["billing"], indexed["group_db"].parent]
    before = {str(r): _tree(r) for r in roots}

    home = _json(server, "/api/projects")
    for p in home["projects"]:
        pid = p["id"]
        for path in ("", "/coverage", "/specs", "/endpoints", "/symbols?q=a"):
            _json(server, f"/api/p/{pid}{path}")
        for spec in (
            s for cap in _json(server, f"/api/p/{pid}/specs")["capabilities"] for s in cap["specs"]
        ):
            _json(server, f"/api/p/{pid}/specs/{spec['spec_id']}")
        for hit in _json(server, f"/api/p/{pid}/symbols?q=pkg")["results"]:
            _json(server, f"/api/p/{pid}/symbol?qname={hit['qname']}")
    for g in home["groups"]:
        _json(server, f"/api/g/{g['id']}")
    for path in ("/", f"/p/{home['projects'][0]['id']}"):
        _get(server, path)

    after = {str(r): _tree(r) for r in roots}
    assert after == before


def test_readonly_connection_refuses_writes(indexed):
    import sqlite3

    from livespec_mcp.storage.readonly import connect_readonly

    conn = connect_readonly(indexed["billing"] / ".mcp-docs" / "docs.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM spec")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Removal of the in-repo explorers (hard cut)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explorer_tools_are_gone_from_tools_list():
    async with Client(mcp) as c:  # conftest forces LIVESPEC_PLUGINS=all
        tools = {t.name: t for t in await c.list_tools()}
    assert "export_explorer" not in tools
    assert "export_flow_explorer" not in tools
    assert "explorer" not in tools["index_project"].inputSchema.get("properties", {})
    # Non-UI docs tooling stays.
    assert {"generate_docs", "list_docs", "export_documentation"} <= set(tools)


@pytest.mark.parametrize("argv", [["explorer", "serve", "."], ["fastapi", "init", "."]])
def test_explorer_cli_subcommands_are_gone(argv):
    from livespec_mcp import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_explorer_modules_are_gone():
    import importlib.util

    for mod in (
        "livespec_mcp.explorer",
        "livespec_mcp.tools.explorer",
        "livespec_mcp.tools.flow_explorer",
    ):
        assert importlib.util.find_spec(mod) is None, mod


@pytest.mark.asyncio
async def test_indexing_writes_no_ui_bundle_even_for_a_fastapi_app(workspace):
    """The old first-index autodetect built .mcp-docs/explorer/ and patched
    main.py for any FastAPI app. Neither may happen now."""
    main_py = (
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/x')\ndef x():\n    return 1\n"
    )
    (workspace / "main.py").write_text(main_py)
    async with Client(mcp) as c:
        out = _call(await c.call_tool("index_project", {"workspace": str(workspace)}))
    assert "explorer_regenerated" not in out
    assert not (workspace / ".mcp-docs" / "explorer").exists()
    assert (workspace / "main.py").read_text() == main_py
    assert not (workspace / ".cursor").exists()
