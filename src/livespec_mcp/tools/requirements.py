"""RF tools: CRUD + linking + implementation lookup.

P1.2 consolidation: `suggest_rf_links` removed. To get implementation
candidates for an RF, call `search(query=<rf.title + rf.description>,
scope='code')` directly — the agent can then post-filter and call
`link_requirement_to_code` for each accepted candidate.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.domain.matcher import scan_annotations
from livespec_mcp.state import get_state


def _next_rf_id(conn, project_id: int) -> str:
    row = conn.execute(
        "SELECT rf_id FROM rf WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    n = 1
    if row:
        digits = "".join(c for c in row["rf_id"] if c.isdigit())
        if digits:
            n = int(digits) + 1
    return f"RF-{n:03d}"


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    def create_requirement(
        title: str,
        description: str | None = None,
        rf_id: str | None = None,
        module: str | None = None,
        priority: Literal["low", "medium", "high", "critical"] = "medium",
        status: Literal["draft", "active", "deprecated"] = "draft",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Create a Functional Requirement.

        Auto-assigns rf_id (RF-NNN) if not provided. Not idempotent — use
        `update_requirement` for changes.
        """
        st = get_state(workspace)
        pid = st.project_id
        rid = rf_id or _next_rf_id(st.conn, pid)
        module_id = None
        if module:
            r = st.conn.execute(
                "SELECT id FROM module WHERE project_id=? AND name=?", (pid, module)
            ).fetchone()
            if r:
                module_id = int(r["id"])
            else:
                cur = st.conn.execute(
                    "INSERT INTO module(project_id, name) VALUES(?,?)", (pid, module)
                )
                module_id = int(cur.lastrowid)
        cur = st.conn.execute(
            """INSERT INTO rf(project_id, rf_id, title, description, module_id, status, priority)
               VALUES(?,?,?,?,?,?,?)""",
            (pid, rid, title, description, module_id, status, priority),
        )
        return {"id": int(cur.lastrowid), "rf_id": rid, "title": title}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def update_requirement(
        rf_id: str,
        title: str | None = None,
        description: str | None = None,
        status: Literal["draft", "active", "deprecated"] | None = None,
        priority: Literal["low", "medium", "high", "critical"] | None = None,
        module: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Patch fields on an existing RF. Idempotent."""
        st = get_state(workspace)
        pid = st.project_id
        row = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
        ).fetchone()
        if not row:
            return {"error": f"RF '{rf_id}' not found", "isError": True}
        rf_pk = int(row["id"])
        sets: list[str] = []
        args: list[Any] = []
        for col, val in [("title", title), ("description", description), ("status", status), ("priority", priority)]:
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if module is not None:
            r = st.conn.execute(
                "SELECT id FROM module WHERE project_id=? AND name=?", (pid, module)
            ).fetchone()
            if not r:
                cur = st.conn.execute("INSERT INTO module(project_id, name) VALUES(?,?)", (pid, module))
                module_id = int(cur.lastrowid)
            else:
                module_id = int(r["id"])
            sets.append("module_id=?")
            args.append(module_id)
        sets.append("updated_at=datetime('now')")
        args.append(rf_pk)
        st.conn.execute(f"UPDATE rf SET {', '.join(sets)} WHERE id=?", args)
        return {"rf_id": rf_id, "updated": True}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def list_requirements(
        status: str | None = None,
        module: str | None = None,
        priority: str | None = None,
        has_implementation: bool | None = None,
        limit: int = 100,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """List RFs with filters. Returns rf_id, title, status, priority, module, link_count."""
        st = get_state(workspace)
        pid = st.project_id
        sql = [
            """SELECT r.id, r.rf_id, r.title, r.description, r.status, r.priority,
                      m.name AS module,
                      (SELECT COUNT(*) FROM rf_symbol rs WHERE rs.rf_id=r.id) AS link_count
               FROM rf r LEFT JOIN module m ON m.id=r.module_id
               WHERE r.project_id=?"""
        ]
        args: list[Any] = [pid]
        if status:
            sql.append("AND r.status=?"); args.append(status)
        if priority:
            sql.append("AND r.priority=?"); args.append(priority)
        if module:
            sql.append("AND m.name=?"); args.append(module)
        sql.append("ORDER BY r.rf_id LIMIT ?"); args.append(limit)
        rows = [dict(r) for r in st.conn.execute(" ".join(sql), args).fetchall()]
        if has_implementation is not None:
            rows = [r for r in rows if (r["link_count"] > 0) == has_implementation]
        return {"requirements": rows}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def link_requirement_to_code(
        rf_id: str,
        symbol_qname: str,
        relation: Literal["implements", "tests", "references"] = "implements",
        confidence: float = 1.0,
        source: Literal["manual", "annotation", "embedding", "llm"] = "manual",
        unlink: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Link (or unlink) a symbol to an RF. unlink=True removes the link."""
        st = get_state(workspace)
        pid = st.project_id
        rf = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
        ).fetchone()
        if not rf:
            return {"error": f"RF '{rf_id}' not found", "isError": True}
        sym = st.conn.execute(
            """SELECT s.id FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
            (pid, symbol_qname),
        ).fetchone()
        if not sym:
            return {"error": f"Symbol '{symbol_qname}' not found", "isError": True}
        if unlink:
            st.conn.execute(
                "DELETE FROM rf_symbol WHERE rf_id=? AND symbol_id=? AND relation=?",
                (rf["id"], sym["id"], relation),
            )
            return {"unlinked": True, "rf_id": rf_id, "symbol": symbol_qname}
        st.conn.execute(
            """INSERT OR REPLACE INTO rf_symbol(rf_id, symbol_id, relation, confidence, source)
               VALUES(?,?,?,?,?)""",
            (rf["id"], sym["id"], relation, confidence, source),
        )
        return {"linked": True, "rf_id": rf_id, "symbol": symbol_qname, "relation": relation}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_requirement_implementation(
        rf_id: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """What code implements an RF: list of symbols + files + coverage signals."""
        st = get_state(workspace)
        pid = st.project_id
        rf = st.conn.execute(
            """SELECT r.*, m.name AS module FROM rf r
               LEFT JOIN module m ON m.id=r.module_id
               WHERE r.project_id=? AND r.rf_id=?""",
            (pid, rf_id),
        ).fetchone()
        if not rf:
            return {"error": f"RF '{rf_id}' not found", "isError": True}
        rows = st.conn.execute(
            """SELECT s.qualified_name, s.kind, s.signature, s.start_line, s.end_line,
                      f.path, rs.relation, rs.confidence, rs.source
               FROM rf_symbol rs JOIN symbol s ON s.id=rs.symbol_id
               JOIN file f ON f.id=s.file_id
               WHERE rs.rf_id=?
               ORDER BY rs.confidence DESC, s.qualified_name""",
            (rf["id"],),
        ).fetchall()
        files = sorted({r["path"] for r in rows})
        return {
            "rf": {
                "rf_id": rf["rf_id"],
                "title": rf["title"],
                "description": rf["description"],
                "status": rf["status"],
                "priority": rf["priority"],
                "module": rf["module"],
            },
            "symbols": [dict(r) for r in rows],
            "files": files,
            "coverage": {"symbol_count": len(rows), "file_count": len(files)},
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def scan_rf_annotations(workspace: str | None = None) -> dict[str, Any]:
        """Re-scan all symbol docstrings for RF annotations and (re)link them.

        Two-level matcher (P1.4):
        - Explicit prefix `@rf:RF-001` / `@implements:RF-001` -> confidence 1.0
        - Verb-anchored `implements RF-001` (with negation guard) -> 0.7
        Idempotent: skips existing links.
        """
        st = get_state(workspace)
        pid = st.project_id
        n = scan_annotations(st.conn, pid)
        return {"links_created": n}
