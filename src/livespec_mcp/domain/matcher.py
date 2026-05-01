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

# Level 1: line starts with @rf | @implements | @tests | @see  -OR-
#          @not_rf | @!rf  (negation: cancels any hit on the listed RFs)
# Captures the rest of the line so we can parse:
#   - multiple comma-separated RFs:    @rf:RF-001, RF-002
#   - confidence override at the end:  @rf:RF-001:0.85   (or  @rf:RF-001,RF-002:0.85)
_PREFIX_HEAD_RE = re.compile(
    r"""^\s*[#*]?\s*                       # optional comment leader
        @(?P<verb>not_rf|!rf|rf|implements?|tests?|see|references?)
        \s*[:= ]?\s*
        (?P<rest>[^\n\r]+)""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Each RF-NNN inside the `rest` payload of a prefix annotation.
_RF_TOKEN_RE = re.compile(r"RF[-_]?\d+", re.IGNORECASE)

# Optional `:confidence` suffix at the end of a prefix payload. Accepts
# `:0.85`, `:.85`, `:1.0`, `:1`. Anchored to end so it doesn't eat digits
# from RF tokens.
_CONF_SUFFIX_RE = re.compile(r"\s*:\s*(0?\.\d+|1\.0+|1)\s*$")

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
    confidence: float    # 1.0 (level 1) | 0.7 (level 2) | override (level 1 + suffix)


def _normalize_rf(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    return f"RF-{int(digits):03d}" if digits else raw.upper()


def _relation_for(verb: str) -> str:
    return VERB_TO_RELATION.get(verb.lower().rstrip("s"), VERB_TO_RELATION.get(verb.lower(), "implements"))


def _parse_prefix_payload(rest: str) -> tuple[list[str], float | None]:
    """Parse the payload after `@verb:`.

    Returns (rf_ids, confidence_override). Confidence override is `None` when
    no `:N.NN` suffix is present, in which case the caller should use the
    default for the verb's level.
    """
    payload = rest
    conf: float | None = None
    m = _CONF_SUFFIX_RE.search(payload)
    if m:
        try:
            conf = float(m.group(1))
            if not (0.0 <= conf <= 1.0):
                conf = None
            else:
                payload = payload[: m.start()]
        except ValueError:
            conf = None
    rf_ids = [_normalize_rf(t) for t in _RF_TOKEN_RE.findall(payload)]
    return rf_ids, conf


def parse_annotations(text: str) -> list[AnnotationHit]:
    """Extract all RF annotations from a docstring/comment block.

    Levels:
    - L1 prefix `@rf:RF-001` / `@implements:RF-001` / `@tests:RF-001` -> 1.0
      Multi-RF: `@rf:RF-001, RF-002` (each gets its own hit)
      Confidence override: `@rf:RF-001:0.85` (applies to all RFs in the line)
    - L1 negation `@not_rf:RF-001` (or `@!rf:RF-001`) cancels every hit
      (L1 OR L2) for the listed RFs in this docstring.
    - L2 verb-anchored `... implements RF-001` -> 0.7, with negation-window
      guard ("not", "no", "never", "doesn't", "without", "skip", "TODO").
    """
    if not text:
        return []
    hits: list[AnnotationHit] = []
    seen: set[tuple[str, str]] = set()
    negated_rfs: set[str] = set()

    # First pass: L1 prefix annotations (positive + negative)
    for m in _PREFIX_HEAD_RE.finditer(text):
        verb = m.group("verb").lower()
        rest = m.group("rest")
        rf_ids, conf_override = _parse_prefix_payload(rest)
        if not rf_ids:
            continue
        if verb in ("not_rf", "!rf"):
            negated_rfs.update(rf_ids)
            continue
        relation = _relation_for(verb)
        for rf_id in rf_ids:
            key = (rf_id, relation)
            if key in seen:
                continue
            seen.add(key)
            confidence = conf_override if conf_override is not None else 1.0
            hits.append(AnnotationHit(rf_id=rf_id, relation=relation, confidence=confidence))

    # Second pass: L2 verb-anchored
    for m in _VERB_RE.finditer(text):
        rf_id = _normalize_rf(m.group("rf"))
        relation = _relation_for(m.group("verb"))
        key = (rf_id, relation)
        if key in seen:
            continue
        window_start = max(0, m.start() - 12)
        window = text[window_start : m.start()]
        if _NEGATION_RE.search(window):
            continue
        seen.add(key)
        hits.append(AnnotationHit(rf_id=rf_id, relation=relation, confidence=0.7))

    if negated_rfs:
        hits = [h for h in hits if h.rf_id not in negated_rfs]
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
