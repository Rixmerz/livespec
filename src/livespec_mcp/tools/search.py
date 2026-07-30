"""FTS5 keyword search over AST-aware chunks of symbols + Specs.

Dense-vector / sqlite-vec / fastembed support was removed — ``search`` is
FTS5-only. Chunks are rebuilt inside ``index_project``.

Prefer ``search`` for "code that talks about X" (keywords / phrases).
Prefer ``find_symbol`` for exact names. Prefer ``grep_in_indexed_files`` for
literal/regex line matches with ``scope_fresh``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from livespec_mcp.domain.rag import chunks_index_fresh, keyword_search
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
        """Keyword retrieval over chunked symbols + Specs (SQLite FTS5).

        Prefer this when you want "code that talks about X" without an exact
        symbol-name match (use ``find_symbol`` for names; ``grep_in_indexed_files``
        for literal/regex lines). Wrap terms in double quotes for phrase match
        (``\"create user\"``). ``scope``: 'all' | 'code' | 'specs'.

        Response includes ``query_mode`` (tokens|phrase|mixed) and
        ``index_fresh`` (chunk source files vs indexed content hashes).
        """ + WORKSPACE_DOCSTRING_NOTE
        if not query or not query.strip():
            return mcp_error("query is required", hint="pass a non-empty query string")
        if limit < 1 or limit > 200:
            return mcp_error("limit must be between 1 and 200")
        st = get_state(workspace)
        results, query_mode = keyword_search(
            st.conn, st.project_id, query, scope, limit
        )
        fresh = chunks_index_fresh(st.conn, st.project_id, st.settings.workspace)
        out: dict[str, Any] = {
            "query": query,
            "scope": scope,
            "query_mode": query_mode,
            "results": results,
            "count": len(results),
            "lanes": {"fts5": True},
            **fresh,
        }
        if not fresh["index_fresh"]:
            out["hint"] = (
                "some chunk source files changed on disk — run "
                "index_project(workspace=..., force=false) and re-search"
            )
        return out
