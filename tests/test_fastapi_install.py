"""FastAPI init installs Cursor rule, skill, and session prompt."""

from __future__ import annotations

from pathlib import Path

from livespec_mcp.explorer.install import init_fastapi_project


def test_fastapi_init_installs_cursor_assets_and_autowires(tmp_path: Path):
    main = tmp_path / "main.py"
    main.write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n"
        "@app.get('/ping')\ndef ping():\n    return 'pong'\n",
        encoding="utf-8",
    )
    result = init_fastapi_project(tmp_path, index=True, wire_app=True, install_cursor=True)

    assert result.indexed is True
    assert result.explorer_bundle is True
    assert (tmp_path / ".mcp-docs" / "explorer" / "index.html").is_file()
    assert (tmp_path / ".cursor" / "rules" / "livespec-fastapi.mdc").is_file()
    assert (tmp_path / ".cursor" / "skills" / "livespec-fastapi" / "SKILL.md").is_file()
    prompt = tmp_path / ".livespec" / "SESSION_PROMPT.md"
    assert prompt.is_file()
    assert str(tmp_path.resolve()) in prompt.read_text(encoding="utf-8")
    assert result.autowire.get("wired") is True
    assert "mount_explorer(app" in main.read_text(encoding="utf-8")
    assert result.errors == ()


def test_fastapi_init_cli_json(capsys, tmp_path: Path):
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    from livespec_mcp.cli import main

    rc = main(["fastapi", "init", str(tmp_path), "--no-cursor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"explorer_bundle": true' in out or '"explorer_bundle": True' in out
