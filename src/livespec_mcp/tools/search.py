"""Search + RAG tools.

P1.2 consolidation: `embed_pending` removed — pass `embed=True` to
`rebuild_chunks` instead. Default `embed='auto'` runs embeddings when the
extras (fastembed + sqlite-vec) are available, no-op otherwise.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.domain import rag
from livespec_mcp.state import get_state


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def search(
        query: str,
        scope: Literal["all", "code", "requirements"] = "all",
        limit: int = 20,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Hybrid search over the indexed corpus.

        Lane 1 = SQLite FTS5 over chunks (always available).
        Lane 2 = vector search via sqlite-vec + fastembed (when installed).
        Both lanes are merged with Reciprocal Rank Fusion. Auto-rebuilds chunks
        if the project has none yet.
        """
        st = get_state(workspace)
        pid = st.project_id
        n_chunks = st.conn.execute(
            "SELECT COUNT(*) c FROM chunk WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        if n_chunks == 0:
            with st.lock():
                rag.rebuild_chunks(st.conn, pid)
        results = rag.hybrid_search(st.conn, pid, query, scope=scope, limit=limit)
        return {
            "query": query,
            "lanes": {
                "fts": True,
                "vector": rag.have_embeddings() and rag.have_sqlite_vec(st.conn),
            },
            "results": results,
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def rebuild_chunks(
        embed: Literal["auto", "yes", "no"] = "auto",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """(Re)chunk every indexed symbol and RF for FTS + (optional) vectors.

        embed='auto'  — run embeddings if fastembed + sqlite-vec are installed,
                       skip silently otherwise (default; safe everywhere).
        embed='yes'   — fail fast if extras are missing.
        embed='no'    — chunks only, never call fastembed.

        Idempotent: wipes prior chunks for the project and rebuilds them.
        """
        st = get_state(workspace)
        pid = st.project_id
        with st.lock():
            stats = rag.rebuild_chunks(st.conn, pid)

        embed_stats: dict[str, Any] | None = None
        if embed == "yes" or (embed == "auto" and rag.have_embeddings() and rag.have_sqlite_vec(st.conn)):
            with st.lock():
                embed_stats = rag.embed_pending(st.conn, pid)
        elif embed == "yes":
            return {"error": "embed=yes but extras missing", "isError": True}

        total = st.conn.execute(
            "SELECT COUNT(*) c FROM chunk WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        out: dict[str, Any] = {**stats, "chunks_total": int(total)}
        if embed_stats is not None:
            out["embeddings"] = embed_stats
        return out
