"""Unit tests for the @rf: annotation matcher (P1.4)."""

from __future__ import annotations

from livespec_mcp.domain.matcher import parse_annotations


def test_level1_prefix_high_confidence():
    text = "Login a user.\n\n@rf:RF-001"
    hits = parse_annotations(text)
    assert len(hits) == 1
    assert hits[0].rf_id == "RF-001"
    assert hits[0].confidence == 1.0
    assert hits[0].relation == "implements"


def test_level1_alternate_prefixes():
    text = (
        "@implements:RF-002\n"
        "@tests RF-003\n"
        "@see:RF-004\n"
    )
    hits = parse_annotations(text)
    rf_to_relation = {h.rf_id: h.relation for h in hits}
    assert rf_to_relation == {
        "RF-002": "implements",
        "RF-003": "tests",
        "RF-004": "references",
    }
    assert all(h.confidence == 1.0 for h in hits)


def test_level2_verb_inline():
    text = "This function implements RF-005 by hashing the password."
    hits = parse_annotations(text)
    assert len(hits) == 1
    assert hits[0].rf_id == "RF-005"
    assert hits[0].confidence == 0.7
    assert hits[0].relation == "implements"


def test_level2_negation_dropped():
    """Negated mentions must not link."""
    samples = [
        "This does NOT implement RF-006.",
        "We never implement RF-007 here.",
        "This module doesn't implement RF-008 yet.",
        "TODO: implement RF-009",
    ]
    for s in samples:
        hits = parse_annotations(s)
        assert hits == [], f"Negated text leaked through: {s!r} -> {hits}"


def test_bare_mention_dropped():
    """Mentions without a verb must not produce links."""
    text = "We discussed RF-010 at the standup. The doc for RF-011 is in Notion."
    hits = parse_annotations(text)
    assert hits == []


def test_normalization():
    """RF-1 and RF-001 should normalize to the same id."""
    h1 = parse_annotations("@rf:RF-1")[0]
    h2 = parse_annotations("@rf:RF-001")[0]
    h3 = parse_annotations("@rf:RF_42")[0]
    assert h1.rf_id == "RF-001"
    assert h2.rf_id == "RF-001"
    assert h3.rf_id == "RF-042"


def test_level1_takes_priority_over_level2():
    """When both a prefix and a verb-mention exist for the same RF, prefer level 1."""
    text = "@rf:RF-100\n\nThis function implements RF-100 by hashing."
    hits = parse_annotations(text)
    assert len(hits) == 1
    assert hits[0].confidence == 1.0


def test_multiple_distinct_rfs():
    text = (
        "@rf:RF-001\n"
        "Also implements RF-002 partially.\n"
        "Tests RF-003 indirectly."
    )
    hits = parse_annotations(text)
    rf_ids = {h.rf_id for h in hits}
    assert rf_ids == {"RF-001", "RF-002", "RF-003"}


def test_comment_leader_stripped():
    """Prefix matcher works through `#` and `*` comment leaders."""
    text = "# @rf:RF-050\n * @implements RF-051"
    hits = parse_annotations(text)
    rf_ids = {h.rf_id for h in hits}
    assert rf_ids == {"RF-050", "RF-051"}
