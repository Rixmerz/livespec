"""The plugin's pinned server version must match the package it ships from.

`plugin/.mcp.json` runs the server with `uvx livespec@<version>`. Pinning is
what makes the plugin reproducible — unpinned, the server silently becomes
whatever PyPI resolves to, which is how a plugin ends up running code its own
manifest never described.

A pin only helps while it is current. Left behind after a release, it quietly
holds every install on an old server while the repo, the changelog and the
plugin version all claim otherwise — the same declared-vs-actual gap the pin
exists to close. So the pin is asserted against `pyproject.toml`, not trusted.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _package_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _pinned_version() -> str:
    cfg = json.loads((REPO / "plugin" / ".mcp.json").read_text(encoding="utf-8"))
    args = cfg["mcpServers"]["livespec"]["args"]
    spec = next((a for a in args if a.startswith("livespec")), None)
    assert spec is not None, f"no livespec spec found in .mcp.json args: {args}"
    m = re.fullmatch(r"livespec@(.+)", spec)
    assert m, (
        f"server spec {spec!r} is not pinned — it must be 'livespec@<version>' so "
        f"the plugin runs a known server build"
    )
    return m.group(1)


def test_mcp_json_pin_matches_package_version():
    assert _pinned_version() == _package_version(), (
        f"plugin/.mcp.json pins livespec@{_pinned_version()} but pyproject.toml "
        f"declares {_package_version()} — bump the pin with the release"
    )
