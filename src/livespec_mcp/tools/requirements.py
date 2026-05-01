"""RF tools: CRUD + linking + implementation lookup.

P1.2 consolidation: `suggest_rf_links` removed. To get implementation
candidates for an RF, call `search(query=<rf.title + rf.description>,
scope='code')` directly — the agent can then post-filter and call
`link_requirement_to_code` for each accepted candidate.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from pathlib import Path

from livespec_mcp.domain.matcher import scan_annotations
from livespec_mcp.domain.md_rfs import parse_rfs_markdown
from livespec_mcp.state import get_state
from livespec_mcp.tools.analysis import symbol_not_found_error


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
            return symbol_not_found_error(st.conn, pid, symbol_qname)
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
    def import_requirements_from_markdown(
        path: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Bulk-create / update RFs from a Markdown spec file.

        Format expected: `## RF-NNN: Title` headers, with `**Prioridad:** alta`
        and `**Módulo:** auth` metadata lines (Spanish or English variants).
        Idempotent: re-import updates existing RFs in place rather than duplicating.

        Path is resolved relative to the workspace root if not absolute.
        """
        st = get_state(workspace)
        pid = st.project_id
        p = Path(path)
        if not p.is_absolute():
            p = st.settings.workspace / path
        if not p.exists():
            return {"error": f"file not found: {p}", "isError": True}
        text = p.read_text(encoding="utf-8", errors="replace")
        parsed = parse_rfs_markdown(text)
        created = 0
        updated = 0
        for prf in parsed:
            module_id = None
            if prf.module:
                row = st.conn.execute(
                    "SELECT id FROM module WHERE project_id=? AND name=?", (pid, prf.module)
                ).fetchone()
                if row:
                    module_id = int(row["id"])
                else:
                    cur = st.conn.execute(
                        "INSERT INTO module(project_id, name) VALUES(?,?)", (pid, prf.module)
                    )
                    module_id = int(cur.lastrowid)
            existing = st.conn.execute(
                "SELECT id FROM rf WHERE project_id=? AND rf_id=?", (pid, prf.rf_id)
            ).fetchone()
            if existing:
                st.conn.execute(
                    """UPDATE rf SET title=?, description=?, status=?, priority=?,
                       module_id=?, updated_at=datetime('now') WHERE id=?""",
                    (prf.title, prf.description, prf.status, prf.priority, module_id, existing["id"]),
                )
                updated += 1
            else:
                st.conn.execute(
                    """INSERT INTO rf(project_id, rf_id, title, description, module_id, status, priority)
                       VALUES(?,?,?,?,?,?,?)""",
                    (pid, prf.rf_id, prf.title, prf.description, module_id, prf.status, prf.priority),
                )
                created += 1
        return {
            "source": str(p),
            "parsed": len(parsed),
            "created": created,
            "updated": updated,
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": True})
    def delete_requirement(rf_id: str, workspace: str | None = None) -> dict[str, Any]:
        """Permanently delete an RF and its rf_symbol links (cascade).

        Idempotent: deleting an unknown rf_id returns deleted=False without error.
        """
        st = get_state(workspace)
        pid = st.project_id
        cur = st.conn.execute(
            "DELETE FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
        )
        return {"rf_id": rf_id, "deleted": cur.rowcount > 0}

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

    # ---------- v0.5 P2: RF dependency graph ----------

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def link_requirements(
        parent_rf_id: str,
        child_rf_id: str,
        kind: Literal["requires", "extends", "conflicts"] = "requires",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Declare that one RF depends on another.

        Semantics:
        - `requires`  : parent needs child to be implemented first (the
                        common case — RF-API needs RF-AUTH).
        - `extends`   : parent specializes child's behavior (RF-EXPORT-PDF
                        extends RF-EXPORT).
        - `conflicts` : the two cannot both be active (mutually exclusive
                        rollouts).

        Idempotent on (parent, child, kind). Cycles are rejected at insert
        time: if adding the edge would create parent → … → parent in the
        forward closure, the call returns isError=True without writing.

        Self-loops are rejected by the schema CHECK constraint.
        """
        st = get_state(workspace)
        pid = st.project_id
        if parent_rf_id == child_rf_id:
            return {"error": "An RF cannot depend on itself", "isError": True}
        parent = st.conn.execute(
            "SELECT id, rf_id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, parent_rf_id),
        ).fetchone()
        child = st.conn.execute(
            "SELECT id, rf_id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, child_rf_id),
        ).fetchone()
        if not parent:
            return {"error": f"RF '{parent_rf_id}' not found", "isError": True}
        if not child:
            return {"error": f"RF '{child_rf_id}' not found", "isError": True}

        # Cycle check: would adding parent->child create a path child->parent?
        # That happens if the descendant set of `child` already contains `parent`.
        descendants: set[int] = set()
        frontier = [int(child["id"])]
        while frontier:
            current = frontier.pop()
            for r in st.conn.execute(
                "SELECT child_rf_id FROM rf_dependency WHERE parent_rf_id=?",
                (current,),
            ):
                cid = int(r["child_rf_id"])
                if cid in descendants:
                    continue
                descendants.add(cid)
                if cid == int(parent["id"]):
                    return {
                        "error": (
                            f"would create a cycle: {child_rf_id} already "
                            f"transitively depends on {parent_rf_id}"
                        ),
                        "isError": True,
                    }
                frontier.append(cid)

        cur = st.conn.execute(
            """INSERT OR IGNORE INTO rf_dependency(parent_rf_id, child_rf_id, kind)
               VALUES(?,?,?)""",
            (int(parent["id"]), int(child["id"]), kind),
        )
        return {
            "linked": cur.rowcount > 0,
            "parent": parent_rf_id,
            "child": child_rf_id,
            "kind": kind,
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": True})
    def unlink_requirements(
        parent_rf_id: str,
        child_rf_id: str,
        kind: Literal["requires", "extends", "conflicts"] | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Remove an RF dependency edge. If `kind` is None, drops every edge
        between the pair regardless of kind. Idempotent."""
        st = get_state(workspace)
        pid = st.project_id
        parent = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, parent_rf_id),
        ).fetchone()
        child = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, child_rf_id),
        ).fetchone()
        if not parent or not child:
            return {"unlinked": 0, "parent": parent_rf_id, "child": child_rf_id}
        if kind is None:
            cur = st.conn.execute(
                "DELETE FROM rf_dependency WHERE parent_rf_id=? AND child_rf_id=?",
                (int(parent["id"]), int(child["id"])),
            )
        else:
            cur = st.conn.execute(
                """DELETE FROM rf_dependency WHERE parent_rf_id=? AND child_rf_id=?
                   AND kind=?""",
                (int(parent["id"]), int(child["id"]), kind),
            )
        return {
            "unlinked": cur.rowcount,
            "parent": parent_rf_id,
            "child": child_rf_id,
            "kind": kind,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_requirement_dependencies(
        rf_id: str,
        direction: Literal["forward", "backward", "both"] = "both",
        max_depth: int = 5,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Walk the RF dependency graph from a given RF.

        - forward:  what does this RF depend on (children, transitively)?
        - backward: what depends on this RF (parents, transitively)?
        - both:     union of both.

        Returns the visited RF metadata + the edges traversed."""
        st = get_state(workspace)
        pid = st.project_id
        root = st.conn.execute(
            "SELECT id, rf_id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, rf_id),
        ).fetchone()
        if not root:
            return {"error": f"RF '{rf_id}' not found", "isError": True}
        root_id = int(root["id"])

        visited: set[int] = {root_id}
        edges: list[tuple[int, int, str]] = []

        def walk(start: int, forward: bool) -> None:
            frontier = [(start, 0)]
            while frontier:
                node, depth = frontier.pop()
                if depth >= max_depth:
                    continue
                if forward:
                    rows = st.conn.execute(
                        """SELECT parent_rf_id, child_rf_id, kind FROM rf_dependency
                           WHERE parent_rf_id=?""",
                        (node,),
                    )
                else:
                    rows = st.conn.execute(
                        """SELECT parent_rf_id, child_rf_id, kind FROM rf_dependency
                           WHERE child_rf_id=?""",
                        (node,),
                    )
                for r in rows:
                    edges.append((int(r["parent_rf_id"]), int(r["child_rf_id"]), r["kind"]))
                    next_id = int(r["child_rf_id"]) if forward else int(r["parent_rf_id"])
                    if next_id not in visited:
                        visited.add(next_id)
                        frontier.append((next_id, depth + 1))

        if direction in ("forward", "both"):
            walk(root_id, forward=True)
        if direction in ("backward", "both"):
            walk(root_id, forward=False)

        # Resolve metadata for visited RFs
        if visited:
            placeholders = ",".join("?" * len(visited))
            rf_meta = {
                int(r["id"]): {
                    "rf_id": r["rf_id"],
                    "title": r["title"],
                    "status": r["status"],
                    "priority": r["priority"],
                }
                for r in st.conn.execute(
                    f"SELECT id, rf_id, title, status, priority FROM rf WHERE id IN ({placeholders})",
                    list(visited),
                )
            }
        else:
            rf_meta = {}

        # Dedupe edges
        edge_keys: set[tuple[int, int, str]] = set()
        edge_payload: list[dict[str, Any]] = []
        for p, c, k in edges:
            key = (p, c, k)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edge_payload.append({
                "parent": rf_meta.get(p, {}).get("rf_id"),
                "child": rf_meta.get(c, {}).get("rf_id"),
                "kind": k,
            })

        return {
            "root": rf_id,
            "direction": direction,
            "nodes": list(rf_meta.values()),
            "edges": edge_payload,
        }
