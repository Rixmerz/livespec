"""A tool an agent is never told about is a tool that does not exist.

`test_tool_count_sync.py` already checks that the docs say the right *number*.
That number was right while six core tools went completely unmentioned in the
Skill an agent actually loads: `read_unit`, `resolve_location`,
`search_similar`, `debt_baseline_capture`, `debt_baseline_status` and
`ingest_external_graph`. Two of them are the ones the last two releases were
built around.

Counting is not coverage. The failure mode is silent in the worst way: the tool
ships, it is registered, `tools/list` returns it, CI is green, and no agent ever
calls it because nothing in its operating manual says when to. That is a
feature built and not delivered.

So the two documents an agent is actually given — the preloaded Skill and the
`agent_playbook` MCP prompt shipped in the wheel — must name every core tool.
The Quickstart and the subagent definition are deliberately NOT held to this:
the Quickstart is a tour, and the subagent defers to the Skill by design ("the
full livespec Skill is preloaded into your context").
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastmcp import Client

REPO = Path(__file__).resolve().parents[1]

#: The documents an agent is handed. The Skill is preloaded by the plugin; the
#: playbook is served as an MCP prompt and ships inside the wheel.
AGENT_MANUALS = (
    "plugin/skills/livespec/SKILL.md",
    "src/livespec_mcp/templates/AGENT_PLAYBOOK.md",
)


async def _core_tools() -> set[str]:
    """Exactly what a default install shows in `tools/list`."""
    prev = os.environ.get("LIVESPEC_PLUGINS")
    os.environ.pop("LIVESPEC_PLUGINS", None)
    try:
        from livespec_mcp.server import mcp

        async with Client(mcp) as client:
            return {t.name for t in await client.list_tools()}
    finally:
        if prev is not None:
            os.environ["LIVESPEC_PLUGINS"] = prev


@pytest.mark.asyncio
@pytest.mark.parametrize("doc", AGENT_MANUALS)
async def test_every_core_tool_is_named_in_the_agent_manual(doc: str):
    text = (REPO / doc).read_text(encoding="utf-8")
    missing = sorted(
        name for name in await _core_tools() if not re.search(rf"\b{re.escape(name)}\b", text)
    )

    assert missing == [], (
        f"{doc} never mentions {missing}. A core tool an agent is not told "
        "about is a tool that does not exist — say when to call it, or move it "
        "out of the core surface."
    )


def test_the_playbook_ships_identically_in_the_wheel_and_the_docs():
    """Two copies exist because the wheel cannot reach `docs/`. They are the
    same document, and a fix applied to one of them only is worse than one
    copy: the agent reads the wheel's, the human reads the repo's, and they
    quietly disagree about how the tool behaves."""
    shipped = (REPO / "src/livespec_mcp/templates/AGENT_PLAYBOOK.md").read_text(encoding="utf-8")
    documented = (REPO / "docs/AGENT_PLAYBOOK.md").read_text(encoding="utf-8")

    assert shipped == documented, (
        "docs/AGENT_PLAYBOOK.md and the copy shipped in the wheel have drifted "
        "— copy one over the other"
    )


@pytest.mark.asyncio
async def test_the_skill_covers_the_external_graph_workflow():
    """The specific gap this file was written for. The Graphify integration was
    two releases of work with zero mentions anywhere an agent would look."""
    text = (REPO / "plugin/skills/livespec/SKILL.md").read_text(encoding="utf-8")

    for needle in (
        "corroborate_with",
        "ingest_external_graph",
        "via_external_edge",
        "excluded_by_edge_type",
        "remove=True",
    ):
        assert needle in text, f"the Skill never mentions {needle}"


@pytest.mark.asyncio
async def test_the_manuals_warn_about_unparsed_languages():
    """`languages_failed` changes what every other number means, so an agent
    that does not know the field exists will report counts that silently
    exclude whole languages."""
    for doc in AGENT_MANUALS:
        text = (REPO / doc).read_text(encoding="utf-8")
        assert "languages_failed" in text, f"{doc} never mentions languages_failed"
