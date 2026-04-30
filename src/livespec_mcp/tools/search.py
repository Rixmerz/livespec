"""Search + RAG tools.

`search` is hybrid: FTS5 keyword (always) plus vector lane (when fastembed +
sqlite-vec are installed). `rebuild_chunks` and `embed_pending` give explicit
control over the RAG index. Chunking happens against the latest indexed snapshot.
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
    ) -> dict[str, Any]:
        """Hybrid search over the indexed corpus.

        Lane 1 = SQLite FTS5 over chunks (always available).
        Lane 2 = vector search via sqlite-vec + fastembed (when installed).
        Both lanes are merged with Reciprocal Rank Fusion.

        Run `rebuild_chunks` first if `search` returns empty after a fresh index.
        """
        st = get_state()
        pid = st.project_id
        # Auto-chunk on first search if the project has no chunks yet
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
    def rebuild_chunks() -> dict[str, Any]:
        """(Re)chunk every indexed symbol and RF for FTS + embeddings.

        Idempotent: wipes prior chunks for the project and rebuilds. Cheap (no
        network calls). Run after `index_project` or after creating/editing RFs.
        """
        st = get_state()
        pid = st.project_id
        with st.lock():
            stats = rag.rebuild_chunks(st.conn, pid)
        total = st.conn.execute(
            "SELECT COUNT(*) c FROM chunk WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        return {**stats, "chunks_total": int(total)}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def embed_pending() -> dict[str, Any]:
        """Run fastembed over chunks that don't have a vector yet.

        Requires `pip install -e .[embeddings]`. First run downloads the two
        models (~600MB) into `.mcp-docs/models/`. Skips silently if extras
        are missing — `search` still works via FTS5.
        """
        st = get_state()
        pid = st.project_id
        with st.lock():
            stats = rag.embed_pending(st.conn, pid)
        return stats
