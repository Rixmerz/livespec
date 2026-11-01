"""The CLI subcommands that do not serve MCP never import the server.

`livespec index` / `status` / `grammars` / `--version` used to import
`livespec_mcp.server`, which built the FastMCP app with every tool module on
import: 1.2 s before the command did anything (`livespec --version` took
1.5 s). The app now lives in `livespec_mcp.app` and `server.mcp` is built on
first access; the console entry point stays `livespec_mcp.server:main`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PROBE = """
import json, sys
from livespec_mcp import server
try:
    server.main()
except SystemExit:
    pass
heavy = sorted(m for m in ("fastmcp", "livespec_mcp.app", "livespec_mcp.tools.analysis")
               if m in sys.modules)
print("HEAVY=" + json.dumps(heavy), file=sys.stderr)
"""


def _heavy_modules(args: list[str], env_home: Path) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, *args],
        capture_output=True,
        text=True,
        env={"LIVESPEC_HOME": str(env_home), "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    line = [ln for ln in proc.stderr.splitlines() if ln.startswith("HEAVY=")]
    assert line, proc.stderr
    return json.loads(line[-1][len("HEAVY=") :])


def test_version_index_and_status_do_not_import_the_server(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    home = tmp_path / "home"
    assert _heavy_modules(["--version"], home) == []
    assert _heavy_modules(["index", str(repo)], home) == []
    assert _heavy_modules(["status", str(repo)], home) == []


def test_server_mcp_is_built_on_first_access():
    from livespec_mcp import server

    app = server.mcp
    from livespec_mcp.app import mcp

    assert app is mcp
    assert server.mcp is mcp
