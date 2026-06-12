"""CLI subcommands (v0.14): `livespec-mcp index <path>` / `status <path>`
print JSON to stdout for cron/systemd/pre-commit use. No-arg invocation
stays the stdio MCP server (not exercised here — it would block)."""

from __future__ import annotations

import json
from pathlib import Path

from livespec_mcp.cli import main as cli_main


def test_index_then_status(workspace: Path, capsys):
    (workspace / "app.py").write_text("def app(): pass\n\ndef helper(): app()\n")

    rc = cli_main(["index", str(workspace)])
    assert rc == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["files_total"] == 1
    assert indexed["symbols_total"] == 2
    assert indexed["workspace"] == str(workspace)

    rc = cli_main(["status", str(workspace)])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["files"] == 1
    assert status["symbols"] == 2
    assert status["last_run"] is not None


def test_index_force_flag(workspace: Path, capsys):
    (workspace / "a.py").write_text("def a(): pass\n")
    assert cli_main(["index", str(workspace)]) == 0
    capsys.readouterr()
    assert cli_main(["index", str(workspace), "--force"]) == 0
    forced = json.loads(capsys.readouterr().out)
    assert forced["files_changed"] == 1  # force re-extracts the unchanged file


def test_missing_workspace_is_clean_error(tmp_path: Path, capsys):
    rc = cli_main(["index", str(tmp_path / "nope")])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err


def test_malformed_repo_config_is_clean_error(workspace: Path, capsys):
    (workspace / ".livespec.toml").write_text("not toml ][")
    (workspace / "a.py").write_text("def a(): pass\n")
    rc = cli_main(["index", str(workspace)])
    assert rc == 1
    assert ".livespec.toml" in capsys.readouterr().err
