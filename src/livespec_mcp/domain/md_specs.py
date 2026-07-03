"""Parse a Markdown file containing Spec definitions.

Expected format (loose; the parser tolerates whitespace and order):

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
from dataclasses import dataclass

_HEADER_RE = re.compile(r"^##+\s+(?P<spec>SPEC[-_]?\d+)\s*[:\-]\s*(?P<title>.+?)\s*$")
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

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
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
        cleaned = line.replace("**", "").replace("__", "")
        meta_hits = list(_META_RE.finditer(cleaned))
        if meta_hits:
            for h in meta_hits:
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
