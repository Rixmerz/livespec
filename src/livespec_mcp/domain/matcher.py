"""FR<->code matcher (Fase 3 v1: heuristic annotations only).

Detects `@rf:RF-NNN` or `Implements RF-NNN` patterns in docstrings/signatures.
Embedding+LLM layers (Fase 4/6) are TODO.
"""

from __future__ import annotations

import re
import sqlite3

ANNOT_RE = re.compile(r"(?:@rf:|implements\s+|RF\s*-?\s*)(RF[-_]?\d+)", re.IGNORECASE)
RF_ID_RE = re.compile(r"RF[-_]?(\d+)", re.IGNORECASE)


def _normalize_rf(raw: str) -> str:
    m = RF_ID_RE.search(raw)
    if not m:
        return raw.upper()
    return f"RF-{int(m.group(1)):03d}"


def scan_annotations(conn: sqlite3.Connection, project_id: int) -> int:
    """Walk every symbol's docstring; create rf_symbol links from @rf: annotations.

    Returns count of links created.
    """
    rows = conn.execute(
        """SELECT s.id, s.docstring, s.qualified_name
           FROM symbol s JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ? AND s.docstring IS NOT NULL""",
        (project_id,),
    ).fetchall()

    rf_map: dict[str, int] = {
        r["rf_id"]: int(r["id"])
        for r in conn.execute(
            "SELECT id, rf_id FROM rf WHERE project_id = ?", (project_id,)
        )
    }

    created = 0
    for r in rows:
        for raw in ANNOT_RE.findall(r["docstring"] or ""):
            rf_id_str = _normalize_rf(raw)
            rf_pk = rf_map.get(rf_id_str)
            if rf_pk is None:
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO rf_symbol(rf_id, symbol_id, relation, confidence, source)
                       VALUES(?,?,?,?,?)""",
                    (rf_pk, int(r["id"]), "implements", 1.0, "annotation"),
                )
                created += 1
            except sqlite3.IntegrityError:
                pass
    return created
