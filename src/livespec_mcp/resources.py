"""MCP resources: project:// addressable views.

Canonical URI scheme (v0.20+):

| URI | MIME | Purpose |
|-----|------|---------|
| ``project://overview`` | JSON | Project overview (PageRank spine) |
| ``project://index/status`` | JSON | Index stats |
| ``project://group`` | JSON | Polyrepo group_db + ``xrepo-*`` Specs |
| ``project://specs`` | JSON | All Specs |
| ``project://specs/{spec_id}`` | JSON | Spec + implementations |
| ``project://files/{path}`` | JSON | Indexed file + symbols |
| ``project://symbols/{qname}`` | JSON | Symbol metadata |
| ``guide://cross-repo`` | markdown | How to use group_db + xrepo Specs |
| ``doc://symbol/{qname}`` | markdown | Generated symbol doc |
| ``doc://spec/{spec_id}`` | markdown | Generated Spec doc |
| ``code://symbol/{qname}`` | plain | Raw symbol source slice |

Legacy alias ``livespec://…`` is **not** registered — use ``project://`` /
``doc://`` / ``code://`` / ``guide://`` as above. External docs may refer to the
product as "livespec" but resource URIs stay on these schemes for MCP
compatibility.

Workspace resolution (v0.14): resource URIs have no ``workspace`` parameter
channel, so resources bind to the **most recently used** workspace (the one
the last tool call touched). Before any tool call there is nothing to bind
to — JSON resources then return an `mcp_error`-shaped payload with a hint,
text resources a one-line explanation. ``guide://cross-repo`` is static and
does not need a workspace.

Error shape (v0.14, closes the v0.6 P4 contract gap): JSON resources use
``tools._errors.mcp_error`` for every error payload. Text resources
(text/markdown, text/plain) stay human-readable text — a JSON error blob
inside a markdown document would be worse than a sentence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from livespec_mcp.prompts import _load_cross_repo_guide
from livespec_mcp.state import AppState, get_mru_state, get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import compute_project_overview
from livespec_mcp.tools.indexing import compute_index_status
from livespec_mcp.workspace_param import WorkspaceNotIndexedError, WorkspaceRequiredError

_NO_WORKSPACE_HINT = (
    "Resources bind to the most recently used workspace. Call any tool with "
    "workspace='/abs/path' first (e.g. index_project), then read the resource."
)


def _resolve_state() -> AppState | None:
    # Resources have no workspace= channel — MRU is the contract. Prefer it
    # before get_state(): test harnesses monkeypatch missing-workspace to cwd,
    # which can be an unindexed parent of a group_db polyrepo.
    st = get_mru_state()
    if st is not None:
        return st
    try:
        return get_state()
    except (WorkspaceRequiredError, WorkspaceNotIndexedError, FileNotFoundError):
        return None


def _no_workspace_json() -> str:
    return json.dumps(mcp_error("No active workspace", hint=_NO_WORKSPACE_HINT))


def compute_group_view(st: AppState) -> dict[str, Any]:
    """Membership + mirrored ``xrepo-*`` Specs for the active workspace DB."""
    grouped = bool(st.settings.grouped)
    conn = st.conn
    projects = [
        {
            "id": int(r["id"]),
            "name": Path(r["root"]).name,
            "root": r["root"],
            "xrepo_spec_count": int(
                conn.execute(
                    """SELECT COUNT(*) FROM spec
                       WHERE project_id=? AND spec_id LIKE 'xrepo-%'""",
                    (r["id"],),
                ).fetchone()[0]
            ),
            "spec_count": int(
                conn.execute(
                    "SELECT COUNT(*) FROM spec WHERE project_id=?",
                    (r["id"],),
                ).fetchone()[0]
            ),
        }
        for r in conn.execute("SELECT id, root FROM project ORDER BY id")
    ]
    xrepo_ids = [
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT spec_id FROM spec
               WHERE spec_id LIKE 'xrepo-%' ORDER BY 1"""
        )
    ]
    xrepo_specs: list[dict[str, Any]] = []
    for xid in xrepo_ids:
        meta = conn.execute(
            """SELECT title, status, kind FROM spec
               WHERE spec_id=? ORDER BY project_id LIMIT 1""",
            (xid,),
        ).fetchone()
        by_project = [
            {
                "project": Path(r["root"]).name,
                "root": r["root"],
                "links": int(r["links"]),
            }
            for r in conn.execute(
                """SELECT p.root AS root, COUNT(ss.id) AS links
                   FROM spec s
                   JOIN project p ON p.id = s.project_id
                   LEFT JOIN spec_symbol ss ON ss.spec_id = s.id
                   WHERE s.spec_id=?
                   GROUP BY s.project_id
                   ORDER BY links DESC""",
                (xid,),
            )
        ]
        xrepo_specs.append(
            {
                "spec_id": xid,
                "title": meta["title"] if meta else None,
                "status": meta["status"] if meta else None,
                "kind": meta["kind"] if meta else None,
                "repos": by_project,
                "repo_count": len(by_project),
            }
        )
    return {
        "grouped": grouped,
        "group_db": str(st.settings.db_path) if grouped else None,
        "workspace": str(st.settings.workspace),
        "project_id": st.project_id,
        "projects": projects,
        "xrepo_specs": xrepo_specs,
        "counts": {
            "projects": len(projects),
            "xrepo_specs": len(xrepo_specs),
        },
        "hint": (
            None
            if grouped
            else (
                "This workspace is not using group_db. Set "
                "[workspace] group_db in .livespec.toml on each sibling repo, "
                "mirror Spec ids as xrepo-*, then re-index. "
                "Read guide://cross-repo or fetch prompt cross_repo_workflow."
            )
        ),
        "how_to": "guide://cross-repo",
    }


