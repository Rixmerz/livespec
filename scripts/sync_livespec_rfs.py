#!/usr/bin/env python3
"""Sync RFs from ``.livespec.toml`` + optional links seed (brownfield bootstrap).

Reads ``[requirements].sync_from`` and ``[requirements].links_seed`` and runs
the same logic as the post-``index_project`` hook — useful after clone or when
you edit the markdown spec without a full re-index.

Usage:
    uv run python scripts/sync_livespec_rfs.py
    uv run python scripts/sync_livespec_rfs.py /path/to/repo
    LIVESPEC_WORKSPACE=/path/to/repo uv run python scripts/sync_livespec_rfs.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    workspace = Path(os.environ.get("LIVESPEC_WORKSPACE", sys.argv[1] if len(sys.argv) > 1 else ".")).resolve()
    from livespec_mcp.config import load_repo_config
    from livespec_mcp.domain.requirements_sync import sync_requirements_from_config
    from livespec_mcp.state import get_state

    cfg = load_repo_config(workspace)
    if not cfg.requirements_sync_from and not cfg.requirements_links_seed:
        print(
            "No [requirements] sync_from / links_seed in .livespec.toml — nothing to do.",
            file=sys.stderr,
        )
        return 1
    st = get_state(str(workspace))
    result = sync_requirements_from_config(st)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
