"""Spec<->code matcher with two-level confidence.

Ids are OpenSpec slugs (`auth-user-login`). A slug is recognized because it is
a spec id in the store — there is no open kebab regex, or every hyphen in every
docstring would be a candidate. PREFIX-NNN shape-match only fires when the
store still has rows of that shape (unmigrated data); an empty store matches
nothing by form.

Level 1 — explicit prefix on its own line (or at start of a comment block):
  `@spec:auth-user-login`, `@implements:auth-user-login`, `@see:auth-user-login`
  -> confidence 1.0, source='annotation'

Level 2 — verb-anchored inline mention:
  `... implements auth-user-login`, `tests auth-user-login`
  -> confidence 0.7, source='annotation', requires `relation` derived from verb

Bare mentions like `we should do this for auth-user-login` or `not
auth-user-login` are ignored.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# Bounded so a repo that renamed a whole capability doesn't return one entry
# per annotated symbol. `unknown_ids` stays complete — only the sample is cut.
_UNKNOWN_SAMPLE_MAX = 20

# Level 1: line starts with @spec | @implements | @tests | @see  -OR-
#          @not_spec | @!spec  (negation: cancels any hit on the listed specs)
# Captures the rest of the line so we can parse:
#   - multiple comma-separated specs:  @spec:auth-login, auth-logout
#   - confidence override at the end:  @spec:auth-login:0.85
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

# PREFIX-NNN shape-match is derived only from ids already in the store
# (unmigrated rows). An OpenSpec-slug store yields an empty prefix set — then
# annotations resolve exclusively via ``known_ids``.
_NEVER_RE = re.compile(r"(?!)")  # matches nothing; used when prefixes is empty


def _spec_alt(prefixes: Iterable[str]) -> str:
    """Build a `|`-alternation of escaped prefixes, longest first so e.g.
    `BE-RF` wins over a hypothetical bare `RF`."""
    prefs = sorted({p.upper() for p in prefixes}, key=len, reverse=True)
    return "|".join(re.escape(p) for p in prefs)


@lru_cache(maxsize=64)
def make_spec_token_re(prefixes: tuple[str, ...] = ()) -> re.Pattern[str]:
    """Compile the PREFIX-NNN token regex for prefixes present in the store.

    Empty ``prefixes`` → a never-matching pattern (OpenSpec-slug stores).
    Named groups `prefix` and `num` so normalization preserves the matched
    prefix.
    """
    prefs = tuple(sorted({p.upper() for p in prefixes}, key=len, reverse=True))
    if not prefs:
        return _NEVER_RE
    alt = "|".join(re.escape(p) for p in prefs)
    return re.compile(rf"(?P<prefix>{alt})[-_]?(?P<num>\d{{1,6}})", re.IGNORECASE)


# Default: no PREFIX-NNN scheme until the store says otherwise.
_SPEC_TOKEN_RE = make_spec_token_re()
# Public alias — cross-module callers (specs.scan_annotation_verbs) import
# this instead of the "private" name so the token shape can never drift.
SPEC_TOKEN_RE = _SPEC_TOKEN_RE


def derive_spec_prefixes(spec_ids: Iterable[str]) -> tuple[str, ...]:
    """PREFIX-NNN prefixes actually present in the store (may be empty).

    `BE-RF-102` -> `BE-RF`, `SPEC-241` -> `SPEC`. Slug-only stores return ``()``
    so shape-match stays off — annotations resolve via ``known_ids`` only.
    """
    prefixes: set[str] = set()
    for sid in spec_ids:
        prefix, sep, num = sid.rpartition("-")
        if sep and prefix and num.isdigit():
            prefixes.add(prefix.upper())
    return tuple(sorted(prefixes))

# Optional `:confidence` suffix at the end of a prefix payload. Accepts
# `:0.85`, `:.85`, `:1.0`, `:1`. Anchored to end so it doesn't eat digits
# from SPEC tokens.
_CONF_SUFFIX_RE = re.compile(r"\s*:\s*(0?\.\d+|1\.0+|1)\s*$")

# Level 2: `<verb> PREFIX-NNN` when the store still has that shape.
# Negation guard: must NOT be preceded by "not"/"no"/… within last 12 chars.
@lru_cache(maxsize=64)
def _make_verb_re(prefixes: tuple[str, ...] = ()) -> re.Pattern[str]:
    """Verb-anchored PREFIX-NNN mentions; never-matches when prefixes empty."""
    prefs = tuple(sorted({p.upper() for p in prefixes}, key=len, reverse=True))
    if not prefs:
        return _NEVER_RE
    alt = "|".join(re.escape(p) for p in prefs)
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
    spec_id: str         # store id (OpenSpec slug, or unmigrated PREFIX-NNN)
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
    prefixes: Iterable[str] = (),
    *,
    known_ids: Iterable[str] | None = None,
) -> list[AnnotationHit]:
    """Extract all Spec annotations from a docstring/comment block.

    Levels:
    - L1 prefix `@spec:auth-user-login` / `@implements:…` / `@tests:…` -> 1.0
      when the id is in ``known_ids``. Multi-spec and `:0.85` overrides work.
    - L1 negation `@not_spec:…` / `@!spec:…` cancels hits for listed ids.
    - L2 verb-anchored `implements auth-user-login` -> 0.7 (known_ids), with
      negation-window guard.

    ``prefixes``: PREFIX-NNN schemes still present in the store (may be empty).
    Without ``known_ids`` and without store prefixes, nothing matches.
    """
    if not text:
        return []
    prefixes_key = tuple(sorted({p.upper() for p in prefixes}))
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


# An `@verb:` payload whose first token looks like an id but resolved to
# nothing. Requires a digit or an inner hyphen so `@see the README` doesn't
# nominate "the" as a missing spec.
_ID_SHAPED_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,79}$")


def find_unknown_annotation_ids(
    text: str,
    prefixes: Iterable[str] = (),
    *,
    known_ids: Iterable[str] | None = None,
) -> list[str]:
    """Ids annotated in ``text`` that no spec in the store answers to.

    A renamed OpenSpec requirement changes the slug its id derives from, and
    the stale `@spec:` in the code then matches nothing at all — no hit, no
    link, no complaint. This reports those so the silence is visible; it
    never links anything.
    """
    if not text:
        return []
    prefixes_key = tuple(sorted({p.upper() for p in prefixes}))
    token_re = make_spec_token_re(prefixes_key)
    known = frozenset(known_ids or ())
    known_fold = {k.casefold() for k in known}
    out: list[str] = []
    for m in _PREFIX_HEAD_RE.finditer(text):
        if m.group("verb").lower() in ("not_spec", "!spec"):
            continue
        payload = _CONF_SUFFIX_RE.sub("", m.group("rest"))
        # The id lives in the first slot; the rest of the line is prose.
        first = next((t for t in re.split(r"[,;\s]+", payload) if t), "").strip("`")
        if not first or not _ID_SHAPED_RE.match(first):
            continue
        if not ("-" in first[1:] or any(c.isdigit() for c in first)):
            continue
        if first.casefold() in known_fold:
            continue
        if token_re.fullmatch(first):
            # PREFIX-NNN shape: normalized before it is looked up in the store.
            canon = _normalize_spec(first, token_re)
            if canon.casefold() in known_fold:
                continue
            first = canon
        if first not in out:
            out.append(first)
    return out


@dataclass
class ScanResult:
    created: int
    unknown_ids: list[str]
    unknown_sample: list[dict[str, Any]]


def scan_annotations(conn: sqlite3.Connection, project_id: int) -> ScanResult:
    """@spec:spec-code-traceability

    Walk every symbol's docstring; create spec_symbol links from Spec annotations.

    Returns the number of links created (duplicates skipped) plus the ids that
    were annotated in code but match no spec in the store — see
    ``find_unknown_annotation_ids``.
    """
    rows = conn.execute(
        """SELECT s.id, s.docstring, s.qualified_name, f.path AS file_path
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
    unknown: list[str] = []
    sample: list[dict[str, Any]] = []
    for r in rows:
        doc = r["docstring"] or ""
        for missing in find_unknown_annotation_ids(doc, prefixes, known_ids=known_ids):
            if missing not in unknown:
                unknown.append(missing)
            if len(sample) < _UNKNOWN_SAMPLE_MAX:
                sample.append({
                    "spec_id": missing,
                    "qualified_name": r["qualified_name"],
                    "file_path": r["file_path"],
                })
        for hit in parse_annotations(doc, prefixes, known_ids=known_ids):
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
    return ScanResult(created=created, unknown_ids=unknown, unknown_sample=sample)
