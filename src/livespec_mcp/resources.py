"""MCP resources: project:// addressable views.

Canonical URI scheme (v0.20):

| URI | MIME | Purpose |
|-----|------|---------|
| ``project://overview`` | JSON | Project overview (PageRank spine) |
| ``project://index/status`` | JSON | Index stats |
| ``project://specs`` | JSON | All Specs |
| ``project://specs/{spec_id}`` | JSON | Spec + implementations |
| ``project://files/{path}`` | JSON | Indexed file + symbols |
| ``project://symbols/{qname}`` | JSON | Symbol metadata |
| ``doc://symbol/{qname}`` | markdown | Generated symbol doc |
| ``doc://spec/{spec_id}`` | markdown | Generated Spec doc |
| ``code://symbol/{qname}`` | plain | Raw symbol source slice |

Legacy alias ``livespec://…`` is **not** registered — use ``project://`` /
``doc://`` / ``code://`` as above. External docs may refer to the product as
"livespec" but resource URIs stay ``project://`` for MCP compatibility.

Workspace resolution (v0.14): resource URIs have no ``workspace`` parameter
channel, so resources bind to the **most recently used** workspace (the one
the last tool call touched). Before any tool call there is nothing to bind
to — JSON resources then return an `mcp_error`-shaped payload with a hint,
text resources a one-line explanation.

Error shape (v0.14, closes the v0.6 P4 contract gap): JSON resources use
``tools._errors.mcp_error`` for every error payload. Text resources
(text/markdown, text/plain) stay human-readable text — a JSON error blob
inside a markdown document would be worse than a sentence.
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from livespec_mcp.state import AppState, get_mru_state, get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import compute_project_overview
from livespec_mcp.tools.indexing import compute_index_status
from livespec_mcp.workspace_param import WorkspaceRequiredError

_NO_WORKSPACE_HINT = (
    "Resources bind to the most recently used workspace. Call any tool with "
    "workspace='/abs/path' first (e.g. index_project), then read the resource."
)


def _resolve_state() -> AppState | None:
    try:
        return get_state()
    except WorkspaceRequiredError:
        return get_mru_state()


def _no_workspace_json() -> str:
    return json.dumps(mcp_error("No active workspace", hint=_NO_WORKSPACE_HINT))


def register(mcp: FastMCP) -> None:
    @mcp.resource("project://overview", mime_type="application/json")
    def project_overview() -> str:
        """Tool-parity view of get_project_overview (default include_infrastructure=False)."""
        st = _resolve_state()
        if st is None:
            return _no_workspace_json()
        return json.dumps(compute_project_overview(st))

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
        return json.dumps({"specs": rows})

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
            return f"# No doc for `{qname}`\n\nRun `generate_docs_for_symbol` first."
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
            return f"# No doc for `{spec_id}`\n\nRun `generate_docs_for_spec` first."
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
