#!/usr/bin/env python3
"""Sync Specs from ``.livespec.toml`` + optional links seed (brownfield bootstrap).

Reads ``[specs].sync_from`` and ``[specs].links_seed`` and runs the same
logic as the post-``index_project`` hook — useful after clone or when you
edit the markdown spec without a full re-index.

Usage:
    uv run python scripts/sync_livespec_specs.py
    uv run python scripts/sync_livespec_specs.py /path/to/repo
    LIVESPEC_WORKSPACE=/path/to/repo uv run python scripts/sync_livespec_specs.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    workspace = Path(os.environ.get("LIVESPEC_WORKSPACE", sys.argv[1] if len(sys.argv) > 1 else ".")).resolve()
    from livespec_mcp.config import load_repo_config
    from livespec_mcp.domain.specs_sync import sync_specs_from_config
    from livespec_mcp.state import get_state

    cfg = load_repo_config(workspace)
    if not cfg.specs_sync_from and not cfg.specs_links_seed:
        print(
            "No [specs] sync_from / links_seed in .livespec.toml — nothing to do.",
            file=sys.stderr,
        )
        return 1
    st = get_state(str(workspace))
    result = sync_specs_from_config(st)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
