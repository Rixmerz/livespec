"""Doc generation tools.

P1.2 consolidation:
- `generate_docs_for_symbol` + `generate_docs_for_requirement` merged into
  `generate_docs(target_type, identifier, ...)`.
- `detect_stale_docs` merged into `list_docs(only_stale=True)`.
- All tools accept optional `workspace` for multi-tenant operation.

Generation supports two modes (host-agnostic):
  1. caller_supplied: pass `content=...`, tool persists.
  2. sampling: omit `content`, tool calls `ctx.sample()`.
  3. needs_caller_content: no content + sampling unsupported, tool returns
     the prompt and source so the caller can write and retry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastmcp import Context, FastMCP

from livespec_mcp.state import get_state


def _persist_doc(
    st,
    target_type: str,
    target_key: str,
    content: str,
    body_hash: str | None,
    signature_hash: str | None = None,
) -> None:
    pid = st.project_id
    st.conn.execute(
        """INSERT INTO doc(project_id, target_type, target_key, content,
                            body_hash_at_write, signature_hash_at_write)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(project_id, target_type, target_key)
           DO UPDATE SET content=excluded.content,
                         body_hash_at_write=excluded.body_hash_at_write,
                         signature_hash_at_write=excluded.signature_hash_at_write,
                         generated_at=datetime('now')""",
        (pid, target_type, target_key, content, body_hash, signature_hash),
    )
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
    async def generate_docs(
        target_type: Literal["symbol", "requirement"],
        identifier: str,
        ctx: Context,
        content: str | None = None,
        max_tokens: int = 600,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Persist Markdown docs for a symbol or RF.

        Modes:
        - caller_supplied: pass `content=...`, tool persists immediately.
        - sampling: omit `content`, tool calls `ctx.sample()` (Cursor/Desktop).
        - needs_caller_content: sampling unsupported AND no content → tool
          returns the prompt + source so the caller can write and retry.

        Mirrors result to `.mcp-docs/docs/<target_type>/<key>.md`.
        """
        st = get_state(workspace)
        pid = st.project_id

        if target_type == "symbol":
            sym = st.conn.execute(
                """SELECT s.id, s.name, s.qualified_name, s.kind, s.signature, s.signature_hash,
                          s.docstring, s.start_line, s.end_line, s.body_hash, f.path AS file_path
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

            target_key = sym_d["qualified_name"]
            body_hash = sym_d["body_hash"]
            sig_hash = sym_d.get("signature_hash")

            if content is not None:
                _persist_doc(st, "symbol", target_key, content, body_hash, sig_hash)
                return {
                    "target": target_key,
                    "saved_to": f"doc://symbol/{target_key}",
                    "length": len(content),
                    "mode": "caller_supplied",
                }

            prompt = _symbol_prompt(sym_d, source)
            try:
                response = await ctx.sample(prompt, max_tokens=max_tokens)
            except Exception as e:
                return {
                    "mode": "needs_caller_content",
                    "reason": f"sampling unavailable: {e}",
                    "instruction": (
                        "Write Markdown docs for this symbol and re-call this tool "
                        "with `content` set."
                    ),
                    "prompt": prompt,
                    "source": source,
                    "target": target_key,
                    "body_hash": body_hash,
                }
            content_str = response.text if hasattr(response, "text") else str(response)
            _persist_doc(st, "symbol", target_key, content_str, body_hash, sig_hash)
            return {
                "target": target_key,
                "saved_to": f"doc://symbol/{target_key}",
                "length": len(content_str),
                "mode": "sampling",
            }

        # target_type == "requirement"
        rf = st.conn.execute(
            "SELECT * FROM rf WHERE project_id=? AND rf_id=?", (pid, identifier)
        ).fetchone()
        if not rf:
            return {"error": f"RF '{identifier}' not found", "isError": True}
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
        target_key = rf_d["rf_id"]

        if content is not None:
            _persist_doc(st, "requirement", target_key, content, None, None)
            return {
                "target": target_key,
                "saved_to": f"doc://requirement/{target_key}",
                "length": len(content),
                "mode": "caller_supplied",
            }

        prompt = _rf_prompt(rf_d, symbols)
        try:
            response = await ctx.sample(prompt, max_tokens=max_tokens)
        except Exception as e:
            return {
                "mode": "needs_caller_content",
                "reason": f"sampling unavailable: {e}",
                "instruction": (
                    "Write the RF spec in Markdown and re-call this tool with `content` set."
                ),
                "prompt": prompt,
                "linked_symbols": symbols,
                "target": target_key,
            }
        content_str = response.text if hasattr(response, "text") else str(response)
        _persist_doc(st, "requirement", target_key, content_str, None, None)
        return {
            "target": target_key,
            "saved_to": f"doc://requirement/{target_key}",
            "length": len(content_str),
            "mode": "sampling",
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def list_docs(
        target_type: Literal["symbol", "requirement", "all"] = "all",
        only_stale: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """List generated docs.

        only_stale=True returns only docs whose source has drifted since they
        were generated. For symbols, drift triggers on body_hash OR
        signature_hash mismatch (P2.4). For RFs, drift triggers when the RF
        was updated after the doc was written.
        """
        st = get_state(workspace)
        pid = st.project_id

        if not only_stale:
            sql = ["SELECT target_type, target_key, generated_at FROM doc WHERE project_id=?"]
            args: list = [pid]
            if target_type != "all":
                sql.append("AND target_type=?")
                args.append(target_type)
            sql.append("ORDER BY generated_at DESC")
            rows = [dict(r) for r in st.conn.execute(" ".join(sql), args).fetchall()]
            return {"docs": rows}

        # only_stale path
        stale: list[dict[str, Any]] = []
        if target_type in ("symbol", "all"):
            for r in st.conn.execute(
                """SELECT d.target_key, d.body_hash_at_write, d.signature_hash_at_write,
                          s.body_hash, s.signature_hash, s.qualified_name, d.generated_at
                   FROM doc d JOIN symbol s ON s.qualified_name = d.target_key
                   JOIN file f ON f.id = s.file_id
                   WHERE d.project_id=? AND f.project_id=? AND d.target_type='symbol'""",
                (pid, pid),
            ):
                drift: list[str] = []
                if r["body_hash_at_write"] != r["body_hash"]:
                    drift.append("body")
                if (
                    r["signature_hash_at_write"] is not None
                    and r["signature_hash_at_write"] != r["signature_hash"]
                ):
                    drift.append("signature")
                if drift:
                    stale.append({
                        "type": "symbol",
                        "target": r["qualified_name"],
                        "drift": "+".join(drift) + " changed",
                        "generated_at": r["generated_at"],
                    })
        if target_type in ("requirement", "all"):
            for r in st.conn.execute(
                """SELECT d.target_key, d.generated_at, r.updated_at, r.rf_id
                   FROM doc d JOIN rf r ON r.rf_id = d.target_key
                   WHERE d.project_id=? AND r.project_id=? AND d.target_type='requirement'
                     AND r.updated_at > d.generated_at""",
                (pid, pid),
            ):
                stale.append({
                    "type": "requirement",
                    "target": r["rf_id"],
                    "drift": "rf updated after doc generation",
                    "generated_at": r["generated_at"],
                })
        return {"stale": stale, "count": len(stale)}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def export_documentation(
        format: Literal["markdown", "json"] = "markdown",
        out_subdir: str = "export",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Dump the entire `doc` table to disk for sharing or static-site building."""
        st = get_state(workspace)
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
        for r in rows:
            d = out_root / r["target_type"]
            d.mkdir(parents=True, exist_ok=True)
            safe = r["target_key"].replace("/", "_")
            (d / f"{safe}.md").write_text(r["content"], encoding="utf-8")
        return {"exported": len(rows), "path": str(out_root)}
