"""Hybrid search tool: FTS5 + optional sqlite-vec via Reciprocal Rank Fusion.

Wired to the orphan RAG layer (domain/rag.py) so an agent can answer
"where does the code talk about X?" without exact symbol-name matches.

FTS5 lane is always available (sqlite ships it). Vector lane activates
when fastembed + sqlite-vec are installed AND embeddings have been
populated via `index_project(embed=True)` or `embed_pending`.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.domain.rag import (
    embed_pending,
    have_embeddings,
    have_sqlite_vec,
    hybrid_search,
)
from livespec_mcp.state import get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def search(
        query: str,
        scope: Literal["all", "code", "specs"] = "all",
        limit: int = 20,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Hybrid retrieval over chunked symbols + Specs.

        FTS5 keyword lane always runs. When embeddings are available,
        a vector lane is fused with Reciprocal Rank Fusion (k=60).
        Run `index_project(embed=True)` once to populate vectors;
        subsequent calls reuse them.

        scope: 'all' | 'code' | 'specs'""" + WORKSPACE_DOCSTRING_NOTE
        if not query or not query.strip():
            return mcp_error("query is required", hint="pass a non-empty query string")
        if limit < 1 or limit > 200:
            return mcp_error("limit must be between 1 and 200")
        st = get_state(workspace)
        results = hybrid_search(st.conn, st.project_id, query, scope, limit)
        return {
            "query": query,
            "scope": scope,
            "results": results,
            "count": len(results),
            "lanes": {
                "fts5": True,
                "vector": have_embeddings() and have_sqlite_vec(st.conn),
            },
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def embed_chunks(workspace: Workspace | None = None) -> dict[str, Any]:
        """Populate vector embeddings for any unembedded chunks.

        Requires the [embeddings] extra (fastembed + sqlite-vec). First
        run downloads ~1.6 GB of ONNX weights. No-op if extras missing
        or all chunks already embedded.
        """
        st = get_state(workspace)
        if not have_embeddings():
            return mcp_error(
                "fastembed not installed",
                hint=(
                    "install embeddings extra: `uv pip install -e \".[embeddings]\"` "
                    "then reconnect MCP (/mcp) and re-run embed_chunks"
                ),
            )
        if not have_sqlite_vec(st.conn):
            return mcp_error(
                "sqlite-vec not loadable",
                hint=(
                    "install embeddings extra: `uv pip install -e \".[embeddings]\"` "
                    "then reconnect MCP (/mcp) and re-run embed_chunks"
                ),
            )
        with st.lock():
            stats = embed_pending(st.conn, st.project_id)
        return {"workspace": str(st.settings.workspace), **stats}
