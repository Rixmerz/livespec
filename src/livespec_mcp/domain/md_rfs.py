"""Parse a Markdown file containing RF definitions.

Expected format (loose; the parser tolerates whitespace and order):

    ## RF-001: Title
    **Prioridad:** alta · **Módulo:** auth
    description...
    blank line
    ## RF-002: ...

Recognised priority synonyms (Spanish / English):
    crítica/critical, alta/high, media/medium, baja/low

Status keywords: draft, active, deprecated. Default = active.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADER_RE = re.compile(r"^##+\s+(?P<rf>RF[-_]?\d+)\s*[:\-]\s*(?P<title>.+?)\s*$")
# Match `Prioridad: value` after stripping markdown bold markers.
_META_RE = re.compile(
    r"\b(prioridad|priority|módulo|modulo|module|status|estado)\s*[:=]\s*"
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


@dataclass
class ParsedRf:
    rf_id: str  # normalized, e.g. "RF-001"
    title: str
    description: str
    priority: str = "medium"
    status: str = "active"
    module: str | None = None


def _normalize_rf(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    return f"RF-{int(digits):03d}" if digits else raw.upper()


def parse_rfs_markdown(text: str) -> list[ParsedRf]:
    """Walk the markdown line by line, splitting on `## RF-NNN: Title` headers."""
    rfs: list[ParsedRf] = []
    current: dict | None = None
    description_lines: list[str] = []

    def _flush() -> None:
        if current is None:
            return
        desc = "\n".join(description_lines).strip()
        rfs.append(ParsedRf(
            rf_id=current["rf_id"],
            title=current["title"],
            description=desc,
            priority=current.get("priority", "medium"),
            status=current.get("status", "active"),
            module=current.get("module"),
        ))

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            current = {
                "rf_id": _normalize_rf(m.group("rf")),
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
            continue
        description_lines.append(raw_line)

    _flush()
    return rfs
