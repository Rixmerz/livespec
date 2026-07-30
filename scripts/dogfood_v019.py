#!/usr/bin/env python3
"""Dogfood v0.19 features against livespec-mcp itself. Run from repo root."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []
CHECKS: list[str] = []


def ok(name: str, detail: str = "") -> None:
    CHECKS.append(f"OK  {name}" + (f" — {detail}" if detail else ""))


def fail(name: str, detail: str) -> None:
    FAILURES.append(f"FAIL {name}: {detail}")


async def main() -> int:
    from fastmcp import Client

    from livespec_mcp.server import mcp

    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        ok("tools/list", f"{len(names)} tools")

        for required in (
            "index_project",
            "grep_in_indexed_files",
            "find_endpoints",
            "search",
            "list_specs",
            "git_diff_impact",
            "find_legacy_flows",
            "import_specs_from_markdown",
            "sync_openspec",
        ):
            if required not in names:
                fail("core tools visible", f"missing {required}")
            else:
                ok("tool registered", required)

        if "agent_scratch" in names:
            fail("agent_scratch removed", "still in tools/list")
        else:
            ok("agent_scratch dropped", "not in tools/list")

        if "export_explorer" in names:
            ok("export_explorer", "visible (LIVESPEC_PLUGINS or prior docs unlock)")
        else:
            ok("export_explorer", "gated until docs plugin / explorer bundle")

        pre_count = len(names)
        idx = await client.call_tool(
            "index_project",
            {"workspace": str(WS), "explorer": True},
        )
        payload = idx.structured_content or {}
        if payload.get("isError"):
            fail("index_project", str(payload))
        else:
            sym = payload.get("symbols_indexed") or payload.get("symbol_count")
            ok(
                "index_project(explorer=True)",
                f"explorer_regenerated={payload.get('explorer_regenerated')} syms≈{sym or '?'}",
            )

        # Plugin menu should expand after first workspace= call
        tools2 = await client.list_tools()
        names2 = {t.name for t in tools2}
        if len(names2) > pre_count:
            ok("plugin menu expand", f"{pre_count} → {len(names2)} tools after index")
        else:
            ok("plugin menu", f"{len(names2)} tools (same as pre-index)")

        if "create_spec" in names2:
            ok("Spec plugin visible", "create_spec")
        if "generate_docs" in names2:
            ok("docs plugin visible", "generate_docs")

        # Index + explorer bundle (continued)
        bundle = WS / ".mcp-docs" / "explorer"
        if (bundle / "index.html").is_file() and (bundle / "data.json").is_file():
            data = json.loads((bundle / "data.json").read_text(encoding="utf-8"))
            eps = data.get("endpoints") or []
            http_eps = [e for e in eps if e.get("method") and e.get("path")]
            ok("explorer bundle", f"{len(eps)} endpoints, {len(http_eps)} with method+path")
            if data.get("meta", {}).get("base_path") != "/explorer":
                fail("meta.base_path", str(data.get("meta", {}).get("base_path")))
            else:
                ok("meta.base_path", "/explorer")
            trend = data.get("trend")
            if isinstance(trend, dict) and trend.get("snapshots"):
                ok("trend snapshots", str(len(trend["snapshots"])))
            elif isinstance(trend, list) and trend:
                ok("trend snapshots", str(len(trend)))
            else:
                ok("trend snapshots", "none (OK if no history yet)")
        else:
            fail("explorer bundle", f"missing under {bundle}")

        # grep_in_indexed_files
        grep = await client.call_tool(
            "grep_in_indexed_files",
            {
                "workspace": str(WS),
                "pattern": "mount_explorer",
                "path_glob": "**/*.py",
                "limit": 5,
            },
        )
        gp = grep.structured_content or {}
        if gp.get("isError"):
            fail("grep_in_indexed_files", str(gp))
        else:
            matches = gp.get("matches") or []
            ok("grep_in_indexed_files", f"{len(matches)} hits for mount_explorer")

        # search snake_case fix
        sr = await client.call_tool(
            "search", {"workspace": str(WS), "query": "index_project", "limit": 5}
        )
        sp = sr.structured_content or {}
        if sp.get("isError"):
            fail("search", str(sp))
        else:
            results = sp.get("results") or []
            ok("search(index_project)", f"{len(results)} results")

        # git_diff_impact payload_warning shape (may or may not warn)
        gd = await client.call_tool(
            "git_diff_impact",
            {"workspace": str(WS), "summary_only": True},
        )
        gdp = gd.structured_content or {}
        if gdp.get("isError"):
            fail("git_diff_impact", str(gdp))
        else:
            ok(
                "git_diff_impact(summary_only)",
                f"files={gdp.get('files_changed_count', '?')} warning={'payload_warning' in gdp}",
            )

        # list_specs (dogfood has Specs)
        lr = await client.call_tool(
            "list_specs", {"workspace": str(WS)}
        )
        lrp = lr.structured_content or {}
        if lrp.get("isError"):
            fail("list_specs", str(lrp))
        else:
            specs = lrp.get("specs") or []
            ok("list_specs", f"{len(specs)} Specs")

        # export_explorer refresh
        ex = await client.call_tool(
            "export_explorer", {"workspace": str(WS)}
        )
        exp = ex.structured_content or {}
        if exp.get("isError"):
            fail("export_explorer", str(exp))
        else:
            aw = (exp.get("autowire") or {})
            ok(
                "export_explorer",
                f"autowire wired={aw.get('wired')} reason={aw.get('reason')}",
            )

        # find_endpoints — MCP tools should appear
        fe = await client.call_tool(
            "find_endpoints", {"workspace": str(WS), "limit": 20}
        )
        fep = fe.structured_content or {}
        if fep.get("isError"):
            fail("find_endpoints", str(fep))
        else:
            ok("find_endpoints", f"total={fep.get('total')} page={len(fep.get('endpoints') or [])}")

    # ASGI mount smoke (in-process)
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from livespec_mcp.explorer.asgi import mount_explorer
    from livespec_mcp.explorer.fastapi import enable_explorer

    app = Starlette(routes=[Route("/", lambda r: PlainTextResponse("api"))])
    mount_explorer(app, workspace=WS)
    client = TestClient(app)
    r = client.get("/explorer/endpoints")
    if r.status_code == 200 and "swagger" in r.text.lower() or "Spec Explorer" in r.text:
        ok("mount_explorer /explorer/endpoints", f"status={r.status_code}")
    elif r.status_code == 200:
        ok("mount_explorer /explorer/endpoints", f"status={r.status_code} len={len(r.text)}")
    else:
        fail("mount_explorer", f"status={r.status_code}")

    html = (WS / ".mcp-docs" / "explorer" / "index.html").read_text(encoding="utf-8")
    for marker in ("ep-base-url", "http-exec", "copy-call", "buildTrendOverview"):
        if marker in html:
            ok("explorer HTML", marker)
        else:
            fail("explorer HTML", f"missing {marker}")

    # pr_diff_impact script
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(WS / "scripts" / "pr_diff_impact.py")],
        cwd=str(WS),
        capture_output=True,
        text=True,
        env={**dict(**{"LIVESPEC_WORKSPACE": str(WS)}), **dict(__import__("os").environ)},
    )
    if proc.returncode == 0 and "Livespec" in proc.stdout:
        ok("pr_diff_impact.py", f"{len(proc.stdout)} chars markdown")
    else:
        fail("pr_diff_impact.py", proc.stderr[:200] or proc.stdout[:200])

    print("\n".join(CHECKS))
    if FAILURES:
        print("\n--- FAILURES ---")
        print("\n".join(FAILURES))
        return 1
    print(f"\nAll {len(CHECKS)} dogfood checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
