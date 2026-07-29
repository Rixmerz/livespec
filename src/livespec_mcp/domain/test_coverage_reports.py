"""Read conventional JavaScript test coverage reports.

Reports are optional evidence: malformed or absent reports produce no coverage
instead of making an analysis tool fail.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

_COVERAGE_REPORTS = (
    ("coverage-final.json", "json"),
    ("lcov.info", "lcov"),
)


def discover_report_coverage(workspace: Path) -> dict[str, set[int]]:
    """Return project-relative paths mapped to lines covered by Jest/Vitest."""
    covered: dict[str, set[int]] = defaultdict(set)
    for filename, report_type in _COVERAGE_REPORTS:
        report_path = workspace / "coverage" / filename
        if not report_path.is_file():
            continue
        try:
            if report_type == "json":
                _merge_istanbul_json(covered, workspace, report_path)
            else:
                _merge_lcov(covered, workspace, report_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return dict(covered)


def _merge_istanbul_json(
    covered: dict[str, set[int]], workspace: Path, report_path: Path
) -> None:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return
    for raw_path, entry in data.items():
        if not isinstance(raw_path, str) or not isinstance(entry, dict):
            continue
        path = _relative_path(workspace, raw_path)
        if path is None:
            continue
        _merge_istanbul_map(covered[path], entry, "statementMap", "s")
        _merge_istanbul_map(covered[path], entry, "fnMap", "f")


def _merge_istanbul_map(
    covered_lines: set[int], entry: dict[str, Any], map_name: str, count_name: str
) -> None:
    locations = entry.get(map_name)
    counts = entry.get(count_name)
    if not isinstance(locations, dict) or not isinstance(counts, dict):
        return
    for key, location in locations.items():
        if not isinstance(location, dict) or not _count_is_covered(counts.get(key)):
            continue
        start = location.get("start")
        if isinstance(start, dict) and isinstance(start.get("line"), int):
            covered_lines.add(start["line"])


def _count_is_covered(count: object) -> bool:
    if isinstance(count, list):
        return any(isinstance(value, (int, float)) and value > 0 for value in count)
    return isinstance(count, (int, float)) and count > 0


def _merge_lcov(covered: dict[str, set[int]], workspace: Path, report_path: Path) -> None:
    current_path: str | None = None
    for line in report_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("SF:"):
            current_path = _relative_path(workspace, line[3:])
        elif current_path is not None and line.startswith("DA:"):
            _add_lcov_line(covered[current_path], line[3:])


def _add_lcov_line(covered_lines: set[int], payload: str) -> None:
    line_number, separator, hits = payload.partition(",")
    if not separator:
        return
    try:
        if int(hits) > 0:
            covered_lines.add(int(line_number))
    except ValueError:
        return


def _relative_path(workspace: Path, raw_path: str) -> str | None:
    path = Path(raw_path)
    try:
        if path.is_absolute():
            return path.resolve().relative_to(workspace.resolve()).as_posix()
        normalized = (workspace / path).resolve()
        return normalized.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return None
