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
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

# Level 1: line starts with @spec | @implements | @tests | @see  -OR-
#          @not_spec | @!spec  (negation: cancels any hit on the listed specs)
# Captures the rest of the line so we can parse:
#   - multiple comma-separated specs:  @spec:SPEC-001, SPEC-002
#   - confidence override at the end:  @spec:SPEC-001:0.85   (or  @spec:SPEC-001,SPEC-002:0.85)
#
# `_PREFIX_HEAD_RE`'s verb alternation is BUILT from this tuple (not
# duplicated) so the regex and the "recognized verb" vocabulary can never
# drift apart. Import `RECOGNIZED_PREFIX_VERBS` / `RECOGNIZED_PREFIX_VERBS_DISPLAY`
# wherever that vocabulary is needed elsewhere (e.g. specs.scan_annotation_verbs) —
# never re-declare the verb list.
RECOGNIZED_PREFIX_VERBS: tuple[str, ...] = (
    "not_spec", "!spec", "spec", "implements?", "tests?", "see", "references?",
)
# Human-readable expansion of the pattern forms above, for did-you-mean
# suggestions. Cosmetic only — it does not affect what the regex matches.
RECOGNIZED_PREFIX_VERBS_DISPLAY: frozenset[str] = frozenset({
    "spec", "implement", "implements", "test", "tests", "see",
    "reference", "references", "not_spec",
})
_PREFIX_HEAD_RE = re.compile(
    r"""^\s*[#*]?\s*                       # optional comment leader
        @(?P<verb>""" + "|".join(RECOGNIZED_PREFIX_VERBS) + r""")
        (?=[:=\s]|$)                       # verb boundary: reject @specifically,
                                           # @testsuite, @seed (prefix-of-a-word)
        \s*[:=]?\s*
        (?P<rest>[^\n\r]+)""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

# Spec-ID prefix is NOT hardcoded to "SPEC" — projects with their own scheme
# (`BE-RF-102`, `FE-RF-119`, `DEVMCP-RF-007`) derive their accepted prefixes
# from the spec IDs actually present in the store (see `derive_spec_prefixes`
# / `scan_annotations`). "SPEC" is always included so a store with no specs
# yet (or none using a custom scheme) behaves exactly as before.
def _spec_alt(prefixes: Iterable[str]) -> str:
    """Build a `|`-alternation of escaped prefixes, longest first so e.g.
    `BE-RF` wins over a hypothetical bare `RF`."""
    prefs = sorted({p.upper() for p in prefixes}, key=len, reverse=True)
    return "|".join(re.escape(p) for p in prefs)


@lru_cache(maxsize=64)
def make_spec_token_re(prefixes: tuple[str, ...] = ("SPEC",)) -> re.Pattern[str]:
    """Compile the SPEC-token regex for a given set of accepted prefixes.

    Named groups `prefix` and `num` so normalization can preserve the
    matched prefix instead of blindly collapsing everything to `SPEC-NNN`.
    """
    alt = _spec_alt(prefixes)
    return re.compile(rf"(?P<prefix>{alt})[-_]?(?P<num>\d{{1,6}})", re.IGNORECASE)


# Each SPEC-NNN (or custom-prefix) token inside the `rest` payload of a
# prefix annotation. Default: SPEC-only, identical to the old hardcoded regex.
_SPEC_TOKEN_RE = make_spec_token_re()
# Public alias — cross-module callers (specs.scan_annotation_verbs) import
# this instead of the "private" name so the token shape can never drift.
SPEC_TOKEN_RE = _SPEC_TOKEN_RE


def derive_spec_prefixes(spec_ids: Iterable[str]) -> tuple[str, ...]:
    """Derive the set of valid ID prefixes from spec IDs in the store.

    `BE-RF-102` -> `BE-RF`, `SPEC-241` -> `SPEC`. Always unions in `SPEC` so
    a spec-less (or SPEC-only) repo behaves exactly as before.
    """
    prefixes = {"SPEC"}
    for sid in spec_ids:
        prefix, sep, num = sid.rpartition("-")
        if sep and prefix and num.isdigit():
            prefixes.add(prefix.upper())
    return tuple(sorted(prefixes))

# Optional `:confidence` suffix at the end of a prefix payload. Accepts
# `:0.85`, `:.85`, `:1.0`, `:1`. Anchored to end so it doesn't eat digits
# from SPEC tokens.
_CONF_SUFFIX_RE = re.compile(r"\s*:\s*(0?\.\d+|1\.0+|1)\s*$")

# Level 2: `<verb> SPEC-NNN`. Negation guard: must NOT be preceded by "not",
# "no", "never", "doesn't", "do not", "without", "skip", "TODO" within last
# 12 chars.
@lru_cache(maxsize=64)
def _make_verb_re(prefixes: tuple[str, ...] = ("SPEC",)) -> re.Pattern[str]:
    """Same shape as the old hardcoded `_VERB_RE`, parameterized by prefix
    set so level-2 verb-anchored mentions work for custom schemes too."""
    alt = _spec_alt(prefixes)
    return re.compile(
        rf"""(?P<verb>implements?|tests?|references?|covers?)
            \s+(?P<spec>(?:{alt})[-_]?\d{{1,6}})\b""",
        re.IGNORECASE | re.VERBOSE,
    )


_VERB_RE = _make_verb_re()
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


def _normalize_match(m: re.Match[str]) -> str:
    """Rebuild `PREFIX-NNN` (zero-padded to 3, never truncated) from a
    `prefix`/`num` capture — preserves the prefix instead of collapsing
    every scheme into `SPEC-NNN`."""
    return f"{m.group('prefix').upper()}-{int(m.group('num')):03d}"


def _normalize_spec(raw: str, token_re: re.Pattern[str] = _SPEC_TOKEN_RE) -> str:
    m = token_re.fullmatch(raw.strip()) or token_re.search(raw)
    return _normalize_match(m) if m else raw.upper()


def _relation_for(verb: str) -> str:
    return VERB_TO_RELATION.get(verb.lower().rstrip("s"), VERB_TO_RELATION.get(verb.lower(), "implements"))


def _parse_prefix_payload(
    rest: str,
    token_re: re.Pattern[str] = _SPEC_TOKEN_RE,
    *,
    known_ids: frozenset[str] | None = None,
) -> tuple[list[str], float | None]:
    """Parse the payload after `@verb:`.

    Returns (spec_ids, confidence_override). Confidence override is `None`
    when no `:N.NN` suffix is present, in which case the caller should use
    the default for the verb's level.

    When ``known_ids`` is provided (typically slug OpenSpec ids from the store),
    also match exact case-insensitive tokens that appear in that set — so
    ``@spec:auth-user-login`` works without inventing an open kebab regex.
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
    spec_ids = [_normalize_match(tm) for tm in token_re.finditer(payload)]
    if known_ids:
        # Map casefold → canonical store id
        by_fold = {k.casefold(): k for k in known_ids if len(k) <= 80}
        # Tokens: comma/whitespace separated fragments after stripping prefixes
        for raw in re.split(r"[,;\s]+", payload):
            tok = raw.strip().strip("`")
            if not tok or len(tok) > 80:
                continue
            canon = by_fold.get(tok.casefold())
            if canon and canon not in spec_ids:
                spec_ids.append(canon)
    return spec_ids, conf


def parse_annotations(
    text: str,
    prefixes: Iterable[str] = ("SPEC",),
    *,
    known_ids: Iterable[str] | None = None,
) -> list[AnnotationHit]:
    """Extract all Spec annotations from a docstring/comment block.

    Levels:
    - L1 prefix `@spec:SPEC-001` / `@implements:SPEC-001` / `@tests:SPEC-001` -> 1.0
      Multi-spec: `@spec:SPEC-001, SPEC-002` (each gets its own hit)
      Confidence override: `@spec:SPEC-001:0.85` (applies to all specs in the line)
      OpenSpec slugs: `@spec:auth-user-login` when ``known_ids`` includes that id
    - L1 negation `@not_spec:SPEC-001` (or `@!spec:SPEC-001`) cancels every
      hit (L1 OR L2) for the listed specs in this docstring.
    - L2 verb-anchored `... implements SPEC-001` -> 0.7, with negation-window
      guard ("not", "no", "never", "doesn't", "without", "skip", "TODO").
      Also matches ``implements auth-user-login`` when that id is in known_ids.

    `prefixes`: accepted spec-ID prefixes beyond `SPEC` (e.g. `("SPEC", "BE-RF")`).
    Defaults to `("SPEC",)`, byte-identical to the old hardcoded behavior —
    callers that don't derive prefixes from a store see no change.
    """
    if not text:
        return []
    prefixes_key = tuple(sorted({p.upper() for p in prefixes})) or ("SPEC",)
    token_re = make_spec_token_re(prefixes_key)
    verb_re = _make_verb_re(prefixes_key)
    known = frozenset(known_ids) if known_ids is not None else frozenset()
    known_fold = {k.casefold(): k for k in known if len(k) <= 80}
    hits: list[AnnotationHit] = []
    seen: set[tuple[str, str]] = set()
    negated_specs: set[str] = set()

    # First pass: L1 prefix annotations (positive + negative)
    for m in _PREFIX_HEAD_RE.finditer(text):
        verb = m.group("verb").lower()
        rest = m.group("rest")
        spec_ids, conf_override = _parse_prefix_payload(
            rest, token_re, known_ids=known or None
        )
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

    # Second pass: L2 verb-anchored (digit-prefix schemes)
    for m in verb_re.finditer(text):
        spec_id = _normalize_spec(m.group("spec"), token_re)
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

    # Second pass (b): L2 for known OpenSpec slug ids
    if known_fold:
        slug_verb_re = re.compile(
            r"""(?P<verb>implements?|tests?|references?|covers?)
                \s+(?P<spec>[A-Za-z0-9][A-Za-z0-9._-]{0,79})\b""",
            re.IGNORECASE | re.VERBOSE,
        )
        for m in slug_verb_re.finditer(text):
            raw = m.group("spec")
            canon = known_fold.get(raw.casefold())
            if canon is None:
                continue
            # Skip if already captured as a digit-prefix token
            if token_re.fullmatch(raw.strip()):
                continue
            relation = _relation_for(m.group("verb"))
            key = (canon, relation)
            if key in seen:
                continue
            window_start = max(0, m.start() - 12)
            window = text[window_start : m.start()]
            if _NEGATION_RE.search(window):
                continue
            seen.add(key)
            hits.append(AnnotationHit(spec_id=canon, relation=relation, confidence=0.7))

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
    prefixes = derive_spec_prefixes(spec_map.keys())
    known_ids = tuple(spec_map.keys())

    created = 0
    for r in rows:
        for hit in parse_annotations(
            r["docstring"] or "", prefixes, known_ids=known_ids
        ):
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
