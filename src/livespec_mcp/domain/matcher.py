"""Spec<->code matcher with two-level confidence.

Level 1 — explicit prefix on its own line (or at start of a comment block):
  `@spec:SPEC-001`, `@implements:SPEC-001`, `@see:SPEC-001`
  -> confidence 1.0, source='annotation'

Level 2 — verb-anchored inline mention:
  `... implements SPEC-001`, `tests SPEC-001`, `references SPEC-001`
  -> confidence 0.7, source='annotation', requires `relation` derived from verb

Bare mentions like `we should do this for SPEC-001` or `not SPEC-001` are
ignored. This is intentionally conservative: previously a regex captured
every SPEC-NNN substring (including negations) which produced false
positives at scale.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# Level 1: line starts with @spec | @implements | @tests | @see  -OR-
#          @not_spec | @!spec  (negation: cancels any hit on the listed specs)
# Captures the rest of the line so we can parse:
#   - multiple comma-separated specs:  @spec:SPEC-001, SPEC-002
#   - confidence override at the end:  @spec:SPEC-001:0.85   (or  @spec:SPEC-001,SPEC-002:0.85)
_PREFIX_HEAD_RE = re.compile(
    r"""^\s*[#*]?\s*                       # optional comment leader
        @(?P<verb>not_spec|!spec|spec|implements?|tests?|see|references?)
        (?=[:=\s]|$)                       # verb boundary: reject @specifically,
                                           # @testsuite, @seed (prefix-of-a-word)
        \s*[:=]?\s*
        (?P<rest>[^\n\r]+)""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Each SPEC-NNN inside the `rest` payload of a prefix annotation.
_SPEC_TOKEN_RE = re.compile(r"SPEC[-_]?\d{1,6}", re.IGNORECASE)

# Optional `:confidence` suffix at the end of a prefix payload. Accepts
# `:0.85`, `:.85`, `:1.0`, `:1`. Anchored to end so it doesn't eat digits
# from SPEC tokens.
_CONF_SUFFIX_RE = re.compile(r"\s*:\s*(0?\.\d+|1\.0+|1)\s*$")

# Level 2: `<verb> SPEC-NNN`. Negation guard: must NOT be preceded by "not",
# "no", "never", "doesn't", "do not", "without", "skip", "TODO" within last
# 12 chars.
_VERB_RE = re.compile(
    r"""(?P<verb>implements?|tests?|references?|covers?)
        \s+(?P<spec>SPEC[-_]?\d{1,6})\b""",
    re.IGNORECASE | re.VERBOSE,
)
_NEGATION_RE = re.compile(
    r"\b(not|no|never|cannot|can'?t|won'?t|isn'?t|shouldn'?t|wouldn'?t|"
    r"doesn'?t|do\s+not|without|skip|TODO|FIXME)\b",
    re.IGNORECASE,
)

VERB_TO_RELATION = {
    "spec": "implements",
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
    spec_id: str         # normalized like "SPEC-001"
    relation: str        # implements | tests | references
    confidence: float    # 1.0 (level 1) | 0.7 (level 2) | override (level 1 + suffix)


def _normalize_spec(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    return f"SPEC-{int(digits):03d}" if digits else raw.upper()


def _relation_for(verb: str) -> str:
    return VERB_TO_RELATION.get(verb.lower().rstrip("s"), VERB_TO_RELATION.get(verb.lower(), "implements"))


def _parse_prefix_payload(rest: str) -> tuple[list[str], float | None]:
    """Parse the payload after `@verb:`.

    Returns (spec_ids, confidence_override). Confidence override is `None`
    when no `:N.NN` suffix is present, in which case the caller should use
    the default for the verb's level.
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
    spec_ids = [_normalize_spec(t) for t in _SPEC_TOKEN_RE.findall(payload)]
    return spec_ids, conf


def parse_annotations(text: str) -> list[AnnotationHit]:
    """Extract all Spec annotations from a docstring/comment block.

    Levels:
    - L1 prefix `@spec:SPEC-001` / `@implements:SPEC-001` / `@tests:SPEC-001` -> 1.0
      Multi-spec: `@spec:SPEC-001, SPEC-002` (each gets its own hit)
      Confidence override: `@spec:SPEC-001:0.85` (applies to all specs in the line)
    - L1 negation `@not_spec:SPEC-001` (or `@!spec:SPEC-001`) cancels every
      hit (L1 OR L2) for the listed specs in this docstring.
    - L2 verb-anchored `... implements SPEC-001` -> 0.7, with negation-window
      guard ("not", "no", "never", "doesn't", "without", "skip", "TODO").
    """
    if not text:
        return []
    hits: list[AnnotationHit] = []
    seen: set[tuple[str, str]] = set()
    negated_specs: set[str] = set()

    # First pass: L1 prefix annotations (positive + negative)
    for m in _PREFIX_HEAD_RE.finditer(text):
        verb = m.group("verb").lower()
        rest = m.group("rest")
        spec_ids, conf_override = _parse_prefix_payload(rest)
        if not spec_ids:
            continue
        if verb in ("not_spec", "!spec"):
            negated_specs.update(spec_ids)
            continue
        relation = _relation_for(verb)
        for spec_id in spec_ids:
            key = (spec_id, relation)
            if key in seen:
                continue
            seen.add(key)
            confidence = conf_override if conf_override is not None else 1.0
            hits.append(AnnotationHit(spec_id=spec_id, relation=relation, confidence=confidence))

    # Second pass: L2 verb-anchored
    for m in _VERB_RE.finditer(text):
        spec_id = _normalize_spec(m.group("spec"))
        relation = _relation_for(m.group("verb"))
        key = (spec_id, relation)
        if key in seen:
            continue
        window_start = max(0, m.start() - 12)
        window = text[window_start : m.start()]
        if _NEGATION_RE.search(window):
            continue
        seen.add(key)
        hits.append(AnnotationHit(spec_id=spec_id, relation=relation, confidence=0.7))

    if negated_specs:
        hits = [h for h in hits if h.spec_id not in negated_specs]
    return hits


def scan_annotations(conn: sqlite3.Connection, project_id: int) -> int:
    """Walk every symbol's docstring; create spec_symbol links from Spec annotations.

    Returns count of links created (skipping duplicates).
    """
    rows = conn.execute(
        """SELECT s.id, s.docstring
           FROM symbol s JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ? AND s.docstring IS NOT NULL""",
        (project_id,),
    ).fetchall()

    spec_map: dict[str, int] = {
        r["spec_id"]: int(r["id"])
        for r in conn.execute(
            "SELECT id, spec_id FROM spec WHERE project_id = ?", (project_id,)
        )
    }

    created = 0
    for r in rows:
        for hit in parse_annotations(r["docstring"] or ""):
            spec_pk = spec_map.get(hit.spec_id)
            if spec_pk is None:
                continue
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO spec_symbol(spec_id, symbol_id, relation, confidence, source)
                       VALUES(?,?,?,?,?)""",
                    (spec_pk, int(r["id"]), hit.relation, hit.confidence, "annotation"),
                )
                if cur.rowcount > 0:
                    created += 1
            except sqlite3.IntegrityError:
                pass
    return created
