"""Spec imports link the `@spec:` annotations indexed before the spec existed.

The order an agent actually runs on a fresh repo is index_project, then
sync_openspec, then a query. The annotation pass used to run only inside an
index run that changed files, so specs imported after the index stayed at zero
links until a forced re-index.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.matcher import parse_annotations
from livespec_mcp.domain.specs_sync import _parse_openspec_tree
from livespec_mcp.server import mcp
from livespec_mcp.state import reset_state

REPO = Path(__file__).resolve().parents[1]

SPEC_MD = """# Billing Specification

## Purpose
Charge customers.

## Requirements

### Requirement: Charge card
The system SHALL charge the card on file.

#### Scenario: Card on file
- **WHEN** a customer checks out
- **THEN** the card is charged
"""


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "openspec" / "specs" / "billing").mkdir(parents=True)
    (tmp_path / "openspec" / "specs" / "billing" / "spec.md").write_text(SPEC_MD)
    (tmp_path / "billing.py").write_text(
        'def charge(card):\n    """@spec:billing-charge-card\n\n    Charge it."""\n'
        "    return card\n"
    )
    return tmp_path


async def _linked(c: Client, ws: Path) -> list[str]:
    out = (
        await c.call_tool(
            "get_spec_implementation", {"workspace": str(ws), "spec_id": "billing-charge-card"}
        )
    ).data
    return [s["qualified_name"] for s in out["symbols"]]


@pytest.mark.asyncio
async def test_sync_openspec_after_index_links_annotations(tmp_path: Path):
    reset_state()
    ws = _workspace(tmp_path)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(ws)})
        synced = (await c.call_tool("sync_openspec", {"workspace": str(ws)})).data
        assert synced["specs"]["created"] == 1
        assert synced["specs"]["spec_links_created"] == 1
        assert await _linked(c, ws) == ["billing.charge"]


@pytest.mark.asyncio
async def test_import_specs_from_markdown_after_index_links_annotations(tmp_path: Path):
    reset_state()
    ws = _workspace(tmp_path)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(ws)})
        await c.call_tool(
            "import_specs_from_markdown",
            {"workspace": str(ws), "path": "openspec/specs"},
        )
        assert await _linked(c, ws) == ["billing.charge"]


@pytest.mark.asyncio
async def test_first_index_with_openspec_dir_config_links_annotations(tmp_path: Path):
    # [specs].openspec_dir imports the specs after the index pipeline's own
    # annotation pass, so a single first index_project must still link them.
    reset_state()
    ws = _workspace(tmp_path)
    (ws / ".livespec.toml").write_text('[specs]\nopenspec_dir = "openspec"\n')
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(ws)})
        assert await _linked(c, ws) == ["billing.charge"]


def test_repo_spec_annotations_are_on_lines_the_parser_reads():
    # The parser reads `@spec:` only at the start of a line; one written
    # mid-sentence links nothing and reports nothing, so it silently drops out
    # of get_spec_implementation.
    known = {p.spec_id for p in _parse_openspec_tree(REPO / "openspec" / "specs")}
    unread = []
    for path in sorted((REPO / "src").rglob("*.py")):
        for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A docstring's quotes are not part of the text scanned.
            line = re.sub(r"^(\s*)[rRbBuU]?(\"\"\"|''')", r"\1", raw)
            line = re.sub(r"(\"\"\"|''')\s*$", "", line)
            for m in re.finditer(r"@spec:([A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])", line):
                if m.group(1) not in known:
                    continue
                hits = {h.spec_id for h in parse_annotations(line, known_ids=known)}
                if m.group(1) not in hits:
                    unread.append(f"{path.relative_to(REPO)}:{n}: {m.group(1)}")
    assert unread == []
