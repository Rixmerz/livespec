"""Unit tests for the @spec: annotation matcher (P1.4)."""

from __future__ import annotations

from livespec_mcp.domain.matcher import parse_annotations

_KNOWN = ["auth-user-login", "auth-session", "untested-feature", "report-covered", "api-surface"]


def test_level1_prefix_high_confidence():
    text = "Login a user.\n\n@spec:auth-user-login"
    hits = parse_annotations(text, known_ids=_KNOWN)
    assert len(hits) == 1
    assert hits[0].spec_id == "auth-user-login"
    assert hits[0].confidence == 1.0
    assert hits[0].relation == "implements"


def test_level1_alternate_prefixes():
    text = (
        "@implements:auth-session\n"
        "@tests untested-feature\n"
        "@see:report-covered\n"
    )
    hits = parse_annotations(text, known_ids=_KNOWN)
    spec_to_relation = {h.spec_id: h.relation for h in hits}
    assert spec_to_relation == {
        "auth-session": "implements",
        "untested-feature": "tests",
        "report-covered": "references",
    }
    assert all(h.confidence == 1.0 for h in hits)


def test_level2_verb_inline():
    text = "This function implements auth-user-login by hashing the password."
    hits = parse_annotations(text, known_ids=_KNOWN)
    assert len(hits) == 1
    assert hits[0].spec_id == "auth-user-login"
    assert hits[0].confidence == 0.7
    assert hits[0].relation == "implements"


def test_level2_negation_dropped():
    """Negated mentions must not link."""
    known = ["neg-six", "neg-seven", "neg-eight", "neg-nine"]
    samples = [
        "This does NOT implement neg-six.",
        "We never implement neg-seven here.",
        "This module doesn't implement neg-eight yet.",
        "TODO: implement neg-nine",
    ]
    for s in samples:
        hits = parse_annotations(s, known_ids=known)
        assert hits == [], f"Negated text leaked through: {s!r} -> {hits}"


def test_bare_mention_dropped():
    """Mentions without a verb must not produce links."""
    known = ["bare-ten", "bare-eleven"]
    text = "We discussed bare-ten at the standup. The doc for bare-eleven is in Notion."
    hits = parse_annotations(text, known_ids=known)
    assert hits == []


def test_slug_ids_are_case_insensitive():
    h1 = parse_annotations("@spec:Auth-User-Login", known_ids=["auth-user-login"])
    h2 = parse_annotations("@spec:auth-user-login", known_ids=["auth-user-login"])
    assert h1[0].spec_id == "auth-user-login"
    assert h2[0].spec_id == "auth-user-login"


def test_level1_takes_priority_over_level2():
    """When both a prefix and a verb-mention exist for the same Spec, prefer level 1."""
    text = "@spec:api-surface\n\nThis function implements api-surface by hashing."
    hits = parse_annotations(text, known_ids=["api-surface"])
    assert len(hits) == 1
    assert hits[0].confidence == 1.0


def test_multiple_distinct_rfs():
    text = (
        "@spec:auth-user-login\n"
        "Also implements auth-session partially.\n"
        "Tests untested-feature indirectly."
    )
    hits = parse_annotations(text, known_ids=_KNOWN)
    spec_ids = {h.spec_id for h in hits}
    assert spec_ids == {"auth-user-login", "auth-session", "untested-feature"}


def test_comment_leader_stripped():
    """Prefix matcher works through `#` and `*` comment leaders."""
    known = ["comment-fifty", "comment-fifty-one"]
    text = "# @spec:comment-fifty\n * @implements comment-fifty-one"
    hits = parse_annotations(text, known_ids=known)
    spec_ids = {h.spec_id for h in hits}
    assert spec_ids == {"comment-fifty", "comment-fifty-one"}


def test_openspec_slug_via_known_ids():
    """OpenSpec kebab ids link only when present in the store allowlist."""
    known = ("auth-user-login", "payments-charge")
    assert parse_annotations("@spec:auth-user-login") == []
    hits = parse_annotations("@spec:auth-user-login", known_ids=known)
    assert len(hits) == 1
    assert hits[0].spec_id == "auth-user-login"
    assert hits[0].confidence == 1.0

    hits2 = parse_annotations(
        "This function implements auth-user-login.", known_ids=known
    )
    assert len(hits2) == 1
    assert hits2[0].spec_id == "auth-user-login"
    assert hits2[0].confidence == 0.7

    # Unknown slug stays invisible (no open kebab regex)
    assert parse_annotations("@spec:not-in-store", known_ids=known) == []
