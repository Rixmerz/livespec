"""Debt baseline — freeze what is already there, gate only what is new.

Turn duplication detection on in an existing repo and it lights up everywhere:
hundreds of near-duplicates that are legitimate, deliberate, or simply nobody's
priority today. All of them true. None of them actionable this week.

That noise is not a tuning problem, it is a lifecycle problem, and the fix is
not a stricter threshold — a threshold high enough to silence a legacy repo is
high enough to miss the duplicate written five minutes ago. The fix is to
record what existed at install time and stop reporting it.

What gets frozen is a *violation*, not a symbol — and that distinction is the
whole feature. Freezing symbols was the first attempt and it made the tool
useless in its main case: after capture, an agent writing a fresh copy of
`format_currency` heard nothing, because the symbol it duplicated was frozen.
That is precisely the moment the tool exists for.

So: two or more bodies that already shared a shape at capture time are accepted
debt and go quiet. A lone `format_currency` is not a violation, so a copy
written afterwards is new duplication and still reports.

The one way frozen debt speaks again is the **boy-scout rule**: the session
already opened that file, so the mess is this change's problem by choice rather
than by accident. Off by default, because it is the rule that generates
argument.

The asymmetry that shapes the whole module: a duplicate that slips through
costs some redundancy, while a false alarm costs the user's trust once and the
feature entirely on the second. Everything here errs toward silence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class BaselineEntry:
    structural_hash: str
    qualified_name: str
    file_path: str


@dataclass
class Verdict:
    """Whether a candidate body should be reported, and why not when it should not."""

    gated: bool
    reason: str
    matches: list[str]

    @property
    def allowed(self) -> bool:
        return not self.gated


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS debt_baseline (
            structural_hash TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            PRIMARY KEY (structural_hash, qualified_name)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_debt_baseline_hash "
        "ON debt_baseline(structural_hash)"
    )


def capture(
    conn: sqlite3.Connection, corpus: list[tuple[str, str, object]]
) -> dict[str, int]:
    """Freeze the duplication that already exists, not every symbol that exists.

    The distinction is the whole feature, and getting it wrong the first time
    made the tool useless in its main case: freezing *symbols* meant that after
    capture, an agent writing a fresh copy of `format_currency` was told
    nothing — the symbol it duplicated was frozen, so the match was suppressed.
    That is exactly the moment the tool exists for.

    What is pre-existing debt is a *violation*: two or more bodies that already
    shared a shape when the snapshot was taken. Those go quiet. A lone
    `format_currency` is not a violation, so a copy written afterwards is new
    duplication and still reports.

    Idempotent. Re-running after new code lands freezes whatever duplication
    arrived since, which is the wrong thing to do casually and the right thing
    to do deliberately after a large merge — `reset=True` is how you say you
    meant it.
    """
    ensure_table(conn)
    now = datetime.now(timezone.utc).isoformat()

    by_shape: dict[str, list[tuple[str, str]]] = {}
    for qname, path, fp in corpus:
        by_shape.setdefault(fp.structural_hash, []).append((qname, path))  # type: ignore[attr-defined]

    violations = [
        (shape, qname, path, now)
        for shape, members in by_shape.items()
        if len(members) > 1
        for qname, path in members
    ]

    before = conn.execute("SELECT COUNT(*) FROM debt_baseline").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO debt_baseline "
        "(structural_hash, qualified_name, file_path, captured_at) "
        "VALUES (?, ?, ?, ?)",
        violations,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM debt_baseline").fetchone()[0]
    return {
        "frozen": after,
        "added": after - before,
        "was": before,
        "scanned": len(corpus),
        "shapes_frozen": len({v[0] for v in violations}),
    }


def clear(conn: sqlite3.Connection) -> int:
    ensure_table(conn)
    n = conn.execute("SELECT COUNT(*) FROM debt_baseline").fetchone()[0]
    conn.execute("DELETE FROM debt_baseline")
    conn.commit()
    return n


def is_frozen(conn: sqlite3.Connection, structural_hash: str) -> list[BaselineEntry]:
    ensure_table(conn)
    return [
        BaselineEntry(r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT structural_hash, qualified_name, file_path "
            "FROM debt_baseline WHERE structural_hash = ?",
            (structural_hash,),
        )
    ]


def has_baseline(conn: sqlite3.Connection) -> bool:
    ensure_table(conn)
    return conn.execute("SELECT 1 FROM debt_baseline LIMIT 1").fetchone() is not None


def judge(
    conn: sqlite3.Connection,
    candidate_hash: str,
    matches: list,
    *,
    touched_files: frozenset[str] = frozenset(),
    boy_scout: bool = False,
) -> Verdict:
    """Should these duplicate matches be reported, given the frozen baseline?

    One rule: a shape that was *already duplicated* when the snapshot was taken
    is accepted debt and stays quiet. Everything else reports — including a
    body written today that copies a symbol which was, until now, unique.

    With no baseline captured nothing is frozen and everything reports. That is
    the honest default for a repo that never opted in, and the reason
    `has_baseline` is checked rather than assumed.
    """
    if not matches:
        return Verdict(gated=False, reason="no duplicates found", matches=[])

    if not has_baseline(conn):
        return Verdict(
            gated=True,
            reason="no baseline captured — every match reports",
            matches=[m.qualified_name for m in matches],
        )

    frozen_shape = is_frozen(conn, candidate_hash)
    if frozen_shape:
        # The boy-scout rule is the one way frozen debt still speaks: the
        # session already opened that file, so the mess is this change's
        # problem by choice rather than by accident.
        if boy_scout:
            live = [m.qualified_name for m in matches if m.file_path in touched_files]
            if live:
                return Verdict(
                    gated=True,
                    reason="frozen debt in a file this session already opened",
                    matches=live,
                )
        return Verdict(
            gated=False,
            reason=(
                f"this shape was already duplicated at capture time "
                f"({len(frozen_shape)} copies, e.g. {frozen_shape[0].qualified_name}) "
                f"— accepted debt, not this change's doing"
            ),
            matches=[],
        )

    return Verdict(
        gated=True,
        reason="new duplication",
        matches=[m.qualified_name for m in matches],
    )
