"""Clone clusters — read one copy of a pasted function instead of all of them.

`search_similar` (tools/analysis.py) asks "does THIS snippet already exist?"
before a write. `find_clone_clusters` asks the reading-side question about a
whole repo: which bodies exist N times, which copy to read, and what the other
copies change. Domain logic lives in ``domain/clones.py``; this module owns the
path rules (what is a test, what is build output) and the payload contract.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain import clones as _clones
from livespec_mcp.domain import debt_baseline as _debt
from livespec_mcp.domain.duplication import NEAR_DUPLICATE_THRESHOLD
from livespec_mcp.state import get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import (
    _is_bundler_output_path,
    _is_test_scaffold_path,
    group_fields,
)
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

# Directories and suffixes whose contents nobody wrote by hand in this repo.
# A vendored library pasted twice, or two generated protobuf modules, are
# "clones" nobody is going to read one of instead of the other.
_VENDORED_SEGMENTS = frozenset(
    {"vendor", "vendored", "_vendor", "third_party", "thirdparty", "external", "site-packages"}
)
_GENERATED_SUFFIXES = (
    "_pb2.py",
    "_pb2_grpc.py",
    ".pb.go",
    ".pb.ts",
    ".g.dart",
    ".generated.ts",
    ".generated.go",
    ".generated.cs",
    ".gen.go",
    ".gen.ts",
)


def is_generated_or_vendored_path(path: str) -> bool:
    """Build output (same rule as ``find_dead_code``), vendored or generated."""
    fp = path.replace("\\", "/").lstrip("/")
    if _is_bundler_output_path(fp):
        return True
    if not _VENDORED_SEGMENTS.isdisjoint(fp.split("/")[:-1]):
        return True
    return fp.endswith(_GENERATED_SUFFIXES)


def _workspace_note(fn):
    fn.__doc__ = (fn.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE
    return fn


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def find_clone_clusters(
        min_size: int = 2,
        threshold: float = NEAR_DUPLICATE_THRESHOLD,
        path: str | None = None,
        include_tests: bool = False,
        limit: int = 50,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Functions pasted N times — read ONE, skim how the others differ.

        Call it on first contact with a legacy repo, before reading any file
        that hotspots or search point at more than once. Each cluster names a
        `representative` to read (`read_unit` / `get_symbol_source`) and, for
        every other copy, only what differs from it:

        - `identical` — same structure. `renamed` lists the identifier/literal
          substitutions (`[in representative, in this copy]`) when the text is
          not the same.
        - `similar` — winnowed-fingerprint overlap >= `threshold`, with
          added/removed/changed line counts and a short diff excerpt
          (`-` representative, `+` this copy; hunk headers are file lines).
          `same_as` points at an earlier copy with the same shape instead of
          repeating its diff.

        No copy's body is ever returned, and excerpts are capped per copy and
        per cluster (`diff_truncated`, `others_omitted` say when).

        The representative is the production copy with the most callers, then
        the shallowest/shortest path, then lexical order. Clusters are ordered
        by `lines_saved` (lines of every copy except the representative).
        `totals` are exact whatever the page: `clusters`, `members`,
        `duplicated_lines`, `lines_saved_if_read_once`.

        Excluded by default, counted in `filtered_out`: bodies under the same
        40-token floor `search_similar` uses (every getter looks like every
        other), build/vendored/generated paths, and test files
        (`include_tests=True` keeps them). Functions and methods only.

        `path` keeps the clusters with at least one copy under that file or
        directory — the other copies may live anywhere. Clusters are single
        linkage: a copy can be below `threshold` against the representative
        when it joined through an intermediate one; its own `similarity` shows
        it. The debt baseline never hides a cluster here; with one captured,
        `frozen_members` says how many copies it had already accepted.
        """
        if min_size < 2:
            return mcp_error("min_size must be >= 2", hint="a cluster is at least two copies")
        if not 0.0 < threshold <= 1.0:
            return mcp_error("threshold must be in (0, 1]", hint="0.80 is the default")
        if limit < 1 or limit > 500:
            return mcp_error("limit must be between 1 and 500", hint="50 is the default")
        if cursor < 0:
            return mcp_error("cursor must be >= 0", hint="pass a prior next_cursor")

        st = get_state(workspace)
        report = _clones.find_clusters(
            st.conn,
            tuple(st.group_project_ids()),
            st.settings.workspace,
            min_size=min_size,
            threshold=threshold,
            include_tests=include_tests,
            is_test=_is_test_scaffold_path,
            is_excluded=is_generated_or_vendored_path,
            path=path,
        )

        total = len(report.clusters)
        out: dict[str, Any] = {
            "totals": report.totals,
            "scanned": report.scanned,
            "filtered_out": report.filtered_out,
            "threshold": threshold,
            "min_size": min_size,
        }
        if path:
            out["path"] = path
        if report.nested_clusters_dropped:
            out["nested_clusters_dropped"] = report.nested_clusters_dropped
        if summary_only:
            out["next_cursor"] = None
            return {**out, **group_fields(st)}

        page = report.clusters[cursor : cursor + limit]
        rendered = _clones.render_page(page, st.settings.workspace)

        if _debt.has_baseline(st.conn):
            frozen = {
                (r[0], r[1])
                for r in st.conn.execute(
                    "SELECT structural_hash, qualified_name FROM debt_baseline"
                )
            }
            for cluster, payload in zip(page, rendered, strict=True):
                payload["frozen_members"] = sum(
                    (m.fingerprint.structural_hash, m.qualified_name) in frozen
                    for m in cluster.members
                )

        out["clusters"] = rendered
        out["next_cursor"] = cursor + limit if cursor + limit < total else None
        if rendered:
            out["next"] = (
                "read_unit(qname=<representative.qualified_name>) — then skim "
                "`others`; do not open the copies"
            )
        return {**out, **group_fields(st)}
