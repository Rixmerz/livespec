"""Validate the Spec DB against OpenSpec (Fission-AI) structural rules (v0.22).

Mirrors what ``openspec validate [--strict]`` checks, at the DB level, so an
agent can ask "is this spec set OpenSpec-valid?" before exporting or handing
the tree to an OpenSpec-native workflow. The load-bearing rule is OpenSpec's
own invariant: **every requirement MUST have at least one scenario.**

Output shape (v0.23 — aggregated, was flat-per-spec before):

``findings`` groups every finding by ``(issue, severity)`` instead of
emitting one entry per spec. On a corpus where a check fires on nearly every
spec (e.g. 185/185 specs missing scenarios), that collapses to ONE finding
entry with ``count`` + a bounded ``sample`` + ``project_level=True`` instead
of 185 flat entries — the previous shape made ``validate_openspec`` return
unbounded payloads that could exceed the caller's token budget on any
non-trivial repo (a real repo's 185-spec store produced a 53KB+ response
that failed outright).

``valid`` reflects ONLY ``error_count == 0`` — warnings never affect it, so
"valid: true next to N warnings" is coherent by construction (warnings are
advisory, counted separately in ``warning_count``).

A separate ``hygiene`` section does light, non-blocking detection of junk
entries (an ``OBSOLETE``/"delete from store" marker sitting in the title or
description) and spec_id prefix mismatches (e.g. a stray ``MCP-RF-001`` in a
store otherwise prefixed ``FE-RF-``) — a report, not an automatic deletion.
"""

from __future__ import annotations

import re
from typing import Any

# OpenSpec requirements use RFC-2119 normative language.
_NORMATIVE_RE = re.compile(r"\b(SHALL|MUST|SHOULD|MAY|REQUIRED)\b")

# Junk / stale-entry markers left in a title or description by someone who
# meant to delete the spec from the store but never did.
_OBSOLETE_RE = re.compile(
    r"\b(OBSOLETE|DELETE\s+FROM\s+STORE|REMOVE\s+FROM\s+STORE)\b", re.IGNORECASE
)

# spec_id prefix, e.g. "BE-RF-102" -> "BE-RF", "SPEC-241" -> "SPEC".
_PREFIX_RE = re.compile(r"^([A-Za-z]+(?:-[A-Za-z]+)*)-\d+")

# A check firing on this fraction (or more) of the corpus is a project-level
# configuration gap, not per-spec signal — only meaningful once the corpus is
# large enough that "100%" isn't just "there are 2 specs".
_PROJECT_LEVEL_MIN_CHECKED = 20
_PROJECT_LEVEL_RATIO = 0.95

DEFAULT_SAMPLE_SIZE = 10
MAX_FINDINGS = 50


def _spec_prefix(spec_id: str) -> str:
    m = _PREFIX_RE.match(spec_id)
    return m.group(1) if m else spec_id


def _detect_hygiene(rows: list[Any], sample_size: int) -> dict[str, Any]:
    """Non-blocking store-hygiene report: junk markers + prefix mismatches."""
    prefix_counts: dict[str, int] = {}
    obsolete: list[str] = []
    by_prefix: dict[str, list[str]] = {}
    for r in rows:
        sid = r["spec_id"]
        prefix = _spec_prefix(sid)
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
        by_prefix.setdefault(prefix, []).append(sid)
        text = f"{r['title'] or ''} {r['description'] or ''}"
        if _OBSOLETE_RE.search(text):
            obsolete.append(sid)

    dominant_prefix: str | None = None
    mismatched: list[str] = []
    if prefix_counts:
        dominant_prefix = max(prefix_counts, key=lambda p: prefix_counts[p])
        for prefix, ids in by_prefix.items():
            if prefix != dominant_prefix:
                mismatched.extend(ids)

    return {
        "dominant_prefix": dominant_prefix,
        "prefix_counts": prefix_counts,
        "mismatched_prefix_specs": mismatched[:sample_size],
        "mismatched_prefix_count": len(mismatched),
        "obsolete_marked_specs": obsolete[:sample_size],
        "obsolete_marked_count": len(obsolete),
    }


