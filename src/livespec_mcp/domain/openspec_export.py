"""Export the Spec DB to an on-disk OpenSpec (Fission-AI) tree (v0.22).

Closes the round-trip: livespec has read OpenSpec markdown since v0.21; this
writes it back out. Given a project's specs (+ scenarios, + change proposals),
emit the canonical OpenSpec layout::

    <root>/specs/<capability>/spec.md      # source-of-truth requirements
    <root>/changes/<name>/proposal.md      # proposed changes (status != archived)
    <root>/changes/<name>/design.md
    <root>/changes/<name>/tasks.md
    <root>/changes/<name>/specs/<cap>/spec.md   # delta requirements
    <root>/archive/<name>/...               # archived changes

Capability == the spec's ``module`` (OpenSpec's unit of a spec file). Specs
with no module are grouped under a single ``general`` capability. Only
non-deprecated specs are emitted as canonical requirements — a deprecated spec
represents removed behaviour and has no place in the living source of truth.

The emitted markdown re-parses cleanly through ``parse_openspec_markdown`` so
export -> import is stable for module-scoped specs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from livespec_mcp.domain.md_specs import _OSPEC_SCENARIO_RE, _slugify

_DEFAULT_CAPABILITY = "general"
_OPERATION_HEADERS = {
    "added": "## ADDED Requirements",
    "modified": "## MODIFIED Requirements",
    "removed": "## REMOVED Requirements",
    "renamed": "## RENAMED Requirements",
}

# Stable id marker — HTML comment is inert for OpenSpec validate / Fission tooling.
_LIVESPEC_ID_COMMENT = "<!-- livespec:id={sid} -->"


def _capability_title(cap: str) -> str:
    words = cap.replace("_", " ").replace("-", " ").split()
    return " ".join(w if w.isupper() else w.capitalize() for w in words) or cap


def _requirement_prose(description: str | None) -> str:
    """The requirement body with any embedded ``#### Scenario:`` blocks removed.

    Scenarios that were flattened into the description at import time are cut so
    the exporter can re-emit them structurally from ``spec_scenario`` without
    duplicating them. Also strips ``<!-- livespec:id=... -->`` (re-emitted
    via ``spec_id=``)."""
    if not description:
        return ""
    out: list[str] = []
    for ln in description.splitlines():
        if _OSPEC_SCENARIO_RE.match(ln.rstrip()):
            break
        if "livespec:id=" in ln and ln.strip().startswith("<!--"):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _emit_requirement(
    title: str,
    description: str | None,
    scenarios: list[tuple[str, str]],
    *,
    spec_id: str | None = None,
) -> list[str]:
    """Render one ``### Requirement:`` block (+ its scenarios) as md lines.

    When ``spec_id`` is set, emit ``<!-- livespec:id=... -->`` immediately under
    the heading so ``sync_openspec`` / ``parse_openspec_markdown`` can round-trip
    without slugifying a fresh id (avoids SPEC-001 → indexing-foo duplicates).
    """
    block: list[str] = [f"### Requirement: {title}", ""]
    if spec_id:
        block += [_LIVESPEC_ID_COMMENT.format(sid=spec_id), ""]
    if scenarios:
        prose = _requirement_prose(description)
        if prose:
            block += [prose, ""]
        for name, body in scenarios:
            block += [f"#### Scenario: {name}"]
            block += [body] if body else []
            block += [""]
    else:
        # No structured scenarios: emit the description verbatim (it may already
        # contain embedded scenarios for legacy-imported specs, or be plain
        # prose for a spec authored via create_spec). Strip re-import id markers.
        if description and description.strip():
            cleaned = "\n".join(
                ln
                for ln in description.splitlines()
                if not (
                    ln.strip().startswith("<!--") and "livespec:id=" in ln
                )
            ).strip()
            if cleaned:
                block += [cleaned, ""]
    return block


def _scenarios_for(conn: Any, spec_pk: int) -> list[tuple[str, str]]:
    return [
        (r["name"], r["body"])
        for r in conn.execute(
            "SELECT name, body FROM spec_scenario WHERE spec_id=? ORDER BY ordinal, id",
            (spec_pk,),
        )
    ]


def _write(path: Path, text: str, written: list[str], root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        written.append(str(path.relative_to(root)))
    except ValueError:
        written.append(str(path))


def export_openspec(
    conn: Any,
    project_id: int,
    root: Path,
    *,
    include_changes: bool = True,
) -> dict[str, Any]:
    """Write the project's specs (+ changes) to an OpenSpec tree at ``root``.

    Returns a summary: capabilities written, spec/scenario/change counts, and
    the list of files written (relative to ``root``)."""
    root = Path(root)
    written: list[str] = []

    # --- canonical specs, grouped by capability (module) ---
    rows = conn.execute(
        """SELECT sp.id, sp.spec_id, sp.title, sp.description,
                  m.name AS module, m.description AS module_purpose
           FROM spec sp LEFT JOIN module m ON m.id=sp.module_id
           WHERE sp.project_id=? AND sp.status != 'deprecated'
           ORDER BY m.name, sp.spec_id""",
        (project_id,),
    ).fetchall()

    by_cap: dict[str, list[Any]] = {}
    cap_purpose: dict[str, str] = {}
    for r in rows:
        cap = r["module"] or _DEFAULT_CAPABILITY
        by_cap.setdefault(cap, []).append(r)
        if r["module_purpose"] and cap not in cap_purpose:
            cap_purpose[cap] = r["module_purpose"]

    spec_count = 0
    scenario_count = 0
    for cap, specs in sorted(by_cap.items()):
        lines: list[str] = [f"# {_capability_title(cap)} Specification", ""]
        # Re-emit the stored ## Purpose verbatim when we captured one on import;
        # otherwise synthesize a minimal placeholder so the file stays valid.
        purpose_body = cap_purpose.get(cap) or (
            f"The `{cap}` capability. Exported by livespec."
        )
        lines += [
            "## Purpose",
            purpose_body,
            "",
            "## Requirements",
            "",
        ]
        for r in specs:
            scenarios = _scenarios_for(conn, int(r["id"]))
            scenario_count += len(scenarios)
            lines += _emit_requirement(
                r["title"], r["description"], scenarios, spec_id=r["spec_id"]
            )
            spec_count += 1
        text = "\n".join(lines).rstrip() + "\n"
        _write(root / "specs" / _slugify(cap) / "spec.md", text, written, root)

    result: dict[str, Any] = {
        "root": str(root),
        "capabilities": sorted(by_cap),
        "specs_written": spec_count,
        "scenarios_written": scenario_count,
        "files": written,
    }

    if include_changes:
        result["changes_written"] = _export_changes(conn, project_id, root, written)

    return result


def _export_changes(
    conn: Any, project_id: int, root: Path, written: list[str]
) -> int:
    changes = conn.execute(
        "SELECT id, name, status, proposal, design, tasks FROM spec_change "
        "WHERE project_id=? ORDER BY name",
        (project_id,),
    ).fetchall()
    n = 0
    for ch in changes:
        base_dir = "archive" if ch["status"] == "archived" else "changes"
        change_root = root / base_dir / _slugify(ch["name"])
        for fname, content in (
            ("proposal.md", ch["proposal"]),
            ("design.md", ch["design"]),
            ("tasks.md", ch["tasks"]),
        ):
            if content and content.strip():
                _write(change_root / fname, content.rstrip() + "\n", written, root)
        deltas = conn.execute(
            """SELECT operation, capability, title, description
               FROM spec_change_delta WHERE change_id=? ORDER BY ordinal, id""",
            (ch["id"],),
        ).fetchall()
        # Group deltas by capability -> operation so each capability gets one
        # delta spec.md with ordered ## ADDED/MODIFIED/REMOVED/RENAMED sections.
        by_cap: dict[str, dict[str, list[Any]]] = {}
        for d in deltas:
            cap = d["capability"] or _DEFAULT_CAPABILITY
            by_cap.setdefault(cap, {}).setdefault(d["operation"], []).append(d)
        for cap, ops in sorted(by_cap.items()):
            lines: list[str] = [f"# {_capability_title(cap)} (delta)", ""]
            for op in ("added", "modified", "removed", "renamed"):
                if op not in ops:
                    continue
                lines += [_OPERATION_HEADERS[op], ""]
                for d in ops[op]:
                    # Deltas may not carry a livespec id yet; title-only is fine.
                    lines += _emit_requirement(d["title"], d["description"], [])
            text = "\n".join(lines).rstrip() + "\n"
            _write(change_root / "specs" / _slugify(cap) / "spec.md", text, written, root)
        n += 1
    return n
