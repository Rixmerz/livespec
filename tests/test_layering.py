"""`tools/` is a thin surface over `domain/`, and the layering has to hold.

The rule the codebase already follows: business logic lives in `domain/` with
no MCP coupling, and `tools/` is the thin MCP surface over it. Nothing enforced
it, and the newest feature is exactly where it broke — `tools/indexing.py`
imported the external-graph helpers back out of `tools/analysis.py`, because
that is where the first consumer happened to put them.

That import is not a style complaint. It put shared logic behind whichever tool
module needed it first, and by the time a third consumer arrived
(`propose_specs_from_codebase`) the cheaper move was a private copy: forty
lines re-deriving the path, re-writing both error messages and re-applying the
overlap threshold by hand. A fix to any of the three reached two call sites out
of three.

These tests are cheap and would have caught it on the commit that introduced it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "livespec_mcp"
DOMAIN = SRC / "domain"
TOOLS = SRC / "tools"


def _imported_modules(path: Path) -> set[str]:
    """Every `livespec_mcp.*` module this file imports, at any nesting level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("livespec_mcp"):
                out.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("livespec_mcp"):
                    out.add(alias.name)
    return out


@pytest.mark.parametrize(
    "path", sorted(DOMAIN.glob("*.py")), ids=lambda p: p.name
)
def test_domain_never_imports_the_mcp_surface(path: Path):
    """Business logic must be callable without an MCP host in the room.

    This is what lets `domain/` be tested directly, reused by the CLI, and
    moved behind a different transport later. A single `from
    livespec_mcp.tools...` here would make the layering decorative.
    """
    offenders = {m for m in _imported_modules(path) if m.startswith("livespec_mcp.tools")}
    assert offenders == set(), f"{path.name} imports the tools layer: {sorted(offenders)}"


def test_the_external_graph_helpers_live_in_domain():
    """The specific regression. Three tool modules consume an external graph;
    all three must reach the same implementation in `domain/`, not each
    other."""
    from livespec_mcp.domain import external_source

    for name in (
        "resolve_external_graph_source",
        "load_gated_external_graph",
        "unknown_relations_report",
    ):
        assert hasattr(external_source, name), name

    assert "livespec_mcp.tools.analysis" not in _imported_modules(
        TOOLS / "indexing.py"
    ), "indexing.py is importing helpers back out of a sibling tool module"


def test_every_external_graph_consumer_goes_through_the_shared_gate():
    """A consumer that calls `load_external_graph` directly has skipped the
    overlap check, and a graph describing another repo then matches nothing —
    which reads as a clean bill of health rather than as an error.

    `domain/external_graph.py` defines it and `domain/external_source.py` is
    the one place allowed to call it.
    """
    allowed = {"external_source.py", "external_graph.py"}
    offenders = []
    for path in sorted(TOOLS.rglob("*.py")) + sorted(DOMAIN.glob("*.py")):
        if path.name in allowed:
            continue
        if "load_external_graph" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)

    assert offenders == [], (
        f"these bypass the overlap gate: {offenders} — call "
        "domain.external_source.load_gated_external_graph instead"
    )
