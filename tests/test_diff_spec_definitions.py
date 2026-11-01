"""A range that edits a spec's own definition touches that spec.

The repository's "Livespec — Spec impact" PR comment said "No tracked specs
are touched by these changes." on a PR that added scenarios to
`openspec/specs/indexer/spec.md` and `openspec/specs/analysis/spec.md`, and
again on a PR that changed core indexer code. Two gaps:

1. Only code was traced to specs (changed symbols → caller cone →
   `spec_symbol`); editing a requirement in the canonical OpenSpec tree was
   no touch at all. `spec_definition_edits` now reads it from git, and
   `git_diff_impact` / `compute_diff_spec_impact` report it apart
   (`spec_definitions_changed`, `via: ["definition"]`).
2. The CI script indexed without importing the repo's `openspec/` tree, so
   the index held no spec and no annotation link: nothing could ever be
   "touched". It now imports specs as `index_project` would, falling back to
   `openspec/`, and the comment says when the index holds none.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.git_range import diff_name_status
from livespec_mcp.domain.tracks import spec_definition_edits
from livespec_mcp.server import mcp
from livespec_mcp.state import get_state
from livespec_mcp.tools.analysis import compute_diff_spec_impact

_SPEC_V1 = """# Indexer Specification

## Purpose

Indexing.

## Requirements

### Requirement: Scoped resolution

The system SHALL resolve calls.

#### Scenario: Keeps edges

- **WHEN** a callee changes
- **THEN** edges remain

### Requirement: Fast walk

The system SHALL walk quickly.

#### Scenario: Walks

- **WHEN** indexing
- **THEN** it is fast
"""

_SPEC_V2 = _SPEC_V1.replace(
    "- **THEN** edges remain\n",
    "- **THEN** edges remain\n\n#### Scenario: Literal receivers\n\n"
    '- **WHEN** `"".join(x)` is called\n- **THEN** no edge is made\n',
).replace(
    "### Requirement: Fast walk\n\nThe system SHALL walk quickly.\n\n"
    "#### Scenario: Walks\n\n- **WHEN** indexing\n- **THEN** it is fast\n",
    "### Requirement: Language families\n\nThe system SHALL keep calls in one "
    "language.\n\n#### Scenario: TS to Python\n\n- **WHEN** a TS call\n"
    "- **THEN** no Python edge\n",
)


def _git(ws: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(ws: Path) -> None:
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "pkg").mkdir()
    (ws / "pkg" / "__init__.py").write_text("")
    (ws / "pkg" / "core.py").write_text(
        'def resolve():\n    """@spec:indexer-scoped-resolution"""\n    return 1\n'
    )
    spec = ws / "openspec" / "specs" / "indexer" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(_SPEC_V1)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "base")
    spec.write_text(_SPEC_V2)
    _git(ws, "commit", "-qam", "edit the spec only")


def test_spec_definition_edits_reads_requirements_from_git(workspace: Path):
    _repo(workspace)
    entries, problem = diff_name_status(str(workspace), "HEAD~1", "HEAD")
    assert problem is None
    edits = {
        e.spec_id: e.change
        for e in spec_definition_edits(str(workspace), "HEAD~1", "HEAD", entries or [])
    }
    assert edits == {
        "indexer-scoped-resolution": "modified",  # a scenario added
        "indexer-language-families": "added",
        "indexer-fast-walk": "removed",
    }


@pytest.mark.asyncio
async def test_git_diff_impact_reports_changed_spec_definitions(workspace: Path):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("git_diff_impact", {"base_ref": "HEAD~1"})).data
    changed = {r["spec_id"]: r for r in out["spec_definitions_changed"]}
    assert set(changed) == {
        "indexer-scoped-resolution",
        "indexer-language-families",
        "indexer-fast-walk",
    }
    assert changed["indexer-scoped-resolution"]["path"] == "openspec/specs/indexer/spec.md"
    assert out["counts"]["spec_definitions_changed"] == 3
    # Only a spec file changed: no code reaches a spec.
    assert out["affected_specs"] == []


@pytest.mark.asyncio
async def test_diff_spec_impact_counts_definitions_even_without_imported_specs(
    workspace: Path,
):
    _repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
    st = get_state(str(workspace))
    result = compute_diff_spec_impact(st, "HEAD~1", "HEAD")
    assert result["specs_indexed"] == 0
    rows = {r["spec_id"]: r for r in result["specs_touched"]}
    assert rows["indexer-scoped-resolution"]["via"] == ["definition"]
    assert rows["indexer-scoped-resolution"]["definition_change"] == "modified"
    assert rows["indexer-fast-walk"]["definition_change"] == "removed"


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pr_diff_impact.py"
    spec = importlib.util.spec_from_file_location("pr_diff_impact", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_pr_script_imports_openspec_and_traces_code(workspace: Path):
    """The CI path: index, import `openspec/` (no `.livespec.toml`), then a
    code change is traced to its annotated spec and a spec edit is a
    definition change."""
    _repo(workspace)
    (workspace / "pkg" / "core.py").write_text(
        'def resolve():\n    """@spec:indexer-scoped-resolution"""\n    return 2\n'
    )
    _git(workspace, "commit", "-qam", "change code")
    script = _load_script()
    from livespec_mcp.tools.indexing import run_index_pipeline

    st = get_state(str(workspace), create=True)
    run_index_pipeline(st, force=False)
    imported = script.import_specs(st)
    assert imported is not None and "openspec" in imported
    result = compute_diff_spec_impact(st, "HEAD~2", "HEAD")
    assert result["specs_indexed"] >= 2
    row = next(r for r in result["specs_touched"] if r["spec_id"] == "indexer-scoped-resolution")
    assert row["via"] == ["definition", "code"]
    assert row["files"] == ["pkg/core.py"]

    body = script.render_markdown(
        base="a",
        head="b",
        files_changed=result["files_changed"],
        specs_touched=result["specs_touched"],
        workspace=workspace,
        specs_indexed=result["specs_indexed"],
    )
    assert "definition modified" in body
    assert "No tracked specs" not in body


def test_comment_says_when_the_index_holds_no_specs(tmp_path: Path):
    script = _load_script()
    body = script.render_markdown(
        base="a",
        head="b",
        files_changed=["src/x.py"],
        specs_touched=[],
        workspace=tmp_path,
        specs_indexed=0,
    )
    assert "holds **no specs**" in body
    body = script.render_markdown(
        base="a",
        head="b",
        files_changed=["src/x.py"],
        specs_touched=[],
        workspace=tmp_path,
        specs_indexed=5,
    )
    assert "no changed code reaches a spec-linked symbol" in body
