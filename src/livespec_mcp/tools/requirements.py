"""RF tools: CRUD + linking + implementation lookup."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.domain.matcher import scan_annotations
from livespec_mcp.state import get_state


def _next_rf_id(conn, project_id: int) -> str:
    row = conn.execute(
        """SELECT rf_id FROM rf WHERE project_id=? ORDER BY id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    n = 1
    if row:
        raw = row["rf_id"]
        digits = "".join(c for c in raw if c.isdigit())
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
        source: str | None = None,
    ) -> dict[str, Any]:
        """Create a Functional Requirement.

        Auto-assigns rf_id (RF-NNN) if not provided. Not idempotent — use `update_requirement`
        for changes.
        """
        st = get_state()
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
            """INSERT INTO rf(project_id, rf_id, title, description, module_id, status, priority, source)
               VALUES(?,?,?,?,?,?,?,?)""",
            (pid, rid, title, description, module_id, status, priority, source),
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
    ) -> dict[str, Any]:
        """Patch fields on an existing RF. Idempotent."""
        st = get_state()
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
    ) -> dict[str, Any]:
        """List RFs with filters. Returns rf_id, title, status, priority, module, link_count."""
        st = get_state()
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
    ) -> dict[str, Any]:
        """Vincula (o desvincula) un símbolo a un RF.

        Use unlink=True to remove the link instead of creating it.
        """
        st = get_state()
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
    def get_requirement_implementation(rf_id: str) -> dict[str, Any]:
        """What code implements an RF: list of symbols + files + coverage signals."""
        st = get_state()
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

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def suggest_rf_links(
        rf_id: str,
        limit: int = 10,
        min_score: float = 0.05,
    ) -> dict[str, Any]:
        """Propose candidate symbols that may implement an RF.

        Uses hybrid `search` over chunks with the RF title + description as the
        query. Returns ranked candidates with confidence scores. The agent (or
        a human reviewer) can confirm them with `link_requirement_to_code`.
        """
        from livespec_mcp.domain import rag

        st = get_state()
        pid = st.project_id
        rf = st.conn.execute(
            "SELECT id, rf_id, title, description FROM rf WHERE project_id=? AND rf_id=?",
            (pid, rf_id),
        ).fetchone()
        if not rf:
            return {"error": f"RF '{rf_id}' not found", "isError": True}
        query = " ".join(filter(None, [rf["title"], rf["description"]]))
        results = rag.hybrid_search(st.conn, pid, query, scope="code", limit=limit * 2)
        candidates: list[dict] = []
        seen_syms: set[int] = set()
        for r in results:
            if r["source_type"] != "symbol" or r["source_id"] is None:
                continue
            sid = int(r["source_id"])
            if sid in seen_syms:
                continue
            seen_syms.add(sid)
            sym = st.conn.execute(
                """SELECT s.qualified_name, s.kind, f.path FROM symbol s
                   JOIN file f ON f.id=s.file_id WHERE s.id=?""",
                (sid,),
            ).fetchone()
            if not sym:
                continue
            if r["score"] < min_score:
                continue
            already = st.conn.execute(
                "SELECT 1 FROM rf_symbol WHERE rf_id=? AND symbol_id=?",
                (rf["id"], sid),
            ).fetchone()
            candidates.append({
                "qualified_name": sym["qualified_name"],
                "kind": sym["kind"],
                "file_path": sym["path"],
                "score": r["score"],
                "snippet": r["snippet"],
                "already_linked": bool(already),
            })
            if len(candidates) >= limit:
                break
        return {"rf_id": rf_id, "candidates": candidates}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def scan_rf_annotations() -> dict[str, Any]:
        """Re-scan all symbol docstrings for `@rf:RF-NNN` annotations and (re)link them.

        Idempotent: skips existing links.
        """
        st = get_state()
        pid = st.project_id
        n = scan_annotations(st.conn, pid)
        return {"links_created": n}
