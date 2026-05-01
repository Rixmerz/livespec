"""v0.5 P3.B2: matcher precision/recall regression test.

Loads `tests/data/matcher_golden.jsonl` — a curated set of (input, expected
hits) pairs covering the full annotation grammar (single, multi, confidence
override, negation, verb-anchored, negation guard, edge cases). Asserts the
matcher hits == expected exactly. Failure is a regression on a previously
verified case; if a new case is added, the test must be updated alongside.
"""

from __future__ import annotations

import json
from pathlib import Path

from livespec_mcp.domain.matcher import parse_annotations

GOLDEN = Path(__file__).parent / "data" / "matcher_golden.jsonl"


def _load() -> list[dict]:
    out: list[dict] = []
    for ln in GOLDEN.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        out.append(json.loads(ln))
    return out


def _hit_to_dict(h) -> dict:
    return {"rf_id": h.rf_id, "relation": h.relation, "confidence": h.confidence}


def test_matcher_golden_dataset():
    cases = _load()
    assert len(cases) >= 30, f"golden dataset thin (n={len(cases)})"
    failures: list[str] = []
    for case in cases:
        text = case["text"]
        expected = case["expected"]
        got = sorted(
            (_hit_to_dict(h) for h in parse_annotations(text)),
            key=lambda d: (d["rf_id"], d["relation"]),
        )
        exp_sorted = sorted(expected, key=lambda d: (d["rf_id"], d["relation"]))
        if got != exp_sorted:
            failures.append(
                f"\n  input:    {text!r}"
                f"\n  expected: {exp_sorted}"
                f"\n  got:      {got}"
            )
    assert not failures, (
        f"{len(failures)} matcher cases regressed:" + "".join(failures)
    )
