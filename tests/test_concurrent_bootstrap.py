"""Two livespec processes opening one index at once.

A session hook starting `livespec index` while a task started another (or two
MCP servers on one shared index) raced in `connect()`: switching to WAL and
creating the schema could be refused at once with `database is locked`, and
both could run the same migration. The bootstrap is now serialized.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path

from livespec_mcp.cli import main as cli_main
from livespec_mcp.storage import db as db_mod
from livespec_mcp.storage.db import connect


def _open(path: str) -> str:
    try:
        connect(Path(path)).close()
        return "ok"
    except Exception as exc:  # the result is what the test reads
        return repr(exc)


def test_many_processes_open_a_fresh_index_at_once(tmp_path):
    results = []
    ctx = mp.get_context("spawn")
    for round_ in range(4):
        path = tmp_path / str(round_) / "docs.db"
        path.parent.mkdir()
        with ctx.Pool(6) as pool:
            results += pool.map(_open, [str(path)] * 6)
    assert set(results) == {"ok"}


def test_a_locked_bootstrap_is_retried(tmp_path, monkeypatch):
    real = db_mod._run_migrations
    calls = []

    def flaky(conn):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(conn)

    monkeypatch.setattr(db_mod, "_run_migrations", flaky)
    monkeypatch.setattr(db_mod, "SETUP_PAUSE_S", 0)
    connect(tmp_path / "docs.db").close()
    assert len(calls) == 2

    def broken(conn):
        raise sqlite3.OperationalError("no such table: x")

    monkeypatch.setattr(db_mod, "_run_migrations", broken)
    try:
        connect(tmp_path / "other.db")
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)  # anything but "locked" is not retried
    else:
        raise AssertionError("expected the error to surface")


def test_a_busy_index_is_never_called_broken(tmp_path, monkeypatch, capsys):
    from livespec_mcp import cli

    def busy(path, force=False):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "_cmd_index", busy)
    assert cli_main(["index", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "busy" in err and "do not delete" in err and "Delete <repo>" not in err
