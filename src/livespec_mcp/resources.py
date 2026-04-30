"""MCP resources: project:// addressable views."""

from __future__ import annotations

import json

from fastmcp import FastMCP

from livespec_mcp.state import get_state


def register(mcp: FastMCP) -> None:
    @mcp.resource("project://overview", mime_type="application/json")
    def project_overview() -> str:
        st = get_state()
        pid = st.project_id
        files = st.conn.execute("SELECT COUNT(*) c FROM file WHERE project_id=?", (pid,)).fetchone()["c"]
        syms = st.conn.execute(
            "SELECT COUNT(*) c FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?",
            (pid,),
        ).fetchone()["c"]
        rfs = st.conn.execute("SELECT COUNT(*) c FROM rf WHERE project_id=?", (pid,)).fetchone()["c"]
        return json.dumps({
            "workspace": str(st.settings.workspace),
            "files": int(files),
            "symbols": int(syms),
            "requirements": int(rfs),
        })

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

    @mcp.resource("project://index/status", mime_type="application/json")
    def index_status() -> str:
        st = get_state()
        pid = st.project_id
        last = st.conn.execute(
            "SELECT * FROM index_run WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)
        ).fetchone()
        return json.dumps({"last_run": dict(last) if last else None})
