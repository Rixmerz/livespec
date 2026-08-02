"""Parse OpenSpec (Fission-AI) Markdown into Spec definitions.

Authoring lives under ``openspec/``:

    ### Requirement: User login
    The system SHALL authenticate users with email + password.
    #### Scenario: Valid credentials
    - **WHEN** credentials are valid
    - **THEN** a session token is returned

The former livespec-native ``## SPEC-NNN:`` catalog is removed (hard cut).
Migrate those files to ``openspec/specs/<capability>/spec.md`` and sync.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# OpenSpec (Fission-AI) interop: `### Requirement: <name>` anchors one spec;
# `## ADDED|MODIFIED|REMOVED Requirements` are the change-delta section headers;
# `#### Scenario: <name>` is a requirement's atomic WHEN/THEN behaviour block.
_OSPEC_REQ_RE = re.compile(r"^###\s+Requirement:\s*(?P<name>.+?)\s*$")
# Stable id written by export_openspec — prefer over title slug on re-import.
_LIVESPEC_ID_RE = re.compile(
    r"^<!--\s*livespec:id=(?P<sid>[^\s>]+)\s*-->\s*$", re.IGNORECASE
)
_OSPEC_DELTA_RE = re.compile(
    r"^##\s+(?P<verb>ADDED|MODIFIED|REMOVED|RENAMED)\s+Requirements\b", re.IGNORECASE
)
_OSPEC_SCENARIO_RE = re.compile(r"^####\s+Scenario:\s*(?P<name>.+?)\s*$")
_OSPEC_PURPOSE_RE = re.compile(r"^##\s+Purpose\s*$", re.IGNORECASE)
# `## RENAMED Requirements` uses FROM/TO bullets. The name may be bare or wrapped
# as `### Requirement: <name>` (optionally backticked).
_OSPEC_RENAME_RE = re.compile(
    r"^\s*[-*]\s*(?P<dir>FROM|TO)\s*:\s*(?P<val>.+?)\s*$", re.IGNORECASE
)
# Detected only to reject — the native catalog dialect is gone.
_LEGACY_SPEC_HEADER_RE = re.compile(
    r"^##+\s+SPEC[-_]?\d+\s*[:\-]\s*.+?\s*$", re.IGNORECASE
)


class UnsupportedSpecCatalogError(ValueError):
    """Markdown uses the removed ``## SPEC-NNN:`` dialect."""


def _clean_rename_value(raw: str) -> str:
    """Strip backticks and a leading ``### Requirement:`` from a FROM/TO value."""
    v = raw.strip().strip("`").strip()
    m = re.match(r"^#*\s*Requirement:\s*(?P<name>.+?)\s*$", v, re.IGNORECASE)
    return (m.group("name") if m else v).strip()


def extract_purpose(text: str) -> str | None:
    """Return the body of the ``## Purpose`` section of an OpenSpec spec file.

    Everything between ``## Purpose`` and the next ``##`` heading (or EOF),
    trimmed. ``None`` when there is no Purpose section. Fenced code is passed
    through untouched (a ``## Purpose`` inside a fence is ignored)."""
    lines = text.splitlines()
    out: list[str] = []
    capturing = False
    in_fence = False
    fence_marker = ""
    for raw in lines:
        stripped = raw.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif stripped.startswith(fence_marker):
                in_fence, fence_marker = False, ""
            if capturing:
                out.append(raw)
            continue
        if in_fence:
            if capturing:
                out.append(raw)
            continue
        if _OSPEC_PURPOSE_RE.match(raw.rstrip()):
            capturing = True
            continue
        if capturing and re.match(r"^##(?!#)", stripped):
            break
        if capturing:
            out.append(raw)
    body = "\n".join(out).strip()
    return body or None


def reject_legacy_spec_catalog(text: str) -> None:
    """Raise if ``text`` still uses the removed ``## SPEC-NNN:`` dialect.

    Headers inside fenced code blocks are ignored (examples stay allowed).
    """
    in_fence = False
    fence_marker = ""
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif stripped.startswith(fence_marker):
                in_fence, fence_marker = False, ""
            continue
        if in_fence:
            continue
        if _LEGACY_SPEC_HEADER_RE.match(raw.rstrip()):
            raise UnsupportedSpecCatalogError(
                "native ## SPEC-NNN: catalogs are removed — migrate to "
                "openspec/specs/<capability>/spec.md (### Requirement:) and "
                "call sync_openspec"
            )


