"""MCP resources: project:// addressable views."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from livespec_mcp.state import get_state
from livespec_mcp.tools.analysis import compute_project_overview
from livespec_mcp.tools.indexing import compute_index_status


def register(mcp: FastMCP) -> None:
    @mcp.resource("project://overview", mime_type="application/json")
    def project_overview() -> str:
        """Tool-parity view of get_project_overview (default include_infrastructure=False)."""
        return json.dumps(compute_project_overview(get_state()))

    @mcp.resource("project://requirements", mime_type="application/json")
    def list_requirements() -> str:
        st = get_state()
        pid = st.project_id
        rows = [
            dict(r)
            for r in st.conn.execute(
                """SELECT r.rf_id, r.title, r.status, r.priority, m.name as module
                   FROM rf r LEFT JOIN module m ON m.id=r.module_id
                   WHERE r.project_id=? ORDER BY r.rf_id""",
                (pid,),
            )
        ]
        return json.dumps({"requirements": rows})

    @mcp.resource("project://requirements/{rf_id}", mime_type="application/json")
    def requirement(rf_id: str) -> str:
        st = get_state()
        pid = st.project_id
        row = st.conn.execute(
            """SELECT r.*, m.name as module FROM rf r LEFT JOIN module m ON m.id=r.module_id
               WHERE r.project_id=? AND r.rf_id=?""",
            (pid, rf_id),
        ).fetchone()
        if not row:
            return json.dumps({"error": f"RF '{rf_id}' not found"})
        symbols = [
            dict(r)
            for r in st.conn.execute(
                """SELECT s.qualified_name, f.path, rs.relation, rs.confidence
                   FROM rf_symbol rs JOIN symbol s ON s.id=rs.symbol_id
                   JOIN file f ON f.id=s.file_id WHERE rs.rf_id=?""",
                (row["id"],),
            )
        ]
        out = dict(row)
        out["implementations"] = symbols
        return json.dumps(out)

    @mcp.resource("project://files/{path*}", mime_type="application/json")
    def file_view(path: str) -> str:
        st = get_state()
        pid = st.project_id
        row = st.conn.execute(
            "SELECT * FROM file WHERE project_id=? AND path=?", (pid, path)
        ).fetchone()
        if not row:
            return json.dumps({"error": f"File '{path}' not indexed"})
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
        st = get_state()
        pid = st.project_id
        row = st.conn.execute(
            """SELECT s.*, f.path FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
            (pid, qname),
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Symbol '{qname}' not found"})
        return json.dumps(dict(row))

    @mcp.resource("doc://symbol/{qname*}", mime_type="text/markdown")
    def doc_symbol(qname: str) -> str:
        st = get_state()
        pid = st.project_id
        row = st.conn.execute(
            """SELECT content FROM doc
               WHERE project_id=? AND target_type='symbol' AND target_key=?""",
            (pid, qname),
        ).fetchone()
        if not row:
            return f"# No doc for `{qname}`\n\nRun `generate_docs_for_symbol` first."
        return row["content"]

    @mcp.resource("doc://requirement/{rf_id}", mime_type="text/markdown")
    def doc_requirement(rf_id: str) -> str:
        st = get_state()
        pid = st.project_id
        row = st.conn.execute(
            """SELECT content FROM doc
               WHERE project_id=? AND target_type='requirement' AND target_key=?""",
            (pid, rf_id),
        ).fetchone()
        if not row:
            return f"# No doc for `{rf_id}`\n\nRun `generate_docs_for_requirement` first."
        return row["content"]

    @mcp.resource("code://symbol/{qname*}", mime_type="text/plain")
    def code_symbol(qname: str) -> str:
        """Raw source body of a symbol (no JSON wrapping). Drop into context."""
        st = get_state()
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
        """Tool-parity view of get_index_status."""
        return json.dumps(compute_index_status(get_state()))
