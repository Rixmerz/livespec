"""Aggregator over `agent_log.jsonl` streams from the v0.8 battle-test.

Reads one or more JSONL log files (default: `<workspace>/.mcp-docs/agent_log.jsonl`
for the workspaces given on the CLI; or stdin if `-` is passed) and emits:

  - Per-tool call count, error count, latency p50/p95, result_chars p50/max
  - Follow-up tool pairs (A -> B sequences within a session_id) — surfaces
    common 3-tool chains that a composite tool could collapse
  - Per-codebase summary (one block per workspace path observed)
  - Top 10 silent tools (registered but never called) — drop candidates

Output: a human-readable Markdown table by default, or `--json` to dump
the aggregate as a single JSON object suitable for diffing across runs.

This is the input feed for the v0.8 P3 curation pass: drop a tool only
when N-session data says it never gets called, not because the author's
intuition says so. ROADMAP §6 self-admits that intuition was wrong.

Usage:
    uv run python bench/agent_log_analyze.py                  # cwd workspace
    uv run python bench/agent_log_analyze.py path/to/ws ...   # multiple
    uv run python bench/agent_log_analyze.py - --json out.json  # stdin
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = int(len(s) * pct)
    return s[min(idx, len(s) - 1)]


def load_logs(sources: list[Path | str]) -> list[dict]:
    """Load JSONL entries from each source. `-` means stdin; directories
    are resolved to `<dir>/.mcp-docs/agent_log.jsonl`; files are read as-is."""
    entries: list[dict] = []
    for src in sources:
        if src == "-":
            text = sys.stdin.read()
        else:
            p = Path(src)
            if p.is_dir():
                p = p / ".mcp-docs" / "agent_log.jsonl"
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def aggregate(entries: list[dict], known_tools: list[str] | None = None) -> dict:
    by_tool: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    latency: dict[str, list[int]] = defaultdict(list)
    chars: dict[str, list[int]] = defaultdict(list)
    by_session: dict[str, list[dict]] = defaultdict(list)
    by_workspace: Counter[str] = Counter()

    for e in entries:
        n = e.get("tool_name", "<unknown>")
        by_tool[n] += 1
        if e.get("error"):
            errors[n] += 1
        latency[n].append(int(e.get("latency_ms", 0)))
        chars[n].append(int(e.get("result_chars", 0)))
        sid = e.get("session_id")
        if sid:
            by_session[sid].append(e)
        ws = e.get("workspace") or "<unknown>"
        by_workspace[ws] += 1

    pairs: Counter[tuple[str, str]] = Counter()
    for sess_entries in by_session.values():
        sess_entries.sort(key=lambda x: x.get("ts", ""))
        for a, b in zip(sess_entries, sess_entries[1:]):
            pairs[(a["tool_name"], b["tool_name"])] += 1

    tool_rows = []
    for n in sorted(by_tool, key=lambda k: by_tool[k], reverse=True):
        tool_rows.append(
            {
                "name": n,
                "calls": by_tool[n],
                "errors": errors[n],
                "latency_ms_p50": _percentile(latency[n], 0.50),
                "latency_ms_p95": _percentile(latency[n], 0.95),
                "result_chars_p50": _percentile(chars[n], 0.50),
                "result_chars_max": max(chars[n]) if chars[n] else 0,
            }
        )

    silent: list[str] = []
    if known_tools is not None:
        called = set(by_tool.keys())
        silent = sorted(t for t in known_tools if t not in called)

    return {
        "totals": {
            "calls": len(entries),
            "sessions": len(by_session),
            "workspaces": len(by_workspace),
            "tools_called": len(by_tool),
        },
        "by_workspace": [
            {"workspace": ws, "calls": c}
            for ws, c in by_workspace.most_common()
        ],
        "tools": tool_rows,
        "follow_up_pairs": [
            {"from": a, "to": b, "count": c}
            for (a, b), c in pairs.most_common(20)
        ],
        "silent_tools": silent,
    }


def render_markdown(agg: dict) -> str:
    lines: list[str] = []
    t = agg["totals"]
    lines.append("# Agent Log Aggregate")
    lines.append("")
    lines.append(
        f"- Calls: **{t['calls']}** "
        f"across **{t['sessions']}** sessions "
        f"in **{t['workspaces']}** workspaces — "
        f"**{t['tools_called']}** distinct tools called"
    )
    lines.append("")

    if agg["by_workspace"]:
        lines.append("## Calls per workspace")
        lines.append("")
        lines.append("| workspace | calls |")
        lines.append("|---|---:|")
        for r in agg["by_workspace"]:
            lines.append(f"| `{r['workspace']}` | {r['calls']} |")
        lines.append("")

    lines.append("## Tools (by call count)")
    lines.append("")
    lines.append(
        "| tool | calls | errors | p50 ms | p95 ms | p50 chars | max chars |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in agg["tools"]:
        lines.append(
            f"| `{r['name']}` | {r['calls']} | {r['errors']} | "
            f"{r['latency_ms_p50']} | {r['latency_ms_p95']} | "
            f"{r['result_chars_p50']} | {r['result_chars_max']} |"
        )
    lines.append("")

    if agg["follow_up_pairs"]:
        lines.append("## Top follow-up pairs (A → B within session)")
        lines.append("")
        lines.append("| from | to | count |")
        lines.append("|---|---|---:|")
        for p in agg["follow_up_pairs"]:
            lines.append(f"| `{p['from']}` | `{p['to']}` | {p['count']} |")
        lines.append("")

    if agg["silent_tools"]:
        lines.append("## Silent tools (registered but never called)")
        lines.append("")
        for t_name in agg["silent_tools"]:
            lines.append(f"- `{t_name}`")
        lines.append("")

    return "\n".join(lines)


def _list_known_tools() -> list[str]:
    """Best-effort enumeration of tools registered on the live mcp instance.

    Imports lazily so this script works against pre-recorded JSONL even
    when the package isn't importable (e.g. an external analysis tool).
    """
    try:
        import asyncio

        from fastmcp import Client

        from livespec_mcp.server import mcp

        async def _names() -> list[str]:
            async with Client(mcp) as c:
                tools = await c.list_tools()
                return sorted(t.name for t in tools)

        return asyncio.run(_names())
    except Exception:
        return []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "sources",
        nargs="*",
        default=["."],
        help="Workspace dirs, JSONL files, or '-' for stdin (default: cwd)",
    )
    p.add_argument(
        "--json",
        metavar="OUT",
        help="Write aggregate as JSON to this path instead of Markdown",
    )
    p.add_argument(
        "--no-known-tools",
        action="store_true",
        help="Skip enumerating registered tools (no silent_tools section)",
    )
    args = p.parse_args(argv)

    entries = load_logs(args.sources)
    known = None if args.no_known_tools else _list_known_tools()
    agg = aggregate(entries, known_tools=known)

    if args.json:
        Path(args.json).write_text(json.dumps(agg, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    else:
        print(render_markdown(agg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