def register(mcp: FastMCP) -> None:
    @mcp.resource("guide://cross-repo", mime_type="text/markdown")
    def cross_repo_guide() -> str:
        """Static how-to: group_db, mirrored xrepo-* Specs, Flow Explorer."""
        return _load_cross_repo_guide()

    @mcp.resource("project://group", mime_type="application/json")
    def project_group() -> str:
        """Polyrepo membership + xrepo-* Spec rollup for the MRU workspace."""
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        return json.dumps(compute_group_view(st))

    @mcp.resource("project://overview", mime_type="application/json")
    def project_overview() -> str:
        """Tool-parity view of get_project_overview (default include_infrastructure=False)."""
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        return json.dumps(compute_project_overview(st))

    # Resource, not a tool: no query args available, so the ceiling is a flat
    # cap + truncated flag rather than cursor pagination — see the
    # `list_specs(limit=..., cursor=...)` tool for pageable access to the rest.
    _LIST_SPECS_RESOURCE_CAP = 1000

    @mcp.resource("project://specs", mime_type="application/json")
    def list_specs() -> str:
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        pid = st.project_id
        rows = [
            dict(r)
            for r in st.conn.execute(
                """SELECT s.spec_id, s.kind, s.title, s.status, s.priority, m.name as module
                   FROM spec s LEFT JOIN module m ON m.id=s.module_id
                   WHERE s.project_id=? ORDER BY s.spec_id""",
                (pid,),
            )
        ]
        total = len(rows)
        page = rows[:_LIST_SPECS_RESOURCE_CAP]
        return json.dumps({
            "specs": page,
            "total": total,
            "truncated": total > len(page),
        })

    @mcp.resource("project://specs/{spec_id}", mime_type="application/json")
    def spec(spec_id: str) -> str:
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        pid = st.project_id
        row = st.conn.execute(
            """SELECT s.*, m.name as module FROM spec s LEFT JOIN module m ON m.id=s.module_id
               WHERE s.project_id=? AND s.spec_id=?""",
            (pid, spec_id),
        ).fetchone()
        if not row:
            return json.dumps(mcp_error(f"Spec '{spec_id}' not found"))
        symbols = [
            dict(r)
            for r in st.conn.execute(
                """SELECT s2.qualified_name, f.path, ss.relation, ss.confidence
                   FROM spec_symbol ss JOIN symbol s2 ON s2.id=ss.symbol_id
                   JOIN file f ON f.id=s2.file_id WHERE ss.spec_id=?""",
                (row["id"],),
            )
        ]
        out = dict(row)
        out["implementations"] = symbols
        return json.dumps(out)

    @mcp.resource("project://files/{path*}", mime_type="application/json")
    def file_view(path: str) -> str:
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        pid = st.project_id
        row = st.conn.execute(
            "SELECT * FROM file WHERE project_id=? AND path=?", (pid, path)
        ).fetchone()
        if not row:
            return json.dumps(mcp_error(f"File '{path}' not indexed"))
        symbols = [
            dict(r)
            for r in st.conn.execute(
                """SELECT name, qualified_name, kind, start_line, end_line FROM symbol
                   WHERE file_id=? ORDER BY start_line""",
                (row["id"],),
            )
        ]
        return json.dumps({**dict(row), "symbols": symbols})

    @mcp.resource("project://symbols/{qname*}", mime_type="application/json")
    def symbol_view(qname: str) -> str:
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        pid = st.project_id
        row = st.conn.execute(
            """SELECT s.*, f.path FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
            (pid, qname),
        ).fetchone()
        if not row:
            return json.dumps(mcp_error(f"Symbol '{qname}' not found"))
        return json.dumps(dict(row))

    @mcp.resource("doc://symbol/{qname*}", mime_type="text/markdown")
    def doc_symbol(qname: str) -> str:
        st = _resolve_state()
        if st is None:
            return f"# No active workspace\n\n{_NO_WORKSPACE_HINT}"
        pid = st.project_id
        row = st.conn.execute(
            """SELECT content FROM doc
               WHERE project_id=? AND target_type='symbol' AND target_key=?""",
            (pid, qname),
        ).fetchone()
        if not row:
            return f"# No doc for `{qname}`\n\nRun `generate_docs(target_type='symbol', ...)` first."
        return row["content"]

    @mcp.resource("doc://spec/{spec_id}", mime_type="text/markdown")
    def doc_spec(spec_id: str) -> str:
        st = _resolve_state()
        if st is None:
            return f"# No active workspace\n\n{_NO_WORKSPACE_HINT}"
        pid = st.project_id
        row = st.conn.execute(
            """SELECT content FROM doc
               WHERE project_id=? AND target_type='spec' AND target_key=?""",
            (pid, spec_id),
        ).fetchone()
        if not row:
            return f"# No doc for `{spec_id}`\n\nRun `generate_docs(target_type='spec', ...)` first."
        return row["content"]

    @mcp.resource("code://symbol/{qname*}", mime_type="text/plain")
    def code_symbol(qname: str) -> str:
        """Raw source body of a symbol (no JSON wrapping). Drop into context."""
        st = _resolve_state()
        if st is None:
            return f"# No active workspace\n\n{_NO_WORKSPACE_HINT}"
        pid = st.project_id
        row = st.conn.execute(
            """SELECT s.start_line, s.end_line, f.path FROM symbol s
               JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
            (pid, qname),
        ).fetchone()
        if not row:
            return f"# Symbol '{qname}' not found in this workspace"
        try:
            fp = st.settings.workspace / row["path"]
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(int(row["start_line"]) - 1, 0)
            end = min(int(row["end_line"]), len(lines))
            return "\n".join(lines[start:end])
        except OSError as e:
            return f"# Error reading source: {e}"

    @mcp.resource("project://index/status", mime_type="application/json")
    def index_status() -> str:
        """Index status payload — canonical surface.

        v0.9 P6: replaced the deprecated `get_index_status` tool wrapper.
        Returns `{workspace, project_id, files, symbols, edges,
        specs, last_run}`.
        """
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        return json.dumps(compute_index_status(st))
