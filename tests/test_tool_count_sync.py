"""The tool counts the docs advertise must match the tools actually registered.

Every doc that orients a human or an agent leads with a count: the README
headline, the tier breakdown in CLAUDE.md, the beta checklist's
"tool surface truth" row, the QUICKSTART's "the host cached N core tools only"
troubleshooting line. An agent reads one of those and calibrates — "I see 28,
so nothing is missing" — which only works while the number is true.

Nothing enforced it. `get_cross_repo_guide` landed in 0.31.3 as a 28th core
tool and every one of those docs kept saying 27 core / 44 total, including a
checklist item literally titled "everywhere that counts tools". The count is
the kind of fact that drifts silently: adding a tool is the moment you are
least likely to go re-read four markdown files.

So the counts are asserted against the live registry, not trusted. These tests
fail on the commit that adds or removes a tool, which is exactly when the docs
are cheap to fix.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastmcp import Client

REPO = Path(__file__).resolve().parents[1]

# The two plugin tiers are gated out of `tools/list` by default; their sizes are
# fixed by the tier split, so the doc claim is checked against these.
SPEC_PLUGIN_TOOLS = 12
DOCS_PLUGIN_TOOLS = 5


async def _registered(plugins: str | None) -> set[str]:
    """Tool names the server lists, with plugin gating set by `plugins`."""
    prev = os.environ.get("LIVESPEC_PLUGINS")
    if plugins is None:
        os.environ.pop("LIVESPEC_PLUGINS", None)
    else:
        os.environ["LIVESPEC_PLUGINS"] = plugins
    try:
        # Imported inside so the env var is set before the server module reads
        # it at registration time.
        from livespec_mcp.server import mcp

        async with Client(mcp) as client:
            return {t.name for t in await client.list_tools()}
    finally:
        if prev is None:
            os.environ.pop("LIVESPEC_PLUGINS", None)
        else:
            os.environ["LIVESPEC_PLUGINS"] = prev


@pytest.mark.asyncio
async def test_core_and_total_counts_are_internally_consistent():
    """core + spec plugin + docs plugin == everything registered."""
    core = await _registered(None)
    everything = await _registered("all")

    assert len(everything) == len(core) + SPEC_PLUGIN_TOOLS + DOCS_PLUGIN_TOOLS, (
        f"{len(core)} core + {SPEC_PLUGIN_TOOLS} spec + {DOCS_PLUGIN_TOOLS} docs "
        f"!= {len(everything)} registered — the tier constants in this test are "
        f"stale, or a tool landed outside a tier"
    )
    assert core < everything, "gated plugin tools must be a superset of core"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "CLAUDE.md",
        "docs/BETA_CHECKLIST.md",
        "docs/AGENT_QUICKSTART.md",
    ],
)
async def test_docs_do_not_advertise_a_stale_core_count(doc: str):
    """Any "<N> core" claim in a live doc must be the real core count.

    Historical rows (changelog-style tables recording what a past release
    shipped) are exempt: they are records, not claims about today.
    """
    core_n = len(await _registered(None))
    text = (REPO / doc).read_text(encoding="utf-8")

    stale: list[str] = []
    for line in text.splitlines():
        if _is_historical_row(line):
            continue
        for m in re.finditer(r"(\d+)\s+core\b", line):
            if int(m.group(1)) != core_n:
                stale.append(line.strip())
    assert not stale, (
        f"{doc} advertises a core-tool count that is not {core_n}:\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.asyncio
async def test_readme_headline_total_matches_registry():
    """The README headline is the number most readers take away."""
    everything = len(await _registered("all"))
    text = (REPO / "README.md").read_text(encoding="utf-8")

    m = re.search(r"^##\s+Tools\s+\((\d+)\s+total", text, re.MULTILINE)
    assert m, "README has no '## Tools (<N> total: ...)' headline to check"
    assert int(m.group(1)) == everything, (
        f"README headline claims {m.group(1)} tools but {everything} are "
        f"registered — update the headline, the tier breakdown, and the tool list"
    )


@pytest.mark.asyncio
async def test_every_core_tool_is_documented_in_the_readme_list():
    """A tool nobody documented is a tool nobody discovers.

    `get_cross_repo_guide` shipped and stayed absent from the README list for a
    release; an agent reading the docs had no way to learn it existed.
    """
    core = await _registered(None)
    text = (REPO / "README.md").read_text(encoding="utf-8")
    missing = sorted(name for name in core if name not in text)
    assert not missing, f"core tools missing from README: {missing}"


def _is_historical_row(line: str) -> bool:
    """Changelog/roadmap table rows record a past release, not today's surface."""
    stripped = line.lstrip()
    if not stripped.startswith("|"):
        return False
    # Roadmap rows look like: `| 24 — v0.23 | ✅ | ... Tools 36 → 44. ...`
    return bool(re.search(r"v0\.\d+", line)) or "→" in line or "->" in line