@dataclass
class ParsedSpec:
    spec_id: str  # OpenSpec slug, e.g. "auth-user-login"
    title: str
    description: str
    priority: str = "medium"
    status: str = "active"
    module: str | None = None
    kind: str = "functional_requirement"
    # OpenSpec interop: `#### Scenario:` blocks under the requirement, as
    # (name, body) pairs in source order.
    scenarios: list[tuple[str, str]] = field(default_factory=list)
    # OpenSpec change-delta operation for this requirement: added | modified |
    # removed | renamed. ``None`` for a canonical (non-delta) spec.
    operation: str | None = None
    # For operation == "renamed": the OLD requirement name (the FROM side of a
    # ``## RENAMED Requirements`` FROM/TO pair). ``title`` holds the new name.
    rename_from: str | None = None
    # The capability's ``## Purpose`` prose (same for every spec parsed from one
    # OpenSpec file); persisted onto the module so export can re-emit it.
    capability_purpose: str | None = None


def _slugify(text: str) -> str:
    """Deterministic, filesystem-free slug for a requirement name.

    Lowercase, non-alphanumeric runs collapse to a single hyphen, edges
    trimmed. Stable across re-imports so idempotency holds (same requirement
    name → same slug → UPDATE, not a duplicate INSERT)."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "requirement"


def _ospec_spec_id(name: str, capability: str | None) -> str:
    """Slug spec_id for an OpenSpec requirement.

    OpenSpec identifies requirements by name within a capability, not by a
    numeric id. We derive a readable, stable slug id. The capability prefix
    disambiguates same-named requirements across capabilities.
    """
    name_slug = _slugify(name)
    if capability:
        return f"{_slugify(capability)}-{name_slug}"
    return name_slug


def extract_scenarios(description: str) -> list[tuple[str, str]]:
    """Pull ``#### Scenario: <name>`` blocks out of a requirement's body.

    Returns ``(name, body)`` pairs in source order; ``body`` is the raw
    markdown under the heading (typically the ``- **WHEN** … / - **THEN** …``
    bullet list), trimmed. A subsequent ``##``/``###``/``####`` heading closes
    the current scenario. Fenced code blocks are skipped so an example
    ``#### Scenario:`` inside ``` never spawns a phantom scenario. Duplicate
    names keep the first occurrence (mirrors the UNIQUE(spec_id, name) row
    constraint). Idempotent and side-effect free — reused by both import and
    export."""
    scenarios: list[tuple[str, str]] = []
    seen: set[str] = set()
    name: str | None = None
    body: list[str] = []
    in_fence = False
    fence_marker = ""

    def _flush() -> None:
        nonlocal name
        if name is not None and name not in seen:
            seen.add(name)
            scenarios.append((name, "\n".join(body).strip()))

    for raw in description.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif stripped.startswith(fence_marker):
                in_fence, fence_marker = False, ""
            if name is not None:
                body.append(raw)
            continue
        if in_fence:
            if name is not None:
                body.append(raw)
            continue
        m = _OSPEC_SCENARIO_RE.match(raw.rstrip())
        if m:
            _flush()
            name = m.group("name").strip()
            body = []
            continue
        # Any other markdown heading closes the current scenario body.
        if name is not None and re.match(r"^#{2,4}(?!#)\s", stripped):
            _flush()
            name = None
            body = []
            continue
        if name is not None:
            body.append(raw)
    _flush()
    return scenarios


def parse_openspec_markdown(
    text: str, *, capability: str | None = None
) -> list[ParsedSpec]:
    """Parse an OpenSpec-format markdown file into ``ParsedSpec`` objects.

    Anchors on ``### Requirement: <name>`` headings. The prose + any
    ``#### Scenario:`` blocks under a requirement become its description
    verbatim (SHALL statements and WHEN/THEN scenarios are preserved).

    Change-delta sections drive status: requirements under
    ``## REMOVED Requirements`` are imported as ``deprecated``; everything
    else (``## ADDED``/``## MODIFIED`` and plain canonical ``## Requirements``
    specs) is ``active``. ``kind`` defaults to ``functional_requirement`` —
    OpenSpec requirements are functional by nature; reclassify with
    ``update_spec`` if needed.

    Raises ``UnsupportedSpecCatalogError`` when the file still uses
    ``## SPEC-NNN:`` headers (removed dialect).
    """
    reject_legacy_spec_catalog(text)

    specs: list[ParsedSpec] = []
    current: dict | None = None
    description_lines: list[str] = []
    delta_status = "active"  # canonical specs (no delta header) are active
    delta_op: str | None = None  # ADDED/MODIFIED/REMOVED/RENAMED; None = canonical
    pending_from: str | None = None  # FROM side of a RENAMED FROM/TO pair
    purpose = extract_purpose(text)  # capability-level ## Purpose, same for all

    def _flush() -> None:
        if current is None:
            return
        desc = "\n".join(description_lines).strip()
        specs.append(
            ParsedSpec(
                spec_id=current["spec_id"],
                title=current["title"],
                description=desc,
                priority="medium",
                status=current["status"],
                module=capability,
                kind="functional_requirement",
                # Scenarios live verbatim inside the description too (kept for
                # display / search); here we also surface them structurally.
                scenarios=extract_scenarios(desc),
                operation=current["operation"],
                capability_purpose=purpose,
            )
        )

    in_fence = False
    fence_marker = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif stripped.startswith(fence_marker):
                in_fence, fence_marker = False, ""
            if current is not None:
                description_lines.append(raw_line)
            continue
        if in_fence:
            if current is not None:
                description_lines.append(raw_line)
            continue

        delta = _OSPEC_DELTA_RE.match(line)
        if delta:
            # Section header ends the current requirement and sets the status
            # applied to requirements that follow, until the next such header.
            _flush()
            current = None
            description_lines = []
            verb = delta.group("verb").upper()
            delta_status = "deprecated" if verb == "REMOVED" else "active"
            delta_op = verb.lower()
            pending_from = None
            continue

        # RENAMED section: FROM/TO bullet pairs (name change only, no body).
        if delta_op == "renamed":
            rn = _OSPEC_RENAME_RE.match(line)
            if rn:
                val = _clean_rename_value(rn.group("val"))
                if rn.group("dir").upper() == "FROM":
                    pending_from = val
                elif pending_from is not None:  # TO — emit the rename
                    specs.append(
                        ParsedSpec(
                            spec_id=_ospec_spec_id(val, capability),
                            title=val,
                            description="",
                            priority="medium",
                            status="active",
                            module=capability,
                            kind="functional_requirement",
                            operation="renamed",
                            rename_from=pending_from,
                            capability_purpose=purpose,
                        )
                    )
                    pending_from = None
                continue

        req = _OSPEC_REQ_RE.match(line)
        if req:
            _flush()
            name = req.group("name").strip()
            current = {
                "spec_id": _ospec_spec_id(name, capability),
                "title": name,
                "status": delta_status,
                "operation": delta_op,
            }
            description_lines = []
            continue

        # Prefer stable id from export_openspec (<!-- livespec:id=… -->).
        # Must appear before body prose (blank lines after the heading are OK);
        # not kept in the description.
        if current is not None and not any(ln.strip() for ln in description_lines):
            id_m = _LIVESPEC_ID_RE.match(line)
            if id_m:
                current["spec_id"] = id_m.group("sid").strip()
                description_lines = []  # drop leading blanks before the marker
                continue
            if not stripped:
                # Leading blank after ### Requirement — wait for id or prose
                continue

        # Any other level-2/3 heading closes the current requirement body so
        # unrelated sections (## Purpose, ## Why) don't leak into the spec.
        if current is not None and re.match(r"^##(?!#)|^###(?!#)", stripped):
            _flush()
            current = None
            description_lines = []
            continue

        if current is not None:
            description_lines.append(raw_line)

    _flush()
    return specs
