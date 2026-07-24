"""Validate the Spec DB against OpenSpec (Fission-AI) structural rules (v0.22).

Mirrors what ``openspec validate [--strict]`` checks, at the DB level, so an
agent can ask "is this spec set OpenSpec-valid?" before exporting or handing
the tree to an OpenSpec-native workflow. The load-bearing rule is OpenSpec's
own invariant: **every requirement MUST have at least one scenario.**

Findings are split into ``errors`` (block validity) and ``warnings`` (advisory).
In ``strict`` mode the requirement-without-scenario and missing-normative-keyword
findings are promoted to errors, matching ``--strict``.
"""

from __future__ import annotations

import re
from typing import Any

# OpenSpec requirements use RFC-2119 normative language.
_NORMATIVE_RE = re.compile(r"\b(SHALL|MUST|SHOULD|MAY|REQUIRED)\b")


def validate_openspec(
    conn: Any, project_id: int, *, strict: bool = False
) -> dict[str, Any]:
    """Check every non-deprecated spec against OpenSpec structural rules."""
    rows = conn.execute(
        """SELECT sp.id, sp.spec_id, sp.title, sp.description,
                  (SELECT COUNT(*) FROM spec_scenario ss WHERE ss.spec_id=sp.id)
                      AS scenario_count
           FROM spec sp
           WHERE sp.project_id=? AND sp.status != 'deprecated'
           ORDER BY sp.spec_id""",
        (project_id,),
    ).fetchall()

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    without_scenarios: list[str] = []

    def _add(bucket_strict_error: bool, spec_id: str, title: str, issue: str) -> None:
        finding = {"spec_id": spec_id, "title": title, "issue": issue}
        (errors if (strict and bucket_strict_error) else warnings).append(finding)

    for r in rows:
        sid, title = r["spec_id"], r["title"]
        desc = (r["description"] or "").strip()

        if not title or not title.strip():
            errors.append(
                {"spec_id": sid, "title": title, "issue": "requirement has no title"}
            )

        if r["scenario_count"] == 0:
            without_scenarios.append(sid)
            # OpenSpec's defining rule. Always a finding; an error under --strict.
            _add(True, sid, title, "requirement has no scenario (OpenSpec requires >=1)")

        if not desc:
            warnings.append(
                {"spec_id": sid, "title": title, "issue": "requirement has empty body"}
            )
        elif not _NORMATIVE_RE.search(desc):
            _add(
                True,
                sid,
                title,
                "requirement body has no normative keyword (SHALL/MUST/SHOULD/MAY)",
            )

    return {
        "valid": len(errors) == 0,
        "strict": strict,
        "checked": len(rows),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "specs_without_scenarios": without_scenarios,
    }
