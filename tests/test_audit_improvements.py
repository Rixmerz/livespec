"""Audit-driven product fixes: RF bootstrap, sync, endpoint filters."""

from __future__ import annotations

from pathlib import Path

from livespec_mcp.config import load_repo_config
from livespec_mcp.domain.requirements_sync import (
    scan_duplicate_rf_markdown_specs,
    sync_requirements_from_config,
)
from livespec_mcp.state import get_state
from livespec_mcp.tools.analysis import compute_endpoints, filter_api_endpoints
from livespec_mcp.tools.plugins import CORE_PLUGIN_TOOL_NAMES, RF_MUTATION_TOOL_NAMES


def test_import_requirements_always_visible():
    assert "import_requirements_from_markdown" in CORE_PLUGIN_TOOL_NAMES
    assert "import_requirements_from_markdown" not in RF_MUTATION_TOOL_NAMES


def test_duplicate_rf_spec_warning(tmp_path: Path):
    (tmp_path / "a.md").write_text("## RF-001: One\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "b.md").write_text("## RF-001: Duplicate\n", encoding="utf-8")
    warns = scan_duplicate_rf_markdown_specs(tmp_path)
    assert len(warns) == 1
    assert warns[0]["rf_id"] == "RF-001"


def test_filter_endpoints_excludes_pytest_fixtures(workspace: Path):
    (workspace / "tests").mkdir()
    (workspace / "tests" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef client():\n    return 1\n",
        encoding="utf-8",
    )
    (workspace / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n"
        "@app.get('/ok')\ndef ok():\n    return 1\n",
        encoding="utf-8",
    )
    st = get_state(str(workspace))
    from livespec_mcp.tools.indexing import run_index_pipeline

    run_index_pipeline(st, force=True, embed=False)
    raw = compute_endpoints(st, None)
    filtered = filter_api_endpoints(raw, None)
    handlers = {e["qualified_name"] for e in filtered}
    assert not any("conftest" in h for h in handlers)
    assert any(h.endswith(".ok") for h in handlers)


def test_requirements_sync_from_config(tmp_path: Path):
    spec = tmp_path / "docs" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("## RF-010: Sync test\n**Prioridad:** alta\n", encoding="utf-8")
    (tmp_path / ".livespec.toml").write_text(
        '[requirements]\nsync_from = ["docs/spec.md"]\n',
        encoding="utf-8",
    )
    cfg = load_repo_config(tmp_path)
    assert cfg.requirements_sync_from == ("docs/spec.md",)
    st = get_state(str(tmp_path))
    from livespec_mcp.tools.indexing import run_index_pipeline

    run_index_pipeline(st, force=True, embed=False)
    out = sync_requirements_from_config(st)
    assert out is not None
    assert out["imports"][0]["parsed"] == 1


def test_bulk_link_test_module_hint(workspace: Path):
    from livespec_mcp.domain.requirements_sync import bulk_link_rf_symbols_impl

    st = get_state(str(workspace))
    st.conn.execute(
        "INSERT INTO rf(project_id, rf_id, title) VALUES(?, ?, ?)",
        (st.project_id, "RF-099", "t"),
    )
    st.conn.commit()
    result = bulk_link_rf_symbols_impl(
        st,
        [{"rf_id": "RF-099", "symbol_qname": "tests.pkg.test_mod"}],
    )
    assert result["failed"] == 1
    assert result["results"][0].get("hint")
