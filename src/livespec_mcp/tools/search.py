"""Hybrid search (Phase 4 minimal v1: keyword-only via SQL LIKE/BM25).

Embedding layer is scaffolded: when fastembed+sqlite-vec are present we'll add
vector search and Reciprocal Rank Fusion. For now, BM25 over symbol names,
docstrings, and RF titles.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP
from rank_bm25 import BM25Okapi

from livespec_mcp.state import get_state


def _tokenize(text: str) -> list[str]:
    return [t for t in (text or "").lower().split() if t]


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def search(
        query: str,
        scope: Literal["all", "code", "requirements"] = "all",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Keyword search (BM25) over indexed symbols, docstrings and RFs.

        Returns top-k results with score, type and path/qname. Embedding-based
        semantic search will be added once `fastembed` extras are installed.
        """
        st = get_state()
        pid = st.project_id

        corpus: list[dict[str, Any]] = []
        tokens: list[list[str]] = []

        if scope in ("all", "code"):
            for r in st.conn.execute(
                """SELECT s.id, s.name, s.qualified_name, s.kind, s.docstring, s.signature, f.path
                   FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
                (pid,),
            ):
                doc = " ".join(filter(None, [r["name"], r["qualified_name"], r["signature"], r["docstring"]]))
                corpus.append({
                    "type": "symbol",
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file_path": r["path"],
                    "snippet": (r["docstring"] or r["signature"] or "")[:200],
                })
                tokens.append(_tokenize(doc))

        if scope in ("all", "requirements"):
            for r in st.conn.execute(
                "SELECT rf_id, title, description FROM rf WHERE project_id=?", (pid,)
            ):
                doc = " ".join(filter(None, [r["rf_id"], r["title"], r["description"]]))
                corpus.append({
                    "type": "requirement",
                    "rf_id": r["rf_id"],
                    "title": r["title"],
                    "snippet": (r["description"] or "")[:200],
                })
                tokens.append(_tokenize(doc))

        if not corpus:
            return {"results": []}

        bm25 = BM25Okapi(tokens)
        scores = bm25.get_scores(_tokenize(query))
        ranked = sorted(zip(corpus, scores), key=lambda x: x[1], reverse=True)[:limit]
        return {
            "query": query,
            "results": [
                {**doc, "score": round(float(score), 4)}
                for doc, score in ranked
                if score > 0
            ],
        }
