"""Unit tests for the @spec: annotation matcher (P1.4)."""

from __future__ import annotations

from livespec_mcp.domain.matcher import parse_annotations


def test_level1_prefix_high_confidence():
    text = "Login a user.\n\n@spec:SPEC-001"
    hits = parse_annotations(text)
    assert len(hits) == 1
    assert hits[0].spec_id == "SPEC-001"
    assert hits[0].confidence == 1.0
    assert hits[0].relation == "implements"


def test_level1_alternate_prefixes():
    text = (
        "@implements:SPEC-002\n"
        "@tests SPEC-003\n"
        "@see:SPEC-004\n"
    )
    hits = parse_annotations(text)
    spec_to_relation = {h.spec_id: h.relation for h in hits}
    assert spec_to_relation == {
        "SPEC-002": "implements",
        "SPEC-003": "tests",
        "SPEC-004": "references",
    }
    assert all(h.confidence == 1.0 for h in hits)


def test_level2_verb_inline():
    text = "This function implements SPEC-005 by hashing the password."
    hits = parse_annotations(text)
    assert len(hits) == 1
    assert hits[0].spec_id == "SPEC-005"
    assert hits[0].confidence == 0.7
    assert hits[0].relation == "implements"


def test_level2_negation_dropped():
    """Negated mentions must not link."""
    samples = [
        "This does NOT implement SPEC-006.",
        "We never implement SPEC-007 here.",
        "This module doesn't implement SPEC-008 yet.",
        "TODO: implement SPEC-009",
    ]
    for s in samples:
        hits = parse_annotations(s)
        assert hits == [], f"Negated text leaked through: {s!r} -> {hits}"


def test_bare_mention_dropped():
    """Mentions without a verb must not produce links."""
    text = "We discussed SPEC-010 at the standup. The doc for SPEC-011 is in Notion."
    hits = parse_annotations(text)
    assert hits == []


def test_normalization():
    """SPEC-1 and SPEC-001 should normalize to the same id."""
    h1 = parse_annotations("@spec:SPEC-1")[0]
    h2 = parse_annotations("@spec:SPEC-001")[0]
    h3 = parse_annotations("@spec:SPEC_42")[0]
    assert h1.spec_id == "SPEC-001"
    assert h2.spec_id == "SPEC-001"
    assert h3.spec_id == "SPEC-042"


def test_level1_takes_priority_over_level2():
    """When both a prefix and a verb-mention exist for the same Spec, prefer level 1."""
    text = "@spec:SPEC-100\n\nThis function implements SPEC-100 by hashing."
    hits = parse_annotations(text)
    assert len(hits) == 1
    assert hits[0].confidence == 1.0


def test_multiple_distinct_rfs():
    text = (
        "@spec:SPEC-001\n"
        "Also implements SPEC-002 partially.\n"
        "Tests SPEC-003 indirectly."
    )
    hits = parse_annotations(text)
    spec_ids = {h.spec_id for h in hits}
    assert spec_ids == {"SPEC-001", "SPEC-002", "SPEC-003"}


def test_comment_leader_stripped():
    """Prefix matcher works through `#` and `*` comment leaders."""
    text = "# @spec:SPEC-050\n * @implements SPEC-051"
    hits = parse_annotations(text)
    spec_ids = {h.spec_id for h in hits}
    assert spec_ids == {"SPEC-050", "SPEC-051"}
