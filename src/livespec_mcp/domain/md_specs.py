"""Parse a Markdown file containing Spec definitions.

**Preferred authoring format (OpenSpec / Fission-AI)** — write under ``openspec/``:

    ### Requirement: User login
    The system SHALL authenticate users with email + password.
    #### Scenario: Valid credentials
    - **WHEN** credentials are valid
    - **THEN** a session token is returned

**Legacy / compat format** (livespec-native catalog — import only; prefer migrating
to OpenSpec for new work):

    ## SPEC-001: Title
    **Prioridad:** alta · **Módulo:** auth · **Kind:** adr
    description...
    blank line
    ## SPEC-002: ...

Recognised priority synonyms (Spanish / English):
    crítica/critical, alta/high, media/medium, baja/low

Status keywords: draft, active, deprecated. Default = active.

Recognised kind synonyms (Spanish / English), default = functional_requirement:
    rf/fr/funcional/functional_requirement, nfr/no funcional/non_functional_requirement,
    adr, design/diseño, constraint/restricción, epic/épica, other/otro
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADER_RE = re.compile(r"^##+\s+(?P<spec>SPEC[-_]?\d+)\s*[:\-]\s*(?P<title>.+?)\s*$")
# OpenSpec (Fission-AI) interop: `### Requirement: <name>` anchors one spec;
# `## ADDED|MODIFIED|REMOVED Requirements` are the change-delta section headers;
# `#### Scenario: <name>` is a requirement's atomic WHEN/THEN behaviour block.
_OSPEC_REQ_RE = re.compile(r"^###\s+Requirement:\s*(?P<name>.+?)\s*$")
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
# Match `Prioridad: value` after stripping markdown bold markers.
_META_RE = re.compile(
    r"\b(prioridad|priority|módulo|modulo|module|status|estado|kind|tipo)\s*[:=]\s*"
    r"(?P<value>[^\n·•|]+)",
    re.IGNORECASE,
)

_PRIORITY_MAP = {
    "crítica": "critical", "critica": "critical", "critical": "critical",
    "alta": "high", "high": "high",
    "media": "medium", "medium": "medium",
    "baja": "low", "low": "low",
}
_STATUS_MAP = {
    "draft": "draft", "borrador": "draft",
    "active": "active", "activa": "active", "activo": "active",
    "deprecated": "deprecated", "deprecada": "deprecated",
}
_KIND_MAP = {
    "rf": "functional_requirement", "fr": "functional_requirement",
    "funcional": "functional_requirement", "functional": "functional_requirement",
    "functional_requirement": "functional_requirement",
    "nfr": "non_functional_requirement", "no funcional": "non_functional_requirement",
    "non_functional_requirement": "non_functional_requirement",
    "adr": "adr",
    "design": "design", "diseño": "design", "diseno": "design",
    "constraint": "constraint", "restricción": "constraint", "restriccion": "constraint",
    "epic": "epic", "épica": "epic", "epica": "epic",
    "other": "other", "otro": "other",
}


@dataclass
class ParsedSpec:
    spec_id: str  # normalized, e.g. "SPEC-001"
    title: str
    description: str
    priority: str = "medium"
    status: str = "active"
    module: str | None = None
    kind: str = "functional_requirement"
    # OpenSpec interop: `#### Scenario:` blocks under the requirement, as
    # (name, body) pairs in source order. Empty for native livespec specs.
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


def _normalize_spec(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    return f"SPEC-{int(digits):03d}" if digits else raw.upper()


def parse_specs_markdown(text: str) -> list[ParsedSpec]:
    """Walk the markdown line by line, splitting on `## SPEC-NNN: Title` headers."""
    specs: list[ParsedSpec] = []
    current: dict | None = None
    description_lines: list[str] = []

    def _flush() -> None:
        if current is None:
            return
        desc = "\n".join(description_lines).strip()
        specs.append(ParsedSpec(
            spec_id=current["spec_id"],
            title=current["title"],
            description=desc,
            priority=current.get("priority", "medium"),
            status=current.get("status", "active"),
            module=current.get("module"),
            kind=current.get("kind", "functional_requirement"),
        ))

    in_fence = False
    fence_marker = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()

        # Fenced code blocks: never parse headers/metadata inside ``` / ~~~
        # so a `## SPEC-099:` shown as an EXAMPLE in a code block doesn't
        # become a phantom spec. Fence content stays in the description.
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

        m = _HEADER_RE.match(line)
        if m:
            _flush()
            current = {
                "spec_id": _normalize_spec(m.group("spec")),
                "title": m.group("title").strip(),
            }
            description_lines = []
            continue
        if current is None:
            continue
        # Metadata lines (Prioridad, Módulo, Status) — accumulate; do not include
        # in description. Strip markdown bold/italic markers first so the regex
        # doesn't have to handle every `**Name:**` / `**Name**: ` permutation.
        # Only treat a line as metadata when a key sits at its START (after
        # bullets/markers) — otherwise prose like "must show status: active
        # users" would be swallowed and mis-set a field.
        cleaned = line.replace("**", "").replace("__", "")
        meta_start = cleaned.lstrip(" \t-*·•>")
        if _META_RE.match(meta_start):
            for h in _META_RE.finditer(cleaned):
                key = h.group(1).lower()
                value = h.group("value").strip().rstrip(".").lower()
                if key in ("prioridad", "priority"):
                    current["priority"] = _PRIORITY_MAP.get(value, "medium")
                elif key in ("módulo", "modulo", "module"):
                    current["module"] = value
                elif key in ("status", "estado"):
                    current["status"] = _STATUS_MAP.get(value, "active")
                elif key in ("kind", "tipo"):
                    current["kind"] = _KIND_MAP.get(value, "functional_requirement")
            continue
        description_lines.append(raw_line)

    _flush()
    return specs


# ---------- OpenSpec (Fission-AI) interop ----------


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
    numeric id. We derive a readable, stable slug id (free-text `spec_id`
    column allows it, same as legacy ``RF-042`` values). The capability
    prefix disambiguates same-named requirements across capabilities.
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
    """
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


def detect_spec_format(text: str) -> str:
    """Return ``"openspec"`` or ``"livespec"`` for a markdown spec file.

    OpenSpec files use ``### Requirement:`` anchors; the legacy livespec-native
    catalog uses ``## SPEC-NNN:`` headers. When a file has both (unusual),
    **OpenSpec wins** — authoring SSoT is OpenSpec; native headers are treated as
    leftover noise rather than forcing the legacy parser.
    """
    has_livespec = any(_HEADER_RE.match(ln) for ln in text.splitlines())
    has_openspec = bool(re.search(r"^###\s+Requirement:", text, re.MULTILINE))
    if has_openspec:
        return "openspec"
    if has_livespec:
        return "livespec"
    return "livespec"
