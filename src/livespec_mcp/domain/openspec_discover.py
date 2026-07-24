"""Discover and sync an on-disk OpenSpec tree (v0.22, Layer 3).

OpenSpec projects keep everything under an ``openspec/`` directory at the repo
root, optionally described by an ``openspec.json`` config. This module finds
that root and drives a full one-call sync: canonical specs from
``openspec/specs/`` plus change proposals from ``openspec/changes/`` and
``openspec/archive/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONFIG_NAMES = ("openspec.json",)


def discover_openspec_root(workspace: Path, explicit: str | Path | None = None) -> Path | None:
    """Return the OpenSpec root for ``workspace``.

    ``explicit`` (a tool arg or ``[specs].openspec_dir``) wins; otherwise probe
    the conventional ``<workspace>/openspec``. Returns ``None`` when neither
    exists so callers can surface a clean, shaped error."""
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = workspace / p
        return p if p.is_dir() else None
    candidate = workspace / "openspec"
    return candidate if candidate.is_dir() else None


def read_openspec_config(root: Path) -> dict[str, Any]:
    """Read ``openspec.json`` at ``root`` (or its parent). Missing/invalid → {}."""
    for parent in (root, root.parent):
        for name in _CONFIG_NAMES:
            fp = parent / name
            if fp.is_file():
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    return data if isinstance(data, dict) else {}
                except (OSError, json.JSONDecodeError):
                    return {}
    return {}


def sync_openspec_tree(st: Any, root: Path) -> dict[str, Any]:
    """Import specs + changes from an OpenSpec ``root`` in one pass.

    Canonical requirements come from ``root/specs`` only (so change deltas are
    never mistaken for source-of-truth specs); ``root/changes`` and
    ``root/archive`` are ingested as change proposals."""
    from livespec_mcp.domain.openspec_changes import import_changes_tree
    from livespec_mcp.domain.specs_sync import import_specs_from_markdown_file

    root = Path(root)
    specs_dir = root / "specs"
    # Canonical requirements come ONLY from <root>/specs. If that dir is absent
    # (a change-only repo), do NOT fall back to walking the whole root — that
    # would slurp in-flight change *deltas* as source-of-truth specs. The
    # canonical set then comes from applying changes.
    if specs_dir.is_dir():
        specs_result = import_specs_from_markdown_file(
            st, specs_dir, fmt="openspec", check_duplicates=False
        )
    else:
        specs_result = {
            "created": 0,
            "updated": 0,
            "parsed": 0,
            "note": "no specs/ directory — canonical specs come from applied changes",
        }
    changes_result = import_changes_tree(st, root)
    return {
        "root": str(root),
        "config": read_openspec_config(root),
        "specs": specs_result,
        "changes": changes_result,
    }
