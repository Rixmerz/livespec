"""Doc generation tools (Phase 5).

Generation is delegated to the calling LLM via MCP `sampling` (no API key
required on the server). We persist results in the `doc` table and mirror
markdown to `.mcp-docs/docs/` so humans can read/diff them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastmcp import Context, FastMCP

from livespec_mcp.state import get_state


def _persist_doc(st, target_type: str, target_key: str, content: str, body_hash: str | None) -> None:
    pid = st.project_id
    st.conn.execute(
        """INSERT INTO doc(project_id, target_type, target_key, content, body_hash_at_write)
           VALUES(?,?,?,?,?)
           ON CONFLICT(project_id, target_type, target_key)
           DO UPDATE SET content=excluded.content,
                         body_hash_at_write=excluded.body_hash_at_write,
                         generated_at=datetime('now')""",
        (pid, target_type, target_key, content, body_hash),
    )
    # Mirror to filesystem
    safe_key = target_key.replace("/", "_").replace("..", "_")
    out_dir = st.settings.docs_dir / target_type
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{safe_key}.md").write_text(content, encoding="utf-8")


def _symbol_prompt(sym: dict, source: str) -> str:
    return (
        f"Genera documentación técnica concisa en Markdown para el siguiente símbolo. "
        f"Incluye: propósito (1-2 frases), parámetros, retorno, side effects relevantes, "
        f"ejemplo de uso si aporta. NO repitas el código completo. Idioma: español.\n\n"
        f"Qualified name: {sym['qualified_name']}\n"
        f"Kind: {sym['kind']}\n"
        f"Signature: {sym.get('signature') or '(sin firma)'}\n"
        f"File: {sym['file_path']}:{sym['start_line']}-{sym['end_line']}\n"
        f"Existing docstring: {sym.get('docstring') or '(none)'}\n\n"
        f"Source:\n```\n{source}\n```"
    )


def _rf_prompt(rf: dict, symbols: list[dict]) -> str:
    syms = "\n".join(f"- `{s['qualified_name']}` ({s['kind']}) -> {s['file_path']}" for s in symbols)
    return (
        f"Genera una ficha técnica del Requerimiento Funcional `{rf['rf_id']}` en Markdown.\n"
        f"Incluye: descripción funcional, criterios de aceptación inferidos del código, "
        f"y cómo cada símbolo lo implementa.\n\n"
        f"Título: {rf['title']}\n"
        f"Descripción: {rf.get('description') or '(none)'}\n"
        f"Status: {rf.get('status')} | Prioridad: {rf.get('priority')}\n\n"
        f"Símbolos vinculados:\n{syms or '(none)'}"
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True})
    async def generate_docs_for_symbol(
        identifier: str,
        ctx: Context,
        max_tokens: int = 600,
    ) -> dict[str, Any]:
        """Generate Markdown docs for a symbol, delegating LLM work to the client via sampling.

        The MCP client (Claude Desktop, Cursor, Claude Code) handles the actual
        completion — no server-side API key required. The result is persisted in
        SQLite and mirrored to `.mcp-docs/docs/symbol/<qname>.md`.
        """
        st = get_state()
        pid = st.project_id
        sym = st.conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.signature, s.docstring,
                      s.start_line, s.end_line, s.body_hash, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
            (pid, identifier),
        ).fetchone()
        if not sym:
            return {"error": f"Symbol '{identifier}' not found", "isError": True}
        sym_d = dict(sym)
        try:
            fp = st.settings.workspace / sym_d["file_path"]
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            source = "\n".join(
                lines[max(sym_d["start_line"] - 1, 0) : min(sym_d["end_line"], len(lines))]
            )
        except OSError:
            source = ""
        prompt = _symbol_prompt(sym_d, source)
        try:
            response = await ctx.sample(prompt, max_tokens=max_tokens)
        except Exception as e:
            return {"error": f"LLM sampling unavailable: {e}", "isError": True}
        content = response.text if hasattr(response, "text") else str(response)
        _persist_doc(st, "symbol", sym_d["qualified_name"], content, sym_d["body_hash"])
        return {
            "target": sym_d["qualified_name"],
            "saved_to": f"doc://symbol/{sym_d['qualified_name']}",
            "length": len(content),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True})
    async def generate_docs_for_requirement(
        rf_id: str,
        ctx: Context,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        """Generate a one-page RF spec from its linked symbols, via MCP sampling."""
        st = get_state()
        pid = st.project_id
        rf = st.conn.execute(
            "SELECT * FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
        ).fetchone()
        if not rf:
            return {"error": f"RF '{rf_id}' not found", "isError": True}
        rf_d = dict(rf)
        symbols = [
            dict(r)
            for r in st.conn.execute(
                """SELECT s.qualified_name, s.kind, f.path AS file_path
                   FROM rf_symbol rs JOIN symbol s ON s.id=rs.symbol_id
                   JOIN file f ON f.id=s.file_id WHERE rs.rf_id=?""",
                (rf_d["id"],),
            )
        ]
        prompt = _rf_prompt(rf_d, symbols)
        try:
            response = await ctx.sample(prompt, max_tokens=max_tokens)
        except Exception as e:
            return {"error": f"LLM sampling unavailable: {e}", "isError": True}
        content = response.text if hasattr(response, "text") else str(response)
        _persist_doc(st, "requirement", rf_d["rf_id"], content, None)
        return {
            "target": rf_d["rf_id"],
            "saved_to": f"doc://requirement/{rf_d['rf_id']}",
            "length": len(content),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def detect_stale_docs(
        target_type: Literal["symbol", "requirement", "all"] = "all",
    ) -> dict[str, Any]:
        """Find docs whose source has drifted since they were generated.

        For symbols, compares `doc.body_hash_at_write` vs the current
        `symbol.body_hash`. Stale docs should be regenerated via
        `generate_docs_for_symbol`.
        """
        st = get_state()
        pid = st.project_id
        stale: list[dict[str, Any]] = []
        if target_type in ("symbol", "all"):
            rows = st.conn.execute(
                """SELECT d.target_key, d.body_hash_at_write, s.body_hash, s.qualified_name,
                          d.generated_at
                   FROM doc d JOIN symbol s ON s.qualified_name = d.target_key
                   JOIN file f ON f.id = s.file_id
                   WHERE d.project_id=? AND f.project_id=? AND d.target_type='symbol'""",
                (pid, pid),
            ).fetchall()
            for r in rows:
                if r["body_hash_at_write"] != r["body_hash"]:
                    stale.append({
                        "type": "symbol",
                        "target": r["qualified_name"],
                        "drift": "body_hash changed",
                        "generated_at": r["generated_at"],
                    })
        if target_type in ("requirement", "all"):
            # Detect docs whose RF was updated after the doc was written
            rows = st.conn.execute(
                """SELECT d.target_key, d.generated_at, r.updated_at, r.rf_id
                   FROM doc d JOIN rf r ON r.rf_id = d.target_key
                   WHERE d.project_id=? AND r.project_id=? AND d.target_type='requirement'
                     AND r.updated_at > d.generated_at""",
                (pid, pid),
            ).fetchall()
            for r in rows:
                stale.append({
                    "type": "requirement",
                    "target": r["rf_id"],
                    "drift": "rf updated after doc generation",
                    "generated_at": r["generated_at"],
                })
        return {"stale": stale, "count": len(stale)}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def list_docs(target_type: Literal["symbol", "requirement", "all"] = "all") -> dict[str, Any]:
        """Enumerate generated docs."""
        st = get_state()
        pid = st.project_id
        sql = ["SELECT target_type, target_key, generated_at FROM doc WHERE project_id=?"]
        args: list = [pid]
        if target_type != "all":
            sql.append("AND target_type=?")
            args.append(target_type)
        sql.append("ORDER BY generated_at DESC")
        rows = [dict(r) for r in st.conn.execute(" ".join(sql), args).fetchall()]
        return {"docs": rows}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def export_documentation(
        format: Literal["markdown", "json"] = "markdown",
        out_subdir: str = "export",
    ) -> dict[str, Any]:
        """Dump the entire `doc` table to disk for sharing or static-site building.

        Markdown: one file per target. JSON: a single index file.
        Output goes under `.mcp-docs/<out_subdir>/`.
        """
        st = get_state()
        pid = st.project_id
        out_root: Path = st.settings.state_dir / out_subdir
        out_root.mkdir(parents=True, exist_ok=True)
        rows = [
            dict(r)
            for r in st.conn.execute(
                "SELECT target_type, target_key, content, generated_at FROM doc WHERE project_id=?",
                (pid,),
            )
        ]
        if format == "json":
            import json

            (out_root / "docs.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            return {"exported": len(rows), "path": str(out_root / "docs.json")}
        # markdown
        for r in rows:
            d = out_root / r["target_type"]
            d.mkdir(parents=True, exist_ok=True)
            safe = r["target_key"].replace("/", "_")
            (d / f"{safe}.md").write_text(r["content"], encoding="utf-8")
        return {"exported": len(rows), "path": str(out_root)}
