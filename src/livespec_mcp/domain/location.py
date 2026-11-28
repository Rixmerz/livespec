"""Which symbols span a `path:line` — the one query two callers need.

`resolve_location` answers it for an agent holding a stack trace, and
`trace_ingest` answers it for every distinct call site in a runtime trace. The
SQL lives here so the two cannot drift: a symbol that a stack frame resolves to
and a trace event at the same place does not is a bug nobody would find.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

#: Kinds a call can land on. A trace event or a stack frame inside a class body
#: but outside any method resolves to the class for `resolve_location`, and to
#: nothing for a runtime hit — only something callable can have been called.
CALLABLE_KINDS: frozenset[str] = frozenset({"function", "method"})


def symbols_spanning(
    conn: sqlite3.Connection,
    project_ids: Sequence[int],
    line: int,
    *,
    path: str | None = None,
    file_id: int | None = None,
) -> list[sqlite3.Row]:
    """Symbols whose `[start_line, end_line]` contains `line`, innermost first.

    Pass `file_id` when the file is already known exactly. Pass `path` for the
    forgiving match `resolve_location` has always used: the stored path equals
    it or ends with it, so a caller may hand over an absolute path's tail.
    """
    placeholders = ",".join("?" * len(project_ids))
    if file_id is not None:
        where, params = "f.id = ?", [file_id]
    else:
        needle = (path or "").strip().lstrip("./")
        where, params = "(f.path = ? OR f.path LIKE ?)", [needle, f"%{needle}"]
    return conn.execute(
        f"SELECT s.id, s.qualified_name, s.name, s.kind, s.signature, "
        f"       s.start_line, s.end_line, f.path, f.id AS file_id "
        f"FROM symbol s JOIN file f ON f.id = s.file_id "
        f"WHERE f.project_id IN ({placeholders}) "
        f"  AND {where} "
        f"  AND s.start_line <= ? AND s.end_line >= ? "
        f"ORDER BY (s.end_line - s.start_line) ASC",
        (*project_ids, *params, line, line),
    ).fetchall()
