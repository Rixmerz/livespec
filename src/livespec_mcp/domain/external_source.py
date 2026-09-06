"""Choosing and vetting the external graph, without an MCP dependency.

These three steps — pick which `graph.json` to use, load it, refuse it if it
describes a different tree — are shared by every consumer of an external
extractor: dead-code corroboration, orphan-test corroboration, community
grouping for Spec proposals, and ingestion.

They used to live in `tools/analysis.py`, which meant `tools/indexing.py`
imported them back out of a sibling tool module. That import is the reason this
file exists: a tools -> tools dependency puts the shared logic behind whichever
module happened to need it first, and the layering the rest of the codebase
keeps (`tools/` is a thin surface over `domain/`) stopped being true exactly
where the newest feature landed.

The one thing that kept this in `tools/` was `mcp_error`. So the failure is
returned as *data* — a `GraphProblem` with a message and a hint — and the thin
wrappers in `tools/` turn it into the shaped error the contract requires. The
domain does not know what an MCP error looks like, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Where Graphify writes by default. Used only to *tell* the caller a graph is
#: sitting there — never to silently consume it. An index that quietly changed
#: its answers because a file appeared on disk would be worse than one that
#: needs asking.
DEFAULT_EXTERNAL_GRAPH = "graphify-out/graph.json"

#: Below this share of shared files, the graph is describing something else.
MIN_FILE_OVERLAP = 0.1


@dataclass(frozen=True)
class GraphProblem:
    """A reason the external graph cannot be used, in the shape a tool needs.

    Deliberately not an `mcp_error`: this module is domain code and must not
    know the wire shape. The caller in `tools/` maps it.
    """

    message: str
    hint: str | None = None


def resolve_external_graph_source(
    workspace: Path, explicit: str | None
) -> tuple[str | None, str | None]:
    """Pick the graph to use, and a hint when one is merely available.

    Returns ``(path_or_None, hint_or_None)``. Precedence: the explicit
    argument, then ``[graph] external`` in ``.livespec.toml``. A graph sitting
    at Graphify's default output path is reported as a hint only — consuming it
    changes what the tool reports, so it stays opt-in.
    """
    if explicit:
        return explicit, None

    from livespec_mcp.config import load_repo_config

    configured = load_repo_config(workspace).external_graph
    if configured:
        return configured, None

    default_path = workspace / DEFAULT_EXTERNAL_GRAPH
    if default_path.is_file():
        return None, (
            f"An external code graph is available at {DEFAULT_EXTERNAL_GRAPH}. "
            "Pass corroborate_with to drop candidates a second extractor still "
            "sees referenced, or set `[graph] external = "
            f'"{DEFAULT_EXTERNAL_GRAPH}"` in .livespec.toml to use it by '
            "default."
        )
    return None, None


def load_gated_external_graph(
    workspace: Path,
    graph_path: str,
    indexed_files: set[str],
    *,
    keep_link_meta: bool = False,
) -> tuple[Any, float, GraphProblem | None]:
    """Load a graph and refuse it unless it describes this tree.

    Returns ``(graph, overlap, None)`` or ``(None, 0.0, GraphProblem)``.

    The overlap guard is the important half: a graph whose paths don't line up
    matches nothing, and "nothing matched" would otherwise be reported as
    "nothing to drop", which reads as a clean bill of health for candidates
    nobody actually checked.

    The overlap is RETURNED rather than stored on the graph. Parsed graphs are
    cached and shared across calls and workspaces, and overlap is a fact about
    (this graph, one index) — writing it onto the shared object let one
    project's sanity gate report another project's number.
    """
    from livespec_mcp.domain.external_graph import load_external_graph, overlap_ratio

    resolved = Path(graph_path)
    if not resolved.is_absolute():
        resolved = workspace / resolved

    try:
        graph = load_external_graph(resolved, keep_link_meta=keep_link_meta)
    except FileNotFoundError:
        return (
            None,
            0.0,
            GraphProblem(
                f"External graph not found: {resolved}",
                "Generate one with `/graphify <repo>` (writes "
                "graphify-out/graph.json), or pass an absolute path.",
            ),
        )
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        return (
            None,
            0.0,
            GraphProblem(
                f"Could not read external graph {resolved}: {exc}",
                "Expected Graphify's NetworkX node-link graph.json.",
            ),
        )

    overlap = overlap_ratio(graph, indexed_files)
    if overlap < MIN_FILE_OVERLAP:
        return (
            None,
            0.0,
            GraphProblem(
                f"External graph {resolved} shares almost no files with this "
                f"index ({overlap:.0%} of its files are indexed here).",
                "It probably describes a different repo, or was built from a "
                "different root so its paths do not line up. Corroborating "
                "against it would vouch for nothing.",
            ),
        )
    return graph, overlap, None


def unknown_relations_report(graph: Any) -> dict[str, Any]:
    """Name the relations this graph carries that livespec has no rule for.

    Both consumers of an external graph fail *quietly* when the other tool
    grows vocabulary: corroboration ignores the relation, so a real reference
    stops rescuing a candidate, and ingestion skips it, so an edge livespec
    lacks never arrives. Neither is an error. The two most recent additions to
    that vocabulary were found by reading Graphify's source during an audit,
    which is not a maintenance strategy — this is.
    """
    unknown = getattr(graph, "unknown_relations", None)
    if not unknown:
        return {}
    return {
        "unknown_relations": dict(sorted(unknown.items())),
        "unknown_relations_hint": (
            "This graph uses relations livespec classifies as neither "
            "structural nor evidence, so they were ignored. If any of them "
            "means one symbol depends on another, livespec is under-counting; "
            "please report them."
        ),
    }
