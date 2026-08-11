#!/usr/bin/env python3
"""Audit every MCP tool against cross-repo (group_db) and solo workspaces.

Produces a JSON report with latency, error shape, and coarse signal metrics
so we can score keep / improve / fix / drop. Read-mostly; mutations use
dry_run or create+delete a disposable Spec.

Workspaces are read from the environment so private repo paths never land in
this tree::

    LIVESPEC_AUDIT_CROSS_WS=/abs/repo/in/a/group_db  \\
    LIVESPEC_AUDIT_SOLO_WS=/abs/repo                 \\
    LIVESPEC_AUDIT_OUT=/abs/out.json                 \\
    uv run python scripts/dogfood_tool_value_audit.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any

from fastmcp import Client

from livespec_mcp.server import mcp

REPO_ROOT = Path(__file__).resolve().parent.parent
SOLO_WS = os.environ.get("LIVESPEC_AUDIT_SOLO_WS", str(REPO_ROOT))
CROSS_WS = os.environ.get("LIVESPEC_AUDIT_CROSS_WS", SOLO_WS)
OUT = Path(os.environ.get("LIVESPEC_AUDIT_OUT", "/tmp/livespec-tool-value-audit.json"))

# Caps — keep payloads small for the audit.
LIMIT = 20


def _data(result: Any) -> dict[str, Any]:
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        return result.structured_content
    return {"_raw_type": type(result).__name__}


def _signal(payload: dict[str, Any]) -> dict[str, Any]:
    """Coarse usefulness metrics — not a quality judgment."""
    if payload.get("isError"):
        return {
            "error": True,
            "message": (payload.get("error") or "")[:240],
            "hint": (payload.get("hint") or "")[:160] or None,
        }
    sig: dict[str, Any] = {"error": False}
    for k in (
        "count",
        "total",
        "truncated",
        "grouped",
        "group_db",
        "legacy_server_count",
        "orphan_client_count",
        "live_server_count",
        "live_client_count",
        "server_route_count",
        "client_route_count",
        "files_changed_count",
        "symbols_indexed",
        "files_changed",
        "index_fresh",
        "query_mode",
        "found",
        "verified",
        "wired",
        "stale_count",
        "proposal_count",
        "imported",
        "created",
        "updated",
        "linked",
        "deleted",
        "ok",
        "valid",
        "issue_count",
        "warning_count",
        "skipped_covered_count",
        "test_files_count",
        "test_function_symbols",
        "dry_run",
    ):
        if k in payload:
            sig[k] = payload[k]
    for k, v in payload.items():
        if isinstance(v, list):
            sig[f"len_{k}"] = len(v)
        elif isinstance(v, dict) and k in ("counts", "summary", "autowire", "meta"):
            sig[k] = {kk: vv for kk, vv in v.items() if not isinstance(vv, (list, dict))}
            for kk, vv in v.items():
                if isinstance(vv, list):
                    sig[f"len_{k}_{kk}"] = len(vv)
    # Truncate giant string fields
    for k in ("hint", "source", "body", "content"):
        if isinstance(payload.get(k), str):
            sig[f"{k}_chars"] = len(payload[k])
    return sig


async def _call(client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        result = await client.call_tool(name, args)
        payload = _data(result)
        ms = int((time.perf_counter() - t0) * 1000)
        return {
            "tool": name,
            "ok": not bool(payload.get("isError")),
            "ms": ms,
            "args_keys": sorted(k for k in args if k != "workspace"),
            "signal": _signal(payload),
        }
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        return {
            "tool": name,
            "ok": False,
            "ms": ms,
            "args_keys": sorted(k for k in args if k != "workspace"),
            "signal": {
                "error": True,
                "message": f"{type(e).__name__}: {e}"[:240],
                "trace_tail": traceback.format_exc()[-400:],
            },
        }


async def _seed(client: Client, workspace: str) -> dict[str, Any]:
    """Pick concrete anchors so graph/Spec tools have real targets."""
    seed: dict[str, Any] = {"workspace": workspace}
    overview = await _call(client, "get_project_overview", {"workspace": workspace})
    seed["overview"] = overview
    ov_raw = (
        await client.call_tool("get_project_overview", {"workspace": workspace})
    ).data
    tops = ov_raw.get("top_symbols") or []
    for s in tops:
        if s.get("kind") in ("function", "method", "class") and s.get("qualified_name"):
            seed["qname"] = s["qualified_name"]
            seed["find_symbol_query"] = s.get("name")
            break
    if not seed.get("qname"):
        for query in ("Controller", "list", "search", "index_project", "register"):
            fs = await client.call_tool(
                "find_symbol",
                {"workspace": workspace, "query": query, "kind": "function", "limit": 5},
            )
            data = _data(fs)
            syms = data.get("matches") or data.get("symbols") or data.get("results") or []
            for s0 in syms:
                qn = s0.get("qualified_name") or s0.get("qname")
                if qn and s0.get("kind") != "module":
                    seed["qname"] = qn
                    seed["find_symbol_query"] = query
                    break
            if seed.get("qname"):
                break
    # Prefer a server route symbol for cross-repo who_calls.route_callers
    try:
        eps = _data(
            await client.call_tool(
                "find_endpoints",
                {"workspace": workspace, "limit": 10, "summary_only": False},
            )
        )
        for ep in eps.get("endpoints") or eps.get("items") or []:
            qn = ep.get("qualified_name") or ep.get("qname")
            if qn and (ep.get("http_path") or ep.get("path")):
                seed["endpoint_qname"] = qn
                seed["endpoint_path"] = ep.get("http_path") or ep.get("path")
                break
    except Exception:
        pass
    ls = await client.call_tool(
        "list_specs",
        {"workspace": workspace, "limit": 5, "summary_only": False},
    )
    specs = (_data(ls).get("specs") or [])
    if specs:
        seed["spec_id"] = specs[0].get("spec_id") or specs[0].get("id")
        # Prefer a Spec that already has scenarios (link_scenario_symbol)
        for sp in specs:
            sid = sp.get("spec_id") or sp.get("id")
            if not sid:
                continue
            try:
                impl = _data(
                    await client.call_tool(
                        "get_spec_implementation",
                        {"workspace": workspace, "spec_id": sid},
                    )
                )
            except Exception:
                continue
            scens = impl.get("scenarios") or []
            if scens and scens[0].get("name"):
                seed["spec_id"] = sid
                seed["scenario_name"] = scens[0]["name"]
                break
        if not seed.get("scenario_name") and seed.get("spec_id"):
            try:
                impl = _data(
                    await client.call_tool(
                        "get_spec_implementation",
                        {"workspace": workspace, "spec_id": seed["spec_id"]},
                    )
                )
                scens = impl.get("scenarios") or []
                if scens and scens[0].get("name"):
                    seed["scenario_name"] = scens[0]["name"]
            except Exception:
                pass
    lsc = await client.call_tool("list_spec_changes", {"workspace": workspace})
    changes = (_data(lsc).get("changes") or _data(lsc).get("spec_changes") or [])
    proposed = [c for c in changes if (c.get("status") or "") != "archived"]
    if proposed:
        seed["change_name"] = proposed[0].get("name") or proposed[0].get("id")
    else:
        # Temporary OpenSpec change so get/apply/archive are exercised
        change_name = "audit-tool-probe"
        root = Path(workspace) / "openspec" / "changes" / change_name
        cap = "audit"
        (root / "specs" / cap).mkdir(parents=True, exist_ok=True)
        (root / "proposal.md").write_text(
            "# Audit tool probe\n\nTemporary dogfood change.\n", encoding="utf-8"
        )
        (root / "specs" / cap / "spec.md").write_text(
            "## ADDED Requirements\n\n"
            "### Requirement: Audit probe only\n"
            "Temporary.\n\n"
            "#### Scenario: Audit probe scenario\n"
            "- **WHEN** audit runs\n"
            "- **THEN** change tools are exercised\n",
            encoding="utf-8",
        )
        seed["change_name"] = change_name
        seed["change_dir"] = str(root)
        await client.call_tool("sync_openspec", {"workspace": workspace})
    return seed


def _cases(seed: dict[str, Any], *, mode: str) -> list[tuple[str, dict[str, Any]]]:
    ws = seed["workspace"]
    qname = seed.get("qname") or "does.not.exist"
    spec_id = seed.get("spec_id") or "SPEC-MISSING"
    change = seed.get("change_name")
    scenario = seed.get("scenario_name")

    cases: list[tuple[str, dict[str, Any]]] = [
        ("get_project_overview", {"workspace": ws, "include_structural_patterns": False}),
        ("find_symbol", {"workspace": ws, "query": seed.get("find_symbol_query") or "list", "limit": 10}),
        ("quick_orient", {"workspace": ws, "qname": qname}),
        ("get_symbol_source", {"workspace": ws, "qname": qname}),
        ("who_calls", {"workspace": ws, "qname": qname, "limit": LIMIT, "summary_only": True}),
        ("who_does_this_call", {"workspace": ws, "qname": qname, "limit": LIMIT, "summary_only": True}),
        (
            "analyze_impact",
            {
                "workspace": ws,
                "target_type": "symbol",
                "target": qname,
                "limit": LIMIT,
                "summary_only": True,
            },
        ),
        ("find_dead_code", {"workspace": ws, "limit": LIMIT, "summary_only": True}),
        ("find_orphan_tests", {"workspace": ws, "limit": LIMIT, "summary_only": True}),
        ("find_endpoints", {"workspace": ws, "limit": LIMIT, "summary_only": True}),
        (
            "find_legacy_flows",
            {"workspace": ws, "limit": LIMIT, "summary_only": True, "include_infra": False},
        ),
        ("audit_coverage", {"workspace": ws, "limit": LIMIT, "summary_only": True}),
        ("git_diff_impact", {"workspace": ws, "summary_only": True}),
        (
            "grep_in_indexed_files",
            {
                "workspace": ws,
                "pattern": seed.get("find_symbol_query") or qname.rsplit(".", 1)[-1],
                "limit": 10,
            },
        ),
        ("search", {"workspace": ws, "query": "search", "limit": 10}),
        ("list_specs", {"workspace": ws, "limit": LIMIT, "summary_only": True}),
        ("get_spec_implementation", {"workspace": ws, "spec_id": spec_id}),
        ("propose_specs_from_codebase", {"workspace": ws, "max_proposals": 5}),
        ("scan_annotation_verbs", {"workspace": ws, "sample_per_group": 3}),
        ("scan_spec_annotations", {"workspace": ws}),
        ("scan_docstrings_for_spec_hints", {"workspace": ws, "limit": LIMIT, "summary_only": True}),
        ("list_spec_changes", {"workspace": ws}),
        ("validate_openspec", {"workspace": ws, "strict": False}),
        ("sync_openspec", {"workspace": ws}),
        ("export_openspec", {"workspace": ws, "out_dir": ".mcp-docs/_audit_openspec_export"}),
        ("export_explorer", {"workspace": ws}),
        ("export_flow_explorer", {"workspace": ws}),
        ("list_docs", {"workspace": ws}),
        ("export_documentation", {"workspace": ws, "out_subdir": "_audit_docs_export"}),
        (
            "generate_docs",
            {
                "workspace": ws,
                "target_type": "symbol",
                "identifier": qname,
                "content": "audit probe doc — safe to ignore",
            },
        ),
        # Mutation: disposable Spec round-trip
        (
            "create_spec",
            {
                "workspace": ws,
                "title": f"AUDIT probe {mode}",
                "spec_id": f"AUDIT-{mode.upper()}-PROBE",
                "kind": "functional_requirement",
                "status": "draft",
            },
        ),
    ]

    if mode == "cross":
        cases.append(
            (
                "find_legacy_flows",
                {
                    "workspace": ws,
                    "project": "search-service",
                    "limit": LIMIT,
                    "summary_only": True,
                    "include_infra": False,
                },
            )
        )
        cases.append(
            (
                "find_endpoints",
                {"workspace": ws, "framework": "express", "limit": LIMIT, "summary_only": True},
            )
        )
        cases.append(
            (
                "find_endpoints",
                {"workspace": ws, "framework": "spring", "limit": LIMIT, "summary_only": True},
            )
        )
        route_q = seed.get("endpoint_qname") or qname
        cases.append(
            (
                "who_calls",
                {
                    "workspace": ws,
                    "qname": route_q,
                    "limit": LIMIT,
                    "summary_only": False,
                },
            )
        )
        cases.append(
            (
                "who_does_this_call",
                {
                    "workspace": ws,
                    "qname": route_q,
                    "limit": LIMIT,
                    "summary_only": False,
                },
            )
        )

    if change:
        cases.append(("get_spec_change", {"workspace": ws, "name": change}))
        cases.append(
            ("apply_spec_change", {"workspace": ws, "name": change, "dry_run": True})
        )
        cases.append(("archive_spec_change", {"workspace": ws, "name": change}))

    if scenario and seed.get("spec_id"):
        cases.append(
            (
                "link_scenario_symbol",
                {
                    "workspace": ws,
                    "spec_id": seed["spec_id"],
                    "scenario_name": scenario,
                    "symbol_qname": qname,
                    "relation": "implements",
                    "confidence": 0.5,
                    "source": "manual",
                },
            )
        )
        cases.append(
            (
                "link_scenario_symbol",
                {
                    "workspace": ws,
                    "spec_id": seed["spec_id"],
                    "scenario_name": scenario,
                    "symbol_qname": qname,
                    "unlink": True,
                },
            )
        )

    # Spec graph / link probes after create (caller sequences them)
    cases.append(
        (
            "update_spec",
            {
                "workspace": ws,
                "spec_id": f"AUDIT-{mode.upper()}-PROBE",
                "description": "updated by tool-value audit",
            },
        )
    )
    cases.append(
        (
            "link_spec_symbol",
            {
                "workspace": ws,
                "spec_id": f"AUDIT-{mode.upper()}-PROBE",
                "symbol_qname": qname,
                "relation": "implements",
                "confidence": 0.5,
                "source": "manual",
            },
        )
    )
    if seed.get("spec_id") and seed["spec_id"] != f"AUDIT-{mode.upper()}-PROBE":
        cases.append(
            (
                "link_spec_dependency",
                {
                    "workspace": ws,
                    "parent_spec_id": f"AUDIT-{mode.upper()}-PROBE",
                    "child_spec_id": seed["spec_id"],
                    "kind": "requires",
                },
            )
        )
        cases.append(
            (
                "get_spec_dependency_graph",
                {
                    "workspace": ws,
                    "spec_id": f"AUDIT-{mode.upper()}-PROBE",
                    "max_depth": 2,
                },
            )
        )
        cases.append(
            (
                "unlink_spec_dependency",
                {
                    "workspace": ws,
                    "parent_spec_id": f"AUDIT-{mode.upper()}-PROBE",
                    "child_spec_id": seed["spec_id"],
                },
            )
        )
    cases.append(
        (
            "bulk_link_spec_symbols",
            {
                "workspace": ws,
                "mappings": [
                    {
                        "spec_id": f"AUDIT-{mode.upper()}-PROBE",
                        "symbol_qname": qname,
                        "relation": "implements",
                    }
                ],
            },
        )
    )
    cases.append(
        (
            "analyze_impact",
            {
                "workspace": ws,
                "target_type": "spec",
                "target": f"AUDIT-{mode.upper()}-PROBE",
                "summary_only": True,
                "limit": LIMIT,
            },
        )
    )
    cases.append(
        (
            "delete_spec",
            {"workspace": ws, "spec_id": f"AUDIT-{mode.upper()}-PROBE"},
        )
    )

    # index last / light — never force on cross (huge)
    cases.append(("index_project", {"workspace": ws, "force": False, "explorer": False}))

    # import only if openspec dir likely exists
    openspec = Path(ws) / "openspec"
    if openspec.is_dir():
        cases.insert(
            20,
            ("import_specs_from_markdown", {"workspace": ws, "path": "openspec"}),
        )

    # archive_spec_change is probed (dry lifecycle) when a change seed exists
    return cases


async def _run_mode(client: Client, workspace: str, mode: str) -> dict[str, Any]:
    seed = await _seed(client, workspace)
    results = []
    seen_tools: set[str] = set()
    for name, args in _cases(seed, mode=mode):
        # Deduplicate tool name tracking but allow multiple arg variants
        row = await _call(client, name, args)
        row["variant"] = ",".join(f"{k}={args[k]!r}" for k in args if k not in ("workspace",) and k in (
            "framework", "project", "target_type", "summary_only", "dry_run", "force", "unlink"
        ))
        results.append(row)
        seen_tools.add(name)

    # Cleanup temp change dir if we created one
    change_dir = seed.get("change_dir")
    if change_dir:
        import shutil

        shutil.rmtree(change_dir, ignore_errors=True)

    # Tools registered but never called in this mode
    all_tools = {t.name for t in await client.list_tools()}
    skipped = sorted(all_tools - seen_tools)
    return {
        "mode": mode,
        "workspace": workspace,
        "seed": {
            "qname": seed.get("qname"),
            "spec_id": seed.get("spec_id"),
            "change_name": seed.get("change_name"),
            "find_symbol_query": seed.get("find_symbol_query"),
            "scenario_name": seed.get("scenario_name"),
        },
        "results": results,
        "tools_touched": sorted(seen_tools),
        "tools_skipped": skipped,
        "ok_count": sum(1 for r in results if r["ok"]),
        "fail_count": sum(1 for r in results if not r["ok"]),
        "total_ms": sum(r["ms"] for r in results),
    }


async def main() -> int:
    import os

    os.environ.setdefault("LIVESPEC_PLUGINS", "all")
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "modes": [],
    }
    async with Client(mcp) as client:
        tools = await client.list_tools()
        report["tool_count_registered"] = len(tools)
        report["tool_names"] = sorted(t.name for t in tools)
        report["modes"].append(await _run_mode(client, CROSS_WS, "cross"))
        report["modes"].append(await _run_mode(client, SOLO_WS, "solo"))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {OUT}")
    for mode in report["modes"]:
        print(
            f"{mode['mode']}: ok={mode['ok_count']} fail={mode['fail_count']} "
            f"ms={mode['total_ms']} skipped={mode['tools_skipped']}"
        )
        for r in mode["results"]:
            if not r["ok"]:
                print(f"  FAIL {r['tool']} ({r.get('variant')}): {r['signal'].get('message')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
