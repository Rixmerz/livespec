"""Outline of a file or a class: signatures and docstring leads, no bodies.

"What does module X do?" and "what does class Y offer?" used to cost either
the whole file or one `quick_orient` per symbol — measured on this repo, the
second is *more* characters than the first. Everything an outline needs
(signature, docstring, span, parent) is already a column of `symbol`, so this
is one indexed query: nothing to cache, nothing to invalidate, and it is
exactly as fresh as the index.
"""

from __future__ import annotations

import sqlite3
from typing import Any

MAX_OUTLINE_LINES = 150
MAX_DOC_LEAD = 100
MAX_SIGNATURE = 160

_COLUMNS = (
    "s.id, s.parent_symbol_id, s.kind, s.name, s.signature, s.docstring, s.start_line, s.end_line"
)


def _lead(docstring: str | None) -> str:
    if not docstring:
        return ""
    for line in docstring.splitlines():
        text = line.strip()
        if text:
            return text if len(text) <= MAX_DOC_LEAD else text[: MAX_DOC_LEAD - 1] + "…"
    return ""


def _render(rows: list[sqlite3.Row], limit: int) -> dict[str, Any]:
    """`start-end signature — docstring lead`, indented by nesting."""
    depth: dict[int, int] = {}
    lines: list[str] = []
    for r in rows:
        parent = r["parent_symbol_id"]
        d = depth[parent] + 1 if parent in depth else 0
        depth[int(r["id"])] = d
        # A signature can be thousands of characters (FastAPI's
        # `Annotated[..., Doc("...")]` parameters); an outline line cannot.
        head = " ".join((r["signature"] or f"{r['kind']} {r['name']}").split())
        if len(head) > MAX_SIGNATURE:
            head = head[: MAX_SIGNATURE - 1] + "…"
        lead = _lead(r["docstring"])
        lines.append(
            f"{'  ' * d}{r['start_line']}-{r['end_line']} {head}" + (f" — {lead}" if lead else "")
        )
    out: dict[str, Any] = {"symbols": len(lines), "outline": lines[:limit]}
    if len(lines) > limit:
        out["outline_truncated"] = len(lines) - limit
    return out


def file_outline(
    conn: sqlite3.Connection,
    project_ids: list[int],
    path: str,
    *,
    limit: int = MAX_OUTLINE_LINES,
) -> dict[str, Any] | None:
    """Outline of one indexed file, or None when `path` is not an indexed file."""
    fp = path.replace("\\", "/")
    fp = fp[2:] if fp.startswith("./") else fp
    if not fp or not project_ids:
        return None
    marks = ",".join("?" * len(project_ids))
    order = " ".join(f"WHEN {int(pid)} THEN {i}" for i, pid in enumerate(project_ids))
    row = conn.execute(
        f"SELECT id, path, language FROM file WHERE path = ? AND project_id IN ({marks}) "
        f"ORDER BY CASE project_id {order} END LIMIT 1",
        (fp, *project_ids),
    ).fetchone()
    if row is None:
        return None
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM symbol s WHERE s.file_id = ? ORDER BY s.start_line, s.id",
        (row["id"],),
    ).fetchall()
    return {"file_path": row["path"], "language": row["language"], **_render(rows, limit)}


def members_outline(
    conn: sqlite3.Connection, symbol_id: int, *, limit: int = MAX_OUTLINE_LINES
) -> dict[str, Any] | None:
    """Outline of everything nested inside a symbol; None when it has no members."""
    rows = conn.execute(
        f"""WITH RECURSIVE inside(id) AS (
                SELECT id FROM symbol WHERE parent_symbol_id = ?
                UNION
                SELECT c.id FROM symbol c JOIN inside i ON c.parent_symbol_id = i.id
            )
            SELECT {_COLUMNS} FROM symbol s JOIN inside i ON i.id = s.id
            ORDER BY s.start_line, s.id""",
        (symbol_id,),
    ).fetchall()
    if not rows:
        return None
    rendered = _render(rows, limit)
    out: dict[str, Any] = {"members": rendered["symbols"], "outline": rendered["outline"]}
    if "outline_truncated" in rendered:
        out["outline_truncated"] = rendered["outline_truncated"]
    return out
