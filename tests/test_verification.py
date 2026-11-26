"""Spec requirements verified on the code that implements them, and stale once that code changes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from livespec_mcp import verification
from livespec_mcp.cli import main as cli_main
from livespec_mcp.state import get_state


def _indexed(workspace: Path, body: str = "return round(x, 2)") -> None:
    (workspace / "openspec").mkdir(exist_ok=True)
    (workspace / "totals.py").write_text(f"def total(x):\n    {body}\n")
    assert cli_main(["index", str(workspace)]) == 0
    st = get_state(str(workspace))
    sym = st.conn.execute("SELECT id FROM symbol WHERE name = 'total'").fetchone()[0]
    spec = st.conn.execute("SELECT id FROM spec WHERE spec_id = 'RF-1'").fetchone()
    if spec is None:
        st.conn.execute(
            "INSERT INTO spec (project_id, spec_id, title, priority, status) VALUES (?, 'RF-1', "
            "'Totals round half up', 'critical', 'active')",
            (st.project_id,),
        )
        spec = st.conn.execute("SELECT id FROM spec WHERE spec_id = 'RF-1'").fetchone()
    st.conn.execute(
        "INSERT OR IGNORE INTO spec_symbol (spec_id, symbol_id) VALUES (?, ?)", (spec[0], sym)
    )
    st.conn.commit()


def test_a_requirement_is_stale_once_the_code_it_was_checked_on_changes(workspace: Path, capsys):
    _indexed(workspace)
    capsys.readouterr()
    assert cli_main(["stale", str(workspace)]) == 0  # never verified, not strict
    assert cli_main(["stale", str(workspace), "--strict"]) == 1
    assert '"reason": "never"' in capsys.readouterr().out
    assert (
        cli_main(
            ["verify", str(workspace), "--spec", "RF-1", "--by", "verifier", "--commit", "abc"]
        )
        == 0
    )
    record = json.loads((workspace / "openspec" / "verifications.json").read_text())
    assert record["verified"]["RF-1"]["totals.total"]["commit"] == "abc"
    capsys.readouterr()
    assert cli_main(["stale", str(workspace), "--strict"]) == 0
    assert cli_main(["stale", str(workspace), "--require-test", "critical"]) == 1
    assert '"reason": "untested"' in capsys.readouterr().out
    _indexed(workspace, "return int(x)")
    capsys.readouterr()
    assert cli_main(["stale", str(workspace)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["stale"][0]["reason"] == "changed" and out["stale"][0]["verified_at"] == "abc"
    assert (
        cli_main(["verify", str(workspace), "--spec", "RF-1", "--by", "test", "--commit", "def"])
        == 0
    )
    assert cli_main(["stale", str(workspace), "--require-test", "critical"]) == 0


def test_a_verification_says_what_it_cannot_record(workspace: Path, capsys):
    _indexed(workspace)
    assert (
        cli_main(["verify", str(workspace), "--spec", "NOPE", "--by", "test", "--commit", "abc"])
        == 1
    )
    assert "no code implements NOPE" in capsys.readouterr().err
    with pytest.raises(ValueError, match="`by`"):
        verification.record(workspace, [], ["RF-1"], by="vibes", commit="abc")
    with pytest.raises(ValueError, match="names the commit"):
        verification.record(workspace, [], ["RF-1"], by="test", commit="")
    (workspace / "openspec" / "verifications.json").write_text('{"format": 9}')
    assert cli_main(["stale", str(workspace)]) == 1
    assert "not a livespec verification record" in capsys.readouterr().err


def test_without_openspec_the_record_lives_under_livespec(tmp_path: Path):
    assert verification.ledger_path(tmp_path) == tmp_path / ".livespec" / "verifications.json"
