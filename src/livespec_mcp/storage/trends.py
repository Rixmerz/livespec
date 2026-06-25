"""RF test-coverage trend persistence (v0.16 P D).

Every ``audit_coverage`` run appends one snapshot row per RF plus a rollup,
so the coverage ratio can be plotted over time. The table is created by an
append-only migration in ``storage/db.py`` (``rf_coverage_snapshot``).

Two functions:

* ``record_snapshot`` — append one audit's worth of per-RF ratios + rollups,
  all sharing one ``ts``.
* ``read_trend`` — chronological list of ``{ts, avg_test_coverage,
  verified_count}`` rollup rows (one per recorded audit).

Kept deliberately small: the explorer agent consumes ``read_trend`` to build
``data.json``; this module owns only the storage shape.
"""

from __future__ import annotations

import sqlite3
from typing import Any

# Sentinel rf_id used to store the per-snapshot rollup (avg + verified count)
# on its own row. RF ids are user strings like "RF-042"; this one starts with
# a NUL-safe marker that cannot collide with a real RF id.
_ROLLUP_RF_ID = "__rollup__"


def record_snapshot(
    conn: sqlite3.Connection,
    project_id: int,
    per_rf: dict[str, float],
    avg: float | None,
    verified_count: int,
    ts: str,
) -> None:
    """Append one coverage snapshot for ``project_id`` taken at ``ts``.

    ``per_rf`` maps ``rf_id -> test_coverage_ratio``; each becomes one row.
    A single rollup row (``rf_id=__rollup__``) stores ``avg`` (NULL allowed)
    and ``verified_count`` so ``read_trend`` can emit them without re-deriving.

    All rows share the same ``ts`` — that is what groups them into one
    snapshot.

    **Record-on-change:** if the most recent snapshot for ``project_id`` has
    the same rollup (``avg`` + ``verified_count``), this call is a no-op. The
    explorer calls ``compute_coverage`` (hence this) on every export/render, so
    without this guard the trend would fill with near-identical points; we only
    want a new point when coverage actually moved.
    """
    last = conn.execute(
        """SELECT ratio AS avg, verified_count
           FROM rf_coverage_snapshot
           WHERE project_id=? AND rf_id=?
           ORDER BY ts DESC, id DESC LIMIT 1""",
        (project_id, _ROLLUP_RF_ID),
    ).fetchone()
    if last is not None:
        last_avg = last["avg"]
        same_avg = (last_avg is None and avg is None) or (
            last_avg is not None and avg is not None and abs(last_avg - avg) < 1e-9
        )
        if same_avg and int(last["verified_count"] or 0) == int(verified_count):
            return  # unchanged since last snapshot — record only on change

    rows: list[tuple[int, str, str, float | None]] = [
        (project_id, ts, rf_id, float(ratio)) for rf_id, ratio in per_rf.items()
    ]
    # Rollup row: ratio column carries the avg (NULL when no RFs);
    # verified_count rides in its own column.
    conn.executemany(
        """INSERT INTO rf_coverage_snapshot
               (project_id, ts, rf_id, ratio, verified_count)
           VALUES (?, ?, ?, ?, NULL)""",
        rows,
    )
    conn.execute(
        """INSERT INTO rf_coverage_snapshot
               (project_id, ts, rf_id, ratio, verified_count)
           VALUES (?, ?, ?, ?, ?)""",
        (project_id, ts, _ROLLUP_RF_ID, avg, int(verified_count)),
    )


def read_trend(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """Chronological rollup history for ``project_id``.

    Returns one dict per recorded audit:
    ``{ts, avg_test_coverage, verified_count}``, ordered by ``ts`` ascending.
    ``avg_test_coverage`` is ``None`` when the audit had no RFs.
    """
    rows = conn.execute(
        """SELECT ts, ratio AS avg_test_coverage, verified_count
           FROM rf_coverage_snapshot
           WHERE project_id=? AND rf_id=?
           ORDER BY ts ASC, id ASC""",
        (project_id, _ROLLUP_RF_ID),
    ).fetchall()
    return [
        {
            "ts": r["ts"],
            "avg_test_coverage": r["avg_test_coverage"],
            "verified_count": int(r["verified_count"])
            if r["verified_count"] is not None
            else 0,
        }
        for r in rows
    ]
