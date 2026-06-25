#!/usr/bin/env python3
"""Smoke-test livespec MCP after restart (stdio server, real workspaces)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastmcp import Client

from livespec_mcp.server import mcp

OVER = Path("<sample-api>")
LIVESPEC = Path("<repo>")


async def main() -> int:
    errors: list[str] = []

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    required = {
        "index_project",
        "get_project_overview",
        "find_symbol",
        "list_requirements",
        "search",
    }
    missing_tools = required - names
    if missing_tools:
        errors.append(f"missing tools: {sorted(missing_tools)}")

    def _workspace_meta(schema: dict) -> tuple[str, list]:
        if schema.get("description"):
            return schema.get("description", ""), schema.get("examples") or []
        for branch in schema.get("anyOf") or []:
            d, ex = _workspace_meta(branch)
            if d or ex:
                return d, ex
        return "", []

    idx_tool = next(t for t in tools if t.name == "index_project")
    ws_schema = (idx_tool.parameters or {}).get("properties", {}).get("workspace", {})
    desc, examples = _workspace_meta(ws_schema)
    if "REQUIRED on every call" not in desc:
        errors.append(f"workspace description missing: {desc[:120]!r}...")
    if not examples:
        errors.append("workspace parameter has no examples in schema")

    for repo in (OVER, LIVESPEC):
        if not repo.is_dir():
            errors.append(f"repo missing: {repo}")
            continue

    if errors:
        print("SCHEMA CHECK FAILED")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("Schema OK:", len(tools), "tools; workspace description + examples present")

    async with Client(mcp) as client:
        # Missing workspace must fail
        try:
            await client.call_tool("list_requirements", {})
            errors.append("list_requirements without workspace should fail")
        except Exception as exc:
            msg = str(exc).lower()
            if "workspace" not in msg and "required" not in msg:
                errors.append(f"unexpected error without workspace: {exc}")

        for label, ws in (("sample-api", OVER), ("livespec-mcp", LIVESPEC)):
            ws_s = str(ws)
            print(f"\n--- {label} ({ws_s}) ---")
            ov = await client.call_tool(
                "get_project_overview",
                {"workspace": ws_s, "include_infrastructure": False},
            )
            data = ov.data if hasattr(ov, "data") else ov
            if isinstance(data, str):
                data = json.loads(data)
            if data.get("error"):
                errors.append(f"{label} overview error: {data}")
            else:
                syms = data.get("symbol_count") or data.get("top_symbols")
                print(
                    "  overview OK — keys sample:",
                    list(data.keys())[:8],
                    f"symbols/top: {bool(syms)}",
                )

            fs = await client.call_tool(
                "find_symbol",
                {"query": "register", "limit": 3, "workspace": ws_s},
            )
            fd = fs.data if hasattr(fs, "data") else fs
            if isinstance(fd, str):
                fd = json.loads(fd)
            matches = (fd or {}).get("matches") or []
            print(f"  find_symbol(register): {len(matches)} matches")

            lr = await client.call_tool("list_requirements", {"workspace": ws_s, "limit": 5})
            ld = lr.data if hasattr(lr, "data") else lr
            if isinstance(ld, str):
                ld = json.loads(ld)
            reqs = (ld or {}).get("requirements") or (ld or {}).get("items") or []
            print(f"  list_requirements: {len(reqs)} RFs")

    if errors:
        print("\nVALIDATION FAILED")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("\nVALIDATION OK — restart Cursor MCP 'livespec' if tools in IDE are stale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
