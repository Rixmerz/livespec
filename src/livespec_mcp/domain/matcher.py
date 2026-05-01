"""FR<->code matcher with two-level confidence.

Level 1 — explicit prefix on its own line (or at start of a comment block):
  `@rf:RF-001`, `@implements:RF-001`, `@see:RF-001`
  -> confidence 1.0, source='annotation'

Level 2 — verb-anchored inline mention:
  `... implements RF-001`, `tests RF-001`, `references RF-001`
  -> confidence 0.7, source='annotation', requires `relation` derived from verb

Bare mentions like `we should do this for RF-001` or `not RF-001` are ignored.
This is intentionally conservative: previously a regex captured every RF-NNN
substring (including negations) which produced false positives at scale.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# Level 1: line starts with @rf, @implements, @tests, @see
_PREFIX_RE = re.compile(
    r"""^\s*[#*]?\s*                       # optional comment leader
        @(?P<verb>rf|implements?|tests?|see|references?)
        \s*[:= ]\s*
        (?P<rf>RF[-_]?\d+)
        \b""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Level 2: `<verb> RF-NNN`. Negation guard: must NOT be preceded by "not", "no",
# "never", "doesn't", "do not", "without", "skip", "TODO" within last 12 chars.
_VERB_RE = re.compile(
    r"""(?P<verb>implements?|tests?|references?|covers?)
        \s+(?P<rf>RF[-_]?\d+)\b""",
    re.IGNORECASE | re.VERBOSE,
)
_NEGATION_RE = re.compile(
    r"\b(not|no|never|doesn'?t|do\s+not|without|skip|TODO|FIXME)\b",
    re.IGNORECASE,
)

VERB_TO_RELATION = {
    "rf": "implements",
    "implement": "implements",
    "implements": "implements",
    "test": "tests",
    "tests": "tests",
    "reference": "references",
    "references": "references",
    "see": "references",
    "covers": "implements",
    "cover": "implements",
}


@dataclass
class AnnotationHit:
    rf_id: str           # normalized like "RF-001"
    relation: str        # implements | tests | references
    confidence: float    # 1.0 (level 1) | 0.7 (level 2)


def _normalize_rf(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    return f"RF-{int(digits):03d}" if digits else raw.upper()


def _relation_for(verb: str) -> str:
    return VERB_TO_RELATION.get(verb.lower().rstrip("s"), VERB_TO_RELATION.get(verb.lower(), "implements"))


def parse_annotations(text: str) -> list[AnnotationHit]:
    """Extract all RF annotations from a docstring/comment block.

    Returns level-1 hits first (prefix-anchored, conf 1.0), then level-2
    (verb-anchored, conf 0.7). Bare mentions and negated mentions are dropped.
    """
    if not text:
        return []
    hits: list[AnnotationHit] = []
    seen: set[tuple[str, str]] = set()

    # Level 1: explicit prefix
    for m in _PREFIX_RE.finditer(text):
        rf_id = _normalize_rf(m.group("rf"))
        relation = _relation_for(m.group("verb"))
        key = (rf_id, relation)
        if key in seen:
            continue
        seen.add(key)
        hits.append(AnnotationHit(rf_id=rf_id, relation=relation, confidence=1.0))

    # Level 2: verb-anchored, with negation guard
    for m in _VERB_RE.finditer(text):
        # Skip if a level-1 hit already captured this same (rf, relation) — avoid
        # double counting when prefix + sentence both appear.
        rf_id = _normalize_rf(m.group("rf"))
        relation = _relation_for(m.group("verb"))
        key = (rf_id, relation)
        if key in seen:
            continue
        # Negation window: 12 chars before the verb start
        window_start = max(0, m.start() - 12)
        window = text[window_start : m.start()]
        if _NEGATION_RE.search(window):
            continue
        seen.add(key)
        hits.append(AnnotationHit(rf_id=rf_id, relation=relation, confidence=0.7))

    return hits


def scan_annotations(conn: sqlite3.Connection, project_id: int) -> int:
    """Walk every symbol's docstring; create rf_symbol links from RF annotations.

    Returns count of links created (skipping duplicates).
    """
    rows = conn.execute(
        """SELECT s.id, s.docstring
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
        for hit in parse_annotations(r["docstring"] or ""):
            rf_pk = rf_map.get(hit.rf_id)
            if rf_pk is None:
                continue
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO rf_symbol(rf_id, symbol_id, relation, confidence, source)
                       VALUES(?,?,?,?,?)""",
                    (rf_pk, int(r["id"]), hit.relation, hit.confidence, "annotation"),
                )
                if cur.rowcount > 0:
                    created += 1
            except sqlite3.IntegrityError:
                pass
    return created