def validate_openspec(
    conn: Any,
    project_id: int,
    *,
    strict: bool = False,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Check every non-deprecated spec against OpenSpec structural rules.

    Findings are grouped by ``(issue, severity)`` — see module docstring for
    why. ``sample_size`` bounds how many ``{spec_id, title}`` examples ride
    along per finding group (default 10); the group's ``count`` is always
    the true total even when the sample is capped.
    """
    rows = conn.execute(
        """SELECT sp.id, sp.spec_id, sp.title, sp.description,
                  (SELECT COUNT(*) FROM spec_scenario ss WHERE ss.spec_id=sp.id)
                      AS scenario_count
           FROM spec sp
           WHERE sp.project_id=? AND sp.status != 'deprecated'
           ORDER BY sp.spec_id""",
        (project_id,),
    ).fetchall()

    checked = len(rows)
    # (issue, severity) -> running count / bounded sample of {spec_id, title}
    counts: dict[tuple[str, str], int] = {}
    samples: dict[tuple[str, str], list[dict[str, Any]]] = {}
    without_scenarios: list[str] = []

    def _record(severity: str, issue: str, spec_id: str, title: str | None) -> None:
        key = (issue, severity)
        counts[key] = counts.get(key, 0) + 1
        bucket = samples.setdefault(key, [])
        if len(bucket) < sample_size:
            bucket.append({"spec_id": spec_id, "title": title})

    for r in rows:
        sid, title = r["spec_id"], r["title"]
        desc = (r["description"] or "").strip()

        if not title or not title.strip():
            # Always an error, unconditionally (missing title is never OK).
            _record("error", "requirement has no title", sid, title)

        if r["scenario_count"] == 0:
            without_scenarios.append(sid)
            # OpenSpec's defining rule. Always a finding; an error under --strict.
            _record(
                "error" if strict else "warning",
                "requirement has no scenario (OpenSpec requires >=1)",
                sid,
                title,
            )

        if not desc:
            _record("warning", "requirement has empty body", sid, title)
        elif not _NORMATIVE_RE.search(desc):
            _record(
                "error" if strict else "warning",
                "requirement body has no normative keyword (SHALL/MUST/SHOULD/MAY)",
                sid,
                title,
            )

    error_count = sum(n for (_, sev), n in counts.items() if sev == "error")
    warning_count = sum(n for (_, sev), n in counts.items() if sev == "warning")

    findings: list[dict[str, Any]] = []
    for (issue, severity), n in counts.items():
        project_level = (
            checked >= _PROJECT_LEVEL_MIN_CHECKED and n >= checked * _PROJECT_LEVEL_RATIO
        )
        findings.append(
            {
                "issue": issue,
                "severity": severity,
                "count": n,
                "project_level": project_level,
                "sample": samples[(issue, severity)],
                "sample_truncated": n > len(samples[(issue, severity)]),
            }
        )
    findings.sort(key=lambda f: (-f["count"], f["issue"]))
    findings_truncated = len(findings) > MAX_FINDINGS
    findings = findings[:MAX_FINDINGS]

    without_scenarios_capped = without_scenarios[:sample_size]

    return {
        # Coherent by construction: errors alone gate validity, warnings are
        # advisory and counted separately.
        "valid": error_count == 0,
        "strict": strict,
        "checked": checked,
        "error_count": error_count,
        "warning_count": warning_count,
        "findings": findings,
        "findings_truncated": findings_truncated,
        # Kept for backward compat with existing callers that just check
        # membership; capped + counted rather than one entry per spec.
        "specs_without_scenarios": without_scenarios_capped,
        "specs_without_scenarios_count": len(without_scenarios),
        "specs_without_scenarios_truncated": len(without_scenarios)
        > len(without_scenarios_capped),
        "hygiene": _detect_hygiene(rows, sample_size),
    }
