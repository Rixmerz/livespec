"""Spec Explorer bundle builder (livespec-docs plugin surface).

Emits a static, self-contained "Spec Explorer" — a Swagger-UI-style bundle
auto-generated from the project's Specs + call graph + endpoints +
coverage audit. Two artifacts land under ``<workspace>/.mcp-docs/explorer/``:

    data.json   machine-readable bundle (schema below)
    index.html  single self-contained viewer (data inlined; opens via file://)

The data layer REUSES the compute logic behind ``find_endpoints`` and
``audit_coverage`` (``compute_endpoints`` / ``compute_coverage`` in
``tools.analysis``) and reads Spec / spec_symbol / spec_dependency directly —
no MCP round-trips, no duplicated SQL beyond the per-Spec symbol join.

data.json schema:
    {
      "meta": {"project", "generated_at"|null, "base_path": "/explorer",
               "counts": {"specs", "symbols", "endpoints", "files"}},
      "dashboard": {"specs", "dev_state_counts": {...},
                    "with_endpoints", "with_dependencies",
                    "implemented_pct", "verified", "avg_coverage",
                    "avg_test_coverage"},
      "specs": [{"id", "title", "status", "kind", "dev_state", "description",
                        "symbols": [{"qname", "signature"|null, "file", "line"}],
                        "endpoints": [str], "depends_on": [str],
                        "coverage": float|null,
                        "test_coverage_ratio": float, "coverage_source": str,
                        "tested_symbols": int, "total_symbols": int,
                        "uncovered_symbols": [str],
                        "uncovered_symbols_count": int}],
      "spec_topology": {"nodes": [{"id", "title", "dev_state"}],
                      "edges": [{"from", "to", "kind"}]},
      "endpoints": [{"kind": str, "framework"|null, "handler",
                     "signature"|null, "path"|null, "method"|null,
                     "spec_ids": [str]}],
      "fixtures": [{"kind": "fixture", ...same shape as an endpoint...}],
      "coverage": {"orphan_modules": [str], "orphan_endpoints": [str],
                   "totals": {...}},
      "trend": [{"ts": str, "avg_test_coverage": float|null,
                 "verified_count": int}],
      "changes": {"base": str|null, "head": str|null, "files_changed": [str],
                  "specs_touched": [{"spec_id", "title", "files": [str],
                                            "test_coverage_ratio": float}]}
    }

``endpoints`` is the real API surface only: ``kind`` ∈ {tool, resource,
prompt, other} (derived from the handler's decorator — mcp.tool /
mcp.resource / mcp.prompt / the Spec-plugin aliases, or a framework route).
``pytest.fixture`` entries are test infrastructure, NOT API surface, so
they are split into the separate ``fixtures`` collection and excluded from
``meta.counts.endpoints``. The viewer groups endpoints by ``kind``
Swagger-style and shows fixtures in a clearly-separate collapsed section.

The ``coverage`` float per Spec is the AVERAGE LINK CONFIDENCE of that Spec's
``spec_symbol`` rows — i.e. how confident the Spec↔code attributions are, NOT
test coverage and NOT call-graph reachability. The UI labels it as such.
SEPARATE from it, ``test_coverage_ratio`` (0..1) is REAL test coverage
(v0.15): the fraction of the Spec's implementing symbols reached by a test
symbol's depth-3 call cone UNIONED with explicit ``relation='tests'``
links — sourced from ``compute_spec_test_coverage`` / ``compute_coverage``'s
``spec_coverage`` list. ``coverage_source`` ∈ {derived, explicit, both, none}
records HOW that coverage is known. Both meters are shown, each labelled.

``dev_state`` is DERIVED from evidence (symbol links + that confidence +
real test coverage), independent of the manually-maintained ``status``:
    "not_started"  — 0 implementing symbols.
    "verified"     — has symbols AND ``test_coverage_ratio > 0`` (REAL test
                     coverage exists, derived + explicit). v0.15 supersedes
                     the old explicit-test-link-only rule.
    "in_progress"  — has symbols, no test coverage, AND coverage < 0.7.
    "implemented"  — has symbols, no test coverage, AND coverage >= 0.7.

Determinism: ``generated_at`` is the ONLY non-deterministic field and is
injectable (arg ``generated_at``, default None) so two runs on an
unchanged project produce byte-identical ``data.json`` except for it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from livespec_mcp.config import load_repo_config
from livespec_mcp.explorer.autowire import autowire_fastapi_explorer
from livespec_mcp.state import AppState
from livespec_mcp.storage.trends import read_trend
from livespec_mcp.tools.analysis import (
    compute_coverage,
    compute_diff_spec_impact,
    compute_endpoints,
)

_COVERAGE_THRESHOLD = 0.7


def _resolve_diff_range(
    ws_root: str, base: str | None, head: str | None
) -> tuple[str, str] | None:
    """Resolve the git range for the explorer's "Changes" section.

    Defaulting (when ``base``/``head`` are omitted): prefer ``main``..``HEAD``;
    if ``main`` is absent OR it resolves to the same commit as ``HEAD`` (no
    delta to show), fall back to ``HEAD~1``..``HEAD``. Returns ``None`` when
    the workspace is not a git repo / git is unavailable (the caller then
    omits the section). Explicit ``base``/``head`` are passed through verbatim
    and are NOT validated here — ``compute_diff_spec_impact`` already degrades to
    an empty shape on an unknown range.
    """
    if base is not None and head is not None:
        return base, head

    def _rev(ref: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", ws_root, "rev-parse", "--verify", "--quiet", ref],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        out = proc.stdout.strip()
        return out or None

    # No git / no HEAD (empty repo) -> omit the section entirely.
    head_sha = _rev("HEAD")
    if head_sha is None:
        return None

    eff_base = base
    eff_head = head or "HEAD"
    if eff_base is None:
        main_sha = _rev("main")
        # Prefer main..HEAD, but only if main exists AND differs from HEAD.
        if main_sha is not None and main_sha != head_sha:
            eff_base = "main"
        else:
            # Fall back to the previous commit; if HEAD has no parent
            # (single-commit repo), there is no range to show.
            if _rev("HEAD~1") is None:
                return None
            eff_base = "HEAD~1"
    return eff_base, eff_head


def _derive_dev_state(
    symbol_count: int, coverage: float | None, test_coverage_ratio: float
) -> str:
    """Derive a development state from code evidence (not the manual status).

    Thresholds: 0 symbols -> not_started; symbols with coverage < 0.7 ->
    in_progress; coverage >= 0.7 -> implemented. ``verified`` supersedes the
    others whenever REAL test coverage exists (``test_coverage_ratio > 0``) —
    the v0.15 call-graph-derived + explicit-test-link signal from
    ``compute_spec_test_coverage``. ``coverage`` here is average link confidence
    (see module docstring), the same signal the spine bar shows.
    """
    if symbol_count == 0:
        return "not_started"
    # Real test coverage (derived from the call graph and/or explicit test
    # links) is the strongest signal — it promotes to verified directly.
    if test_coverage_ratio > 0:
        return "verified"
    if coverage is None or coverage < _COVERAGE_THRESHOLD:
        return "in_progress"
    return "implemented"


def _framework_of_endpoint(ep: dict[str, Any]) -> str | None:
    """Derive a human framework label from a compute_endpoints entry."""
    if ep.get("hono_method") is not None or ep.get("hono_path") is not None:
        return "hono"
    if ep.get("http_method") is not None or ep.get("http_path") is not None:
        return str(ep.get("http_framework") or "fastapi")
    if ep.get("ts_framework"):
        return str(ep["ts_framework"])
    if ep.get("django_cbv_base"):
        return "django"
    return None


# Map an endpoint's decorator last-segment to a coarse "kind" for grouping.
# MCP servers carry no HTTP framework, so the decorator (mcp.tool / mcp.resource
# / mcp.prompt / pytest.fixture / the Spec-plugin aliases) is the real signal.
# Anything unrecognised falls back to "other" so the surface stays honest.
_KIND_BY_DECORATOR_LASTSEG: dict[str, str] = {
    "tool": "tool",
    "mutation_tool": "tool",
    "agentic_tool": "tool",
    "resource": "resource",
    "prompt": "prompt",
    "fixture": "fixture",
}


def _kind_of_endpoint(ep: dict[str, Any]) -> str:
    """Classify an endpoint as tool / resource / prompt / fixture / other.

    Derives from the ``decorators`` list (e.g. ``["mcp.tool"]`` -> tool,
    ``["pytest.fixture"]`` -> fixture). Framework-routed endpoints (hono /
    TS frameworks / django CBVs) have no MCP decorator and classify as
    "tool" — they are part of the API surface. Falls back to "other".
    """
    if _framework_of_endpoint(ep) is not None:
        return "tool"
    for dec in ep.get("decorators") or []:
        lastseg = str(dec).rsplit(".", 1)[-1]
        kind = _KIND_BY_DECORATOR_LASTSEG.get(lastseg)
        if kind is not None:
            return kind
    return "other"


def compute_explorer_data(
    st: AppState,
    generated_at: str | None = None,
    base: str | None = None,
    head: str | None = None,
) -> dict[str, Any]:
    """Build the full Spec Explorer data bundle for ``st``'s workspace.

    Pure read; reuses ``compute_endpoints`` + ``compute_coverage`` +
    ``compute_diff_spec_impact`` + ``read_trend``. The returned dict matches the
    data.json schema documented in the module docstring. ``generated_at`` is
    passed through verbatim (default None) so callers control determinism.

    ``base``/``head`` scope the top-level ``changes`` section (Spec-centric git
    diff impact). Both default to None — see ``_resolve_diff_range`` for the
    defaulting (``main``..``HEAD`` with a ``HEAD~1``..``HEAD`` fallback, omitted
    entirely when the workspace is not a git repo). Defaulting keeps the
    no-arg ``compute_explorer_data(st)`` call (used by indexing.py's freshness
    hook via ``write_explorer_bundle(st)``) working unchanged.
    """
    conn = st.conn
    pid = st.project_id

    # --- Specs + per-Spec symbols (with signatures) ---------------
    spec_rows = conn.execute(
        """SELECT id, spec_id, title, description, status, priority, kind
           FROM spec WHERE project_id=? ORDER BY spec_id""",
        (pid,),
    ).fetchall()

    # spec.id (internal pk) -> spec_id string, for topology edge resolution
    specid_by_pk: dict[int, str] = {int(r["id"]): r["spec_id"] for r in spec_rows}

    # Coverage audit (single load): reused for the per-Spec REAL test coverage
    # (v0.15) below AND the orphan/gaps section later. This is the ONLY
    # graph-backed coverage computation in this builder — no second load.
    # record=False: a bundle rebuild must not append a trend snapshot (M19).
    cov = compute_coverage(st, record=False)
    # spec_id -> {test_coverage_ratio, coverage_source, tested_symbols,
    # total_symbols} from compute_coverage's spec_coverage list.
    spec_cov_by_id: dict[str, dict[str, Any]] = {
        c["spec_id"]: c for c in cov.get("spec_coverage", [])
    }

    # symbol qname -> set of spec_ids (for endpoint -> Spec mapping). Built
    # from every spec_symbol link, regardless of relation.
    qname_to_specids: dict[str, list[str]] = {}
    for r in conn.execute(
        """SELECT spec.spec_id AS spec_id, s.qualified_name AS qname
           FROM spec_symbol rs
           JOIN spec ON spec.id = rs.spec_id
           JOIN symbol s ON s.id = rs.symbol_id
           WHERE spec.project_id=?
           ORDER BY spec.spec_id, s.qualified_name""",
        (pid,),
    ):
        qname_to_specids.setdefault(r["qname"], [])
        if r["spec_id"] not in qname_to_specids[r["qname"]]:
            qname_to_specids[r["qname"]].append(r["spec_id"])

    # depends_on edges (forward): parent -> child, by spec_id string
    depends_on: dict[str, list[str]] = {}
    topo_edges: list[dict[str, str]] = []
    for r in conn.execute(
        """SELECT parent_spec_id, child_spec_id, kind FROM spec_dependency
           WHERE parent_spec_id IN (SELECT id FROM spec WHERE project_id=?)
           ORDER BY parent_spec_id, child_spec_id, kind""",
        (pid,),
    ):
        parent = specid_by_pk.get(int(r["parent_spec_id"]))
        child = specid_by_pk.get(int(r["child_spec_id"]))
        if parent is None or child is None:
            continue
        depends_on.setdefault(parent, [])
        if child not in depends_on[parent]:
            depends_on[parent].append(child)
        topo_edges.append({"from": parent, "to": child, "kind": r["kind"]})

    # v0.20 M15: the real endpoint-handler qname set, computed ONCE up front so
    # each Spec's `endpoints` can be the intersection of its symbols with the
    # actual endpoint surface. The old filter (`spec_id in
    # qname_to_specids[qname]`) was always true for a spec's own symbols, so
    # every linked symbol was mislabeled an "owned endpoint" and
    # `dashboard.with_endpoints` was inflated.
    # Computed once and reused by the full endpoints section below (was
    # computed twice — a real cost on a large repo where it ast-parses files).
    raw_endpoints = compute_endpoints(st, framework=None)
    _endpoint_handler_qnames = {
        ep.get("qualified_name")
        for ep in raw_endpoints
        if ep.get("qualified_name")
    }

    specs: list[dict[str, Any]] = []
    total_spec_symbols = 0
    for spec in spec_rows:
        sym_rows = conn.execute(
            """SELECT s.qualified_name AS qname, s.signature, f.path AS file,
                      s.start_line AS line, rs.relation, rs.confidence
               FROM spec_symbol rs
               JOIN symbol s ON s.id = rs.symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE rs.spec_id = ?
               ORDER BY rs.confidence DESC, s.qualified_name, s.start_line""",
            (int(spec["id"]),),
        ).fetchall()
        symbols = [
            {
                "qname": sr["qname"],
                "signature": sr["signature"],
                "file": sr["file"],
                "line": int(sr["line"]),
            }
            for sr in sym_rows
        ]
        total_spec_symbols += len(symbols)
        # Coverage signal: avg LINK CONFIDENCE of this Spec's spec_symbol rows,
        # or None when there are no links (unimplemented). This is NOT test
        # coverage — it is how confident the Spec↔code attributions are.
        if sym_rows:
            coverage: float | None = round(
                sum(float(sr["confidence"]) for sr in sym_rows) / len(sym_rows), 4
            )
        else:
            coverage = None
        # REAL test coverage (v0.15): call-graph-derived + explicit test
        # links, from compute_spec_test_coverage. This SUPERSEDES the old
        # explicit-link-only verified rule. `coverage_source` ∈
        # {derived, explicit, both, none}.
        spec_id = spec["spec_id"]
        rc = spec_cov_by_id.get(spec_id)
        test_coverage_ratio = float(rc["test_coverage_ratio"]) if rc else 0.0
        coverage_source = rc["coverage_source"] if rc else "none"
        tested_symbols = int(rc["tested_symbols"]) if rc else 0
        total_symbols = int(rc["total_symbols"]) if rc else len(symbols)
        # v0.16 B: drill-down list of `implements` symbols with NO test
        # coverage (neither call-graph-reached nor explicitly tests-linked).
        # Sourced verbatim from compute_spec_test_coverage's spec_coverage entry
        # — already capped + counted there, NO recompute here.
        uncovered_symbols = list(rc["uncovered_symbols"]) if rc else []
        uncovered_symbols_count = int(rc["uncovered_symbols_count"]) if rc else 0
        dev_state = _derive_dev_state(len(symbols), coverage, test_coverage_ratio)
        # Endpoints owned by this Spec: this Spec's linked symbols that are
        # ACTUALLY endpoint handlers (intersect with the real endpoint set).
        owned_endpoints = sorted(
            {sr["qname"] for sr in sym_rows if sr["qname"] in _endpoint_handler_qnames}
        )
        specs.append(
            {
                "id": spec_id,
                "title": spec["title"],
                "status": spec["status"],
                "kind": spec["kind"],
                "dev_state": dev_state,
                "description": spec["description"] or "",
                "symbols": symbols,
                "endpoints": owned_endpoints,
                "depends_on": sorted(depends_on.get(spec_id, [])),
                "coverage": coverage,
                "test_coverage_ratio": test_coverage_ratio,
                "coverage_source": coverage_source,
                "tested_symbols": tested_symbols,
                "total_symbols": total_symbols,
                "uncovered_symbols": uncovered_symbols,
                "uncovered_symbols_count": uncovered_symbols_count,
            }
        )

    # --- Endpoints (full surface, framework-aware) ---------------------
    # compute_endpoints returns every decorated/route entry point INCLUDING
    # pytest fixtures (test infra, not API surface). We classify each by
    # `kind` (tool/resource/prompt/fixture/other) from its decorator, then
    # split fixtures out of the API surface so the headline count reflects
    # the real surface (tools + resources + prompts + framework routes).
    # raw_endpoints computed once above (reused here).
    endpoints: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    # qname -> signature lookup for endpoint handlers
    sig_by_qname: dict[str, str | None] = {}
    for ep in raw_endpoints:
        qn = ep.get("qualified_name")
        if qn and qn not in sig_by_qname:
            row = conn.execute(
                """SELECT s.signature FROM symbol s
                   JOIN file f ON f.id = s.file_id
                   WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
                (pid, qn),
            ).fetchone()
            sig_by_qname[qn] = row["signature"] if row else None
    for ep in raw_endpoints:
        handler = ep.get("qualified_name") or ""
        kind = _kind_of_endpoint(ep)
        entry = {
            "kind": kind,
            "framework": _framework_of_endpoint(ep),
            "handler": handler,
            "signature": sig_by_qname.get(handler),
            "path": ep.get("hono_path") or ep.get("http_path"),
            "method": ep.get("hono_method") or ep.get("http_method"),
            "spec_ids": list(qname_to_specids.get(handler, [])),
        }
        # pytest fixtures are test infrastructure, not API surface — they
        # are kept in a separate, clearly-labelled collection so the
        # endpoint count is not inflated by test scaffolding.
        if kind == "fixture":
            fixtures.append(entry)
        else:
            endpoints.append(entry)

    # --- Coverage / orphans --------------------------------------------
    # `cov` already computed once above (reused for per-Spec test coverage).
    orphan_endpoints = sorted(
        {ep["handler"] for ep in endpoints if not ep["spec_ids"] and ep["handler"]}
    )
    coverage_section = {
        "orphan_modules": list(cov["modules_truly_orphan"]),
        "orphan_endpoints": orphan_endpoints,
        "totals": dict(cov["counts"]),
    }

    # --- Topology nodes (colored by dev_state in the viewer) -----------
    dev_state_by_id = {r["id"]: r["dev_state"] for r in specs}
    topology = {
        "nodes": [
            {
                "id": r["spec_id"],
                "title": r["title"],
                "dev_state": dev_state_by_id.get(r["spec_id"], "not_started"),
            }
            for r in spec_rows
        ],
        "edges": topo_edges,
    }

    # --- Dashboard rollup (PO-first headline) --------------------------
    dev_state_counts = {
        "not_started": 0,
        "in_progress": 0,
        "implemented": 0,
        "verified": 0,
    }
    coverage_values: list[float] = []
    test_coverage_values: list[float] = []
    with_endpoints = 0
    with_dependencies = 0
    implemented_symbol_count = 0  # Specs with >= 1 implementing symbol
    verified_count = 0  # Specs with REAL test coverage (test_coverage_ratio > 0)
    for r in specs:
        dev_state_counts[r["dev_state"]] = dev_state_counts.get(r["dev_state"], 0) + 1
        if r["coverage"] is not None:
            coverage_values.append(r["coverage"])
        test_coverage_values.append(r["test_coverage_ratio"])
        if r["test_coverage_ratio"] > 0:
            verified_count += 1
        if r["endpoints"]:
            with_endpoints += 1
        if r["depends_on"]:
            with_dependencies += 1
        if r["symbols"]:
            implemented_symbol_count += 1
    total_reqs = len(specs)
    dashboard = {
        "specs": total_reqs,
        "dev_state_counts": dev_state_counts,
        "with_endpoints": with_endpoints,
        "with_dependencies": with_dependencies,
        # % of Specs that have >= 1 implementing symbol (any code attributed).
        "implemented_pct": (
            round(implemented_symbol_count / total_reqs * 100, 1)
            if total_reqs
            else 0.0
        ),
        # Specs backed by REAL test coverage (call-graph-derived + explicit).
        "verified": verified_count,
        # Mean of the per-Spec average-link-confidence values (Specs with links).
        "avg_coverage": (
            round(sum(coverage_values) / len(coverage_values), 4)
            if coverage_values
            else None
        ),
        # Mean of the per-Spec REAL test-coverage ratios (all Specs; None if no Specs).
        "avg_test_coverage": (
            round(sum(test_coverage_values) / len(test_coverage_values), 4)
            if test_coverage_values
            else None
        ),
    }

    files_count = conn.execute(
        "SELECT COUNT(*) c FROM file WHERE project_id=?", (pid,)
    ).fetchone()["c"]

    # --- Coverage trend (top-level) ------------------------------------
    # Chronological rollup snapshots recorded by each audit_coverage run.
    # Sourced verbatim from storage.trends.read_trend — NO recompute.
    trend = read_trend(conn, pid)

    # --- Git diff Spec impact (top-level "changes") ----------------------
    # Resolve the range (main..HEAD, HEAD~1..HEAD fallback, omitted off-git),
    # then delegate to compute_diff_spec_impact — NO diff logic recomputed here.
    ws_root = str(st.settings.workspace)
    rng = _resolve_diff_range(ws_root, base, head)
    if rng is None:
        changes: dict[str, Any] = {
            "base": None,
            "head": None,
            "files_changed": [],
            "specs_touched": [],
        }
    else:
        changes = compute_diff_spec_impact(st, rng[0], rng[1])

    return {
        "meta": {
            "project": st.settings.workspace.name,
            "generated_at": generated_at,
            # Overwritten in write_explorer_bundle from .livespec.toml [explorer].mount_path
            "base_path": "/explorer",
            "counts": {
                "specs": len(spec_rows),
                "symbols": total_spec_symbols,
                "endpoints": len(endpoints),
                "files": int(files_count),
            },
        },
        "dashboard": dashboard,
        "specs": specs,
        "spec_topology": topology,
        "endpoints": endpoints,
        # pytest fixtures, kept separate from the API surface (same shape as
        # an endpoint entry). The viewer shows these in a collapsed section.
        "fixtures": fixtures,
        "coverage": coverage_section,
        # Coverage trend over recorded audit snapshots (chronological).
        "trend": trend,
        # Spec-centric git diff impact for the resolved range (empty off-git).
        "changes": changes,
    }


def _render_index_html(data: dict[str, Any]) -> str:
    """Render the single self-contained viewer with ``data`` inlined.

    Vanilla JS only, one Mermaid CDN <script>. The data is embedded as a
    typed JSON <script> block so the page works over file:// with zero CORS.
    The visual layer is a refined internal-developer-portal aesthetic with
    light/dark support, a confident accent, and a themed Mermaid topology.
    """
    # Inline JSON; </script> can't appear in the data we emit, but escape
    # defensively so a stray sequence can't break the script element.
    inlined = json.dumps(data, indent=2).replace("</", "<\\/")
    project = data["meta"]["project"]
    return _HTML_TEMPLATE.replace("__PROJECT__", _html_escape(project)).replace(
        "__DATA__", inlined
    )


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_explorer_bundle(
    st: AppState,
    generated_at: str | None = None,
    base: str | None = None,
    head: str | None = None,
) -> dict[str, Any]:
    """Compute + write data.json and index.html under .mcp-docs/explorer/.

    Returns ``{"data": <bundle>, "files_written": [<abs paths>]}``.

    ``base``/``head`` default to None and are threaded into
    ``compute_explorer_data`` for the "Changes" section. The no-arg call
    ``write_explorer_bundle(st)`` (indexing.py's freshness hook) therefore
    keeps working unchanged — the range defaults to ``main``..``HEAD`` (or is
    omitted off-git).
    """
    data = compute_explorer_data(st, generated_at=generated_at, base=base, head=head)
    repo_cfg = load_repo_config(st.settings.workspace)
    data["meta"]["base_path"] = repo_cfg.explorer_mount_path
    out_dir: Path = st.settings.state_dir / "explorer"
    out_dir.mkdir(parents=True, exist_ok=True)

    data_path = out_dir / "data.json"
    html_path = out_dir / "index.html"
    data_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(_render_index_html(data), encoding="utf-8")

    repo_cfg = load_repo_config(st.settings.workspace)
    autowire = autowire_fastapi_explorer(
        st.settings.workspace,
        auto_mount=repo_cfg.explorer_auto_mount,
        mount_path=repo_cfg.explorer_mount_path,
    )

    return {
        "data": data,
        "files_written": [str(data_path), str(html_path)],
        "autowire": {
            "wired": autowire.wired,
            "file": autowire.file,
            "app_var": autowire.app_var,
            "reason": autowire.reason,
            "mount_path": repo_cfg.explorer_mount_path,
        },
    }


# --- The single self-contained viewer ----------------------------------
# Vanilla JS, one Mermaid CDN script, data inlined. The page is fully
# self-contained and opens over file:// with zero server. The ONLY
# external dependency is the Mermaid CDN <script> below — offline, the
# Topology tab degrades to a readable plain-text graph source (no other
# tab depends on it). Everything else (fonts, colors, JS) is local.
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spec Explorer · __PROJECT__</title>
<!-- Single external dep. Offline: the Topology tab falls back to text. -->
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  :root {
    color-scheme: light dark;
    /* Surface / elevation scale (neutral, slightly cool) */
    --bg:        #f7f8fb;
    --surface:   #ffffff;
    --surface-2: #f1f3f8;
    --surface-3: #e9ecf4;
    --line:      #e3e6ef;
    --line-soft: #eef0f6;
    /* Text */
    --fg:        #1c2130;
    --fg-soft:   #424a5e;
    --muted:     #6a7488;
    --faint:     #9aa3b5;
    /* One confident accent */
    --accent:      #5848d6;
    --accent-fg:   #ffffff;
    --accent-weak: #ece9fb;
    --accent-line: #d8d2f6;
    --accent-ink:  #4536b8;
    /* Semantic (status / coverage / gaps) */
    --ok:        #1f8a5b;
    --ok-weak:   #e3f4ec;
    --warn:      #b06a00;
    --warn-weak: #f7efdf;
    --danger:    #c0392f;
    --danger-weak:#f8e7e5;
    --info:      #2f6fb0;
    --info-weak: #e4eef8;
    /* Type */
    --font: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --radius: 10px;
    --radius-sm: 7px;
    --shadow: 0 1px 2px rgba(20,24,40,.05), 0 6px 18px -8px rgba(20,24,40,.14);
    --shadow-sm: 0 1px 2px rgba(20,24,40,.06);
    --header-h: 116px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:        #0e1117;
      --surface:   #161b24;
      --surface-2: #1c222d;
      --surface-3: #232a37;
      --line:      #29303d;
      --line-soft: #20262f;
      --fg:        #e8ebf2;
      --fg-soft:   #c2c8d4;
      --muted:     #8a93a6;
      --faint:     #5c6678;
      --accent:      #897ff0;
      --accent-fg:   #11131c;
      --accent-weak: #21223a;
      --accent-line: #34335a;
      --accent-ink:  #b3aaff;
      --ok:        #4cc38a;  --ok-weak:   #16291f;
      --warn:      #e0a445;  --warn-weak: #2b2316;
      --danger:    #f0786b;  --danger-weak:#2c1a18;
      --info:      #6db0e8;  --info-weak: #15222e;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -10px rgba(0,0,0,.6);
      --shadow-sm: 0 1px 2px rgba(0,0,0,.4);
    }
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: var(--font);
    font-size: 14px;
    line-height: 1.5;
    color: var(--fg);
    background:
      radial-gradient(1100px 420px at 78% -8%, var(--accent-weak), transparent 60%),
      var(--bg);
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  a { color: var(--accent-ink); text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* ---- Focus visibility (keyboard nav) ---- */
  :focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: 4px;
  }
  *:focus:not(:focus-visible) { outline: none; }

  /* ---- Header ---- */
  header.app {
    position: sticky; top: 0; z-index: 20;
    padding: 16px 28px 0;
    background: color-mix(in srgb, var(--surface) 82%, transparent);
    backdrop-filter: saturate(140%) blur(8px);
    border-bottom: 1px solid var(--line);
  }
  .brand { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .brand .mark {
    width: 30px; height: 30px; border-radius: 9px; flex: none;
    display: grid; place-items: center;
    background: linear-gradient(150deg, var(--accent), color-mix(in srgb, var(--accent) 55%, #2a8bd6));
    color: var(--accent-fg); box-shadow: var(--shadow-sm);
    font-weight: 700; font-size: 14px; letter-spacing: .5px;
  }
  .brand h1 {
    font-size: 16px; font-weight: 650; margin: 0; letter-spacing: -.01em;
    display: flex; align-items: baseline; gap: 9px;
  }
  .brand h1 .proj { color: var(--accent-ink); font-weight: 700; }
  .brand h1 .kicker {
    font-size: 11px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: .09em;
  }
  .stats { display: flex; gap: 22px; flex-wrap: wrap; margin: 12px 0 14px; }
  .stat { display: flex; flex-direction: column; gap: 1px; }
  .stat .n {
    font-size: 19px; font-weight: 700; line-height: 1; letter-spacing: -.02em;
    font-variant-numeric: tabular-nums;
  }
  .stat .l {
    font-size: 10.5px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: .07em;
  }

  /* ---- Tabs ---- */
  nav.tabs { display: flex; gap: 2px; }
  nav.tabs button {
    appearance: none; border: 0; background: none; cursor: pointer;
    font: inherit; font-size: 13px; font-weight: 550;
    color: var(--muted); padding: 9px 13px 11px;
    border-bottom: 2px solid transparent;
    border-radius: 8px 8px 0 0;
    transition: color .15s ease, background .15s ease;
  }
  nav.tabs button:hover { color: var(--fg); background: var(--surface-2); }
  nav.tabs button[aria-current="page"] {
    color: var(--accent-ink); border-bottom-color: var(--accent);
  }
  nav.tabs button .pill {
    display: inline-block; margin-left: 6px; padding: 0 6px;
    font-size: 11px; font-weight: 650; border-radius: 999px;
    background: var(--surface-3); color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  nav.tabs button[aria-current="page"] .pill {
    background: var(--accent-weak); color: var(--accent-ink);
  }

  /* ---- Layout ---- */
  .panel { display: none; }
  .panel.active { display: block; }
  .split {
    display: grid; grid-template-columns: 300px minmax(0, 1fr);
    height: calc(100vh - var(--header-h));
  }
  main.scroll, .col-scroll {
    overflow-y: auto; height: calc(100vh - var(--header-h));
  }
  main.pad { padding: 26px 32px 60px; max-width: 1180px; }

  /* ---- Sidebar / Spec spine ---- */
  aside.spine {
    border-right: 1px solid var(--line);
    background: var(--surface);
    display: flex; flex-direction: column;
    height: calc(100vh - var(--header-h));
  }
  .spine .search {
    padding: 13px 14px 11px; border-bottom: 1px solid var(--line-soft);
    position: sticky; top: 0; background: var(--surface); z-index: 2;
  }
  .spine .search input {
    width: 100%; font: inherit; font-size: 13px;
    padding: 8px 11px 8px 30px; border-radius: 9px;
    border: 1px solid var(--line); background: var(--surface-2); color: var(--fg);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='15' height='15' viewBox='0 0 24 24' fill='none' stroke='%236a7488' stroke-width='2.2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='m21 21-4.3-4.3'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: 9px center;
    transition: border-color .15s ease, background .15s ease;
  }
  .spine .search input::placeholder { color: var(--faint); }
  .spine .search input:focus {
    border-color: var(--accent); background: var(--surface);
    box-shadow: 0 0 0 3px var(--accent-weak);
    outline: none;
  }
  .spine .list { overflow-y: auto; flex: 1; padding: 6px; }
  .spine .spec {
    display: block; width: 100%; text-align: left; appearance: none; border: 0;
    font: inherit; cursor: pointer; color: inherit;
    padding: 9px 11px; margin: 1px 0; border-radius: 9px;
    background: transparent; position: relative;
    transition: background .13s ease;
  }
  .spine .spec:hover { background: var(--surface-2); }
  .spine .spec[aria-current="true"] {
    background: var(--accent-weak);
    box-shadow: inset 0 0 0 1px var(--accent-line);
  }
  .spine .spec[aria-current="true"]::before {
    content: ""; position: absolute; left: -1px; top: 9px; bottom: 9px;
    width: 3px; border-radius: 3px; background: var(--accent);
  }
  .spine .spec .top { display: flex; align-items: center; gap: 7px; }
  .spine .spec .rid {
    font-family: var(--mono); font-size: 11px; font-weight: 650;
    color: var(--accent-ink); letter-spacing: -.01em;
  }
  .spine .spec[aria-current="true"] .rid { color: var(--accent-ink); }
  .spine .spec .ti {
    font-size: 13px; font-weight: 530; margin-top: 2px; color: var(--fg-soft);
    line-height: 1.35;
  }
  .spine .spec[aria-current="true"] .ti { color: var(--fg); }
  .spine .spec .dot {
    width: 7px; height: 7px; border-radius: 50%; flex: none; margin-left: auto;
  }
  .spine .none { padding: 18px 16px; color: var(--muted); font-style: italic; font-size: 13px; }

  /* ---- Status dots & badges ---- */
  .dot.st-draft       { background: var(--faint); }
  .dot.st-approved    { background: var(--ok); }
  .dot.st-in_progress { background: var(--info); }
  .dot.st-done        { background: var(--ok); }
  .dot.st-deprecated  { background: var(--danger); }

  /* ---- dev_state colour system (the PO-facing development state) ----
     not_started=neutral, in_progress=info, implemented=accent,
     verified=ok. Used by dots, pills, the breakdown bar and topology. */
  .ds-not_started { --ds: var(--faint);  --ds-weak: var(--surface-3); }
  .ds-in_progress { --ds: var(--info);   --ds-weak: var(--info-weak); }
  .ds-implemented { --ds: var(--accent); --ds-weak: var(--accent-weak); }
  .ds-verified    { --ds: var(--ok);     --ds-weak: var(--ok-weak); }
  .dot.ds { background: var(--ds, var(--faint)); }
  .state-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 2px 10px 2px 8px; border-radius: 999px;
    font-size: 11.5px; font-weight: 650; line-height: 1.7;
    background: var(--ds-weak); color: var(--ds);
    border: 1px solid color-mix(in srgb, var(--ds) 30%, transparent);
    white-space: nowrap;
  }
  .state-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ds); flex: none; }
  .state-pill.big { font-size: 13px; padding: 4px 13px 4px 11px; }
  .state-pill.big .dot { width: 8px; height: 8px; }

  /* ---- Project dashboard (PO headline) ---- */
  .dash { margin: 0 0 26px; }
  .dash-tiles {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
    gap: 12px; margin-bottom: 16px;
  }
  .dash-tile {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow-sm);
  }
  .dash-tile .n {
    font-size: 26px; font-weight: 700; letter-spacing: -.02em; line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .dash-tile .n .unit { font-size: 15px; font-weight: 650; color: var(--muted); }
  .dash-tile .k {
    font-size: 11px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: .05em; margin-top: 7px;
  }
  .dash-tile.good .n { color: var(--ok); }
  .dash-tile.accent .n { color: var(--accent-ink); }

  /* status breakdown bar */
  .breakdown { margin-top: 4px; }
  .breakdown .bar {
    display: flex; height: 16px; border-radius: 8px; overflow: hidden;
    border: 1px solid var(--line); background: var(--surface-2);
  }
  .breakdown .bar > span { height: 100%; min-width: 2px; }
  .breakdown .seg-not_started { background: var(--faint); }
  .breakdown .seg-in_progress { background: var(--info); }
  .breakdown .seg-implemented { background: var(--accent); }
  .breakdown .seg-verified    { background: var(--ok); }
  .breakdown .keys {
    display: flex; gap: 16px; flex-wrap: wrap; margin-top: 9px;
    font-size: 12px; color: var(--muted);
  }
  .breakdown .keys .item { display: inline-flex; align-items: center; gap: 6px; }
  .breakdown .keys .sw { width: 11px; height: 11px; border-radius: 3px; flex: none; }
  .breakdown .keys .sw.not_started { background: var(--faint); }
  .breakdown .keys .sw.in_progress { background: var(--info); }
  .breakdown .keys .sw.implemented { background: var(--accent); }
  .breakdown .keys .sw.verified    { background: var(--ok); }
  .breakdown .keys .item b { color: var(--fg); font-variant-numeric: tabular-nums; }

  /* dev_state filter chips */
  .ds-filters { display: flex; gap: 7px; flex-wrap: wrap; padding: 11px 14px 12px; border-bottom: 1px solid var(--line-soft); }
  .ds-chip {
    appearance: none; border: 1px solid var(--line); cursor: pointer;
    font: inherit; font-size: 11.5px; font-weight: 600;
    padding: 3px 10px; border-radius: 999px;
    background: var(--surface-2); color: var(--fg-soft);
    display: inline-flex; align-items: center; gap: 6px;
    transition: background .12s ease, border-color .12s ease;
  }
  .ds-chip .sw { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .ds-chip .sw.all { background: linear-gradient(135deg, var(--accent), var(--ok)); }
  .ds-chip .sw.not_started { background: var(--faint); }
  .ds-chip .sw.in_progress { background: var(--info); }
  .ds-chip .sw.implemented { background: var(--accent); }
  .ds-chip .sw.verified    { background: var(--ok); }
  .ds-chip .ct { font-variant-numeric: tabular-nums; color: var(--muted); font-weight: 650; }
  .ds-chip:hover { background: var(--surface-3); }
  .ds-chip[aria-pressed="true"] {
    background: var(--accent-weak); color: var(--accent-ink); border-color: var(--accent-line);
  }
  .ds-chip[aria-pressed="true"] .ct { color: var(--accent-ink); }

  /* spine coverage micro-bar + summary line */
  .spine .spec .sub { font-size: 11.5px; color: var(--muted); margin-top: 3px; line-height: 1.4; }
  .spine .spec .covbar {
    height: 4px; border-radius: 3px; margin-top: 6px;
    background: color-mix(in srgb, var(--muted) 22%, transparent); overflow: hidden;
  }
  .spine .spec .covbar > span { display: block; height: 100%; border-radius: 3px; background: var(--ds, var(--accent)); }

  /* stale-status flag + how-derived note */
  .stale-flag {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 999px;
    background: var(--warn-weak); color: var(--warn);
    border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
  }
  .derive-note {
    font-size: 12.5px; color: var(--muted); line-height: 1.6;
    background: var(--surface-2); border: 1px solid var(--line-soft);
    border-radius: var(--radius-sm); padding: 11px 14px; margin: 0 0 18px;
    max-width: 78ch;
  }
  .derive-note b { color: var(--fg-soft); font-weight: 650; }
  .derive-note .lbl { font-weight: 700; color: var(--ds, var(--fg)); }

  /* metric row (PO detail) */
  .metrics {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 10px; margin: 16px 0 6px;
  }
  .metric {
    background: var(--surface-2); border: 1px solid var(--line-soft);
    border-radius: var(--radius-sm); padding: 11px 13px;
  }
  .metric .mv { font-size: 19px; font-weight: 700; letter-spacing: -.02em; font-variant-numeric: tabular-nums; line-height: 1; }
  .metric .ml { font-size: 10.5px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-top: 6px; }
  .metric .hint { font-size: 10.5px; color: var(--faint); margin-top: 3px; font-weight: 500; text-transform: none; letter-spacing: 0; }

  /* collapsible technical detail */
  details.tech {
    margin-top: 26px; border: 1px solid var(--line);
    border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow-sm);
    overflow: hidden;
  }
  details.tech > summary {
    cursor: pointer; list-style: none; padding: 13px 16px;
    font-size: 12.5px; font-weight: 650; color: var(--fg-soft);
    display: flex; align-items: center; gap: 9px;
    background: var(--surface-2);
  }
  details.tech > summary::-webkit-details-marker { display: none; }
  details.tech > summary::before {
    content: "▸"; color: var(--muted); font-size: 11px; transition: transform .15s ease;
  }
  details.tech[open] > summary::before { transform: rotate(90deg); }
  details.tech > summary .hint { font-weight: 500; color: var(--muted); font-size: 11.5px; }
  details.tech .tech-body { padding: 6px 16px 18px; }

  .chip {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 2px 9px; border-radius: 999px;
    font-size: 11.5px; font-weight: 600; line-height: 1.7;
    background: var(--surface-3); color: var(--fg-soft);
    border: 1px solid transparent;
  }
  .chip.mono { font-family: var(--mono); font-size: 11px; font-weight: 550; }
  .chip.accent { background: var(--accent-weak); color: var(--accent-ink); border-color: var(--accent-line); }
  .chip.ok     { background: var(--ok-weak);     color: var(--ok); }
  .chip.warn   { background: var(--warn-weak);   color: var(--warn); }
  .chip.info   { background: var(--info-weak);   color: var(--info); }
  .chip.muted  { background: transparent; color: var(--muted); border-color: var(--line); }
  .chip.status {
    text-transform: capitalize; letter-spacing: .01em;
  }
  button.chip {
    appearance: none; font-family: var(--mono); cursor: pointer;
    transition: transform .1s ease, box-shadow .12s ease, background .12s ease;
  }
  button.chip.dep {
    background: var(--accent-weak); color: var(--accent-ink); border-color: var(--accent-line);
  }
  button.chip.dep:hover {
    background: var(--accent); color: var(--accent-fg);
    border-color: var(--accent); box-shadow: var(--shadow-sm);
  }
  button.chip.dep::after { content: "→"; opacity: .55; font-weight: 700; }

  /* ---- Detail panel ---- */
  .detail-head { margin-bottom: 4px; }
  .detail-head .eyebrow {
    font-family: var(--mono); font-size: 12px; font-weight: 650;
    color: var(--accent-ink); letter-spacing: -.01em;
  }
  .detail-head h2.title {
    font-size: 23px; font-weight: 700; letter-spacing: -.02em;
    margin: 3px 0 11px; line-height: 1.2;
  }
  .meta-row { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
  .desc {
    font-size: 14.5px; color: var(--fg-soft); line-height: 1.62;
    max-width: 70ch; margin: 0 0 8px;
  }

  /* Coverage meter inside the chip cluster */
  .cov {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 2px 11px 2px 4px; border-radius: 999px;
    background: var(--surface-3); border: 1px solid var(--line);
  }
  .cov .track {
    width: 64px; height: 6px; border-radius: 4px; overflow: hidden;
    background: color-mix(in srgb, var(--muted) 24%, transparent);
  }
  .cov .fill { height: 100%; border-radius: 4px; background: var(--accent); }
  .cov.high .fill { background: var(--ok); }
  .cov.mid  .fill { background: var(--warn); }
  .cov.low  .fill { background: var(--danger); }
  .cov .v { font-size: 12px; font-weight: 650; font-variant-numeric: tabular-nums; color: var(--fg-soft); }

  /* Two distinct coverage meters (real test coverage + link confidence) */
  .cov-meters {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 14px; margin: 16px 0 4px;
  }
  .cov-meter {
    background: var(--surface-2); border: 1px solid var(--line-soft);
    border-radius: var(--radius-sm); padding: 12px 14px;
  }
  .cov-meter-h { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 9px; }
  .cov-meter-l {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted);
  }
  .cov-meter .cov {
    width: 100%; padding: 4px 12px 4px 4px;
    background: var(--surface); border-color: var(--line-soft);
  }
  .cov-meter .cov .track { flex: 1; width: auto; height: 8px; }
  .cov-meter-hint {
    font-size: 10.5px; color: var(--faint); margin-top: 7px;
    font-weight: 500; line-height: 1.45;
  }

  /* Section headings */
  .sec { margin-top: 28px; }
  .sec-h {
    display: flex; align-items: center; gap: 9px;
    font-size: 11.5px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); margin: 0 0 11px;
  }
  .sec-h .ct {
    font-family: var(--mono); font-size: 11px; font-weight: 650;
    padding: 0 7px; border-radius: 999px; letter-spacing: 0;
    background: var(--surface-3); color: var(--muted);
  }
  .sec-h::after {
    content: ""; flex: 1; height: 1px;
    background: linear-gradient(90deg, var(--line), transparent);
  }

  /* Cards & tables */
  .card {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); box-shadow: var(--shadow-sm);
    overflow: hidden;
  }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  thead th {
    text-align: left; font-size: 10.5px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted);
    padding: 9px 14px; background: var(--surface-2);
    border-bottom: 1px solid var(--line); position: sticky; top: 0;
  }
  tbody td { padding: 9px 14px; border-bottom: 1px solid var(--line-soft); vertical-align: top; }
  tbody tr:last-child td { border-bottom: 0; }
  tbody tr { transition: background .1s ease; }
  tbody tr:hover { background: var(--surface-2); }
  td.mono, .mono { font-family: var(--mono); }
  td .qname { font-family: var(--mono); font-size: 12px; font-weight: 600; color: var(--fg); }
  td .sig { font-family: var(--mono); font-size: 11.5px; color: var(--muted); }
  td .loc {
    font-family: var(--mono); font-size: 11px; color: var(--muted);
    white-space: nowrap;
  }
  td .loc b { color: var(--accent-ink); font-weight: 650; }

  .clusterbox {
    display: flex; flex-wrap: wrap; gap: 6px;
    padding: 4px 0;
  }

  /* Endpoints groups */
  .epgroup { margin-bottom: 22px; }
  .epgroup-h {
    display: flex; align-items: center; gap: 10px; margin: 0 0 9px;
  }
  .epgroup-h .fw {
    font-size: 14px; font-weight: 650; letter-spacing: -.01em;
  }
  .epgroup-h .fwtag {
    font-family: var(--mono); font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .05em;
    padding: 2px 8px; border-radius: 6px;
    background: var(--accent-weak); color: var(--accent-ink);
  }
  .method {
    font-family: var(--mono); font-size: 10.5px; font-weight: 700;
    padding: 1px 7px; border-radius: 5px; letter-spacing: .03em;
    background: var(--surface-3); color: var(--fg-soft);
  }
  .method.get    { background: var(--ok-weak);   color: var(--ok); }
  .method.post   { background: var(--info-weak);  color: var(--info); }
  .method.put    { background: var(--warn-weak);  color: var(--warn); }
  .method.delete { background: var(--danger-weak);color: var(--danger); }
  .path { font-family: var(--mono); font-size: 12px; color: var(--fg-soft); }

  /* Endpoints: static-spec note + per-handler qname + copy-call affordance */
  .ep-note {
    font-size: 12.5px; color: var(--fg-soft); line-height: 1.55;
    background: var(--info-weak); border: 1px solid color-mix(in srgb, var(--info) 28%, transparent);
    border-radius: var(--radius-sm); padding: 10px 14px; margin: 0 0 14px; max-width: 80ch;
  }
  .ep-note b { color: var(--info); font-weight: 700; }
  .ep-q { font-family: var(--mono); font-size: 10.5px; color: var(--faint); margin-top: 2px; word-break: break-all; }
  .ep-route { margin-top: 4px; }
  button.copy-call {
    appearance: none; cursor: pointer; font: inherit;
    font-family: var(--mono); font-size: 11px; font-weight: 600;
    padding: 3px 9px; border-radius: 7px; white-space: nowrap;
    background: var(--surface-2); color: var(--accent-ink);
    border: 1px solid var(--line);
    transition: background .12s ease, border-color .12s ease, color .12s ease;
  }
  button.copy-call:hover { background: var(--accent-weak); border-color: var(--accent-line); }
  button.copy-call.copied { background: var(--ok-weak); color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, transparent); }
  pre.call-shape {
    font-family: var(--mono); font-size: 11px; color: var(--fg-soft);
    margin: 7px 0 0; padding: 9px 11px; white-space: pre-wrap; word-break: break-word;
    background: var(--surface-2); border: 1px solid var(--line-soft); border-radius: var(--radius-sm);
  }
  details.ep-fixtures { margin-top: 26px; }

  /* ---- Landing (/) ---- */
  .landing-hero { margin-bottom: 28px; }
  .landing-hero h2 {
    font-size: 28px; font-weight: 750; letter-spacing: -.03em;
    margin: 0 0 8px; line-height: 1.15;
  }
  .landing-hero .sub { font-size: 15px; color: var(--muted); max-width: 62ch; margin: 0; }
  .nav-cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 14px; margin-top: 22px;
  }
  .nav-card {
    display: block; text-decoration: none; color: inherit;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 16px 18px; box-shadow: var(--shadow-sm);
    transition: border-color .14s ease, box-shadow .14s ease, transform .14s ease;
  }
  .nav-card:hover {
    border-color: var(--accent-line); box-shadow: var(--shadow);
    transform: translateY(-1px); text-decoration: none;
  }
  .nav-card .nc-title { font-size: 15px; font-weight: 650; margin: 0 0 4px; }
  .nav-card .nc-desc { font-size: 12.5px; color: var(--muted); margin: 0; line-height: 1.45; }
  .nav-card .nc-count {
    display: inline-block; margin-top: 10px;
    font-family: var(--mono); font-size: 11px; font-weight: 650;
    padding: 2px 8px; border-radius: 999px;
    background: var(--accent-weak); color: var(--accent-ink);
  }

  /* ---- Swagger-style endpoints ---- */
  .swagger-toolbar {
    display: flex; gap: 12px; flex-wrap: wrap; align-items: center;
    margin: 0 0 18px;
  }
  .swagger-toolbar input {
    flex: 1; min-width: 200px; font: inherit; font-size: 13px;
    padding: 9px 12px 9px 34px; border-radius: 9px;
    border: 1px solid var(--line); background: var(--surface);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='15' height='15' viewBox='0 0 24 24' fill='none' stroke='%236a7488' stroke-width='2.2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='m21 21-4.3-4.3'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: 10px center;
  }
  .swagger-toolbar input:focus {
    border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-weak); outline: none;
  }
  .swagger-tag { margin-bottom: 28px; }
  .swagger-tag-h {
    display: flex; align-items: baseline; gap: 10px;
    margin: 0 0 10px; padding-bottom: 8px;
    border-bottom: 1px solid var(--line);
  }
  .swagger-tag-h .name { font-size: 18px; font-weight: 700; letter-spacing: -.02em; }
  .swagger-tag-h .ct {
    font-family: var(--mono); font-size: 11px; font-weight: 650;
    padding: 2px 8px; border-radius: 999px;
    background: var(--surface-3); color: var(--muted);
  }
  .swagger-tag-h .desc { font-size: 13px; color: var(--muted); margin-left: auto; }
  .swagger-ops { display: flex; flex-direction: column; gap: 8px; }
  details.swagger-op {
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    background: var(--surface); overflow: hidden;
    box-shadow: var(--shadow-sm);
  }
  details.swagger-op[open] { border-color: var(--accent-line); }
  .swagger-op-summary {
    list-style: none; cursor: pointer; display: flex; align-items: stretch;
    gap: 0; min-height: 44px;
  }
  .swagger-op-summary::-webkit-details-marker { display: none; }
  .op-method {
    display: flex; align-items: center; justify-content: center;
    min-width: 72px; padding: 0 12px;
    font-family: var(--mono); font-size: 11px; font-weight: 800;
    letter-spacing: .04em; text-transform: uppercase; color: #fff;
    flex: none;
  }
  .op-method.get     { background: #49cc90; }
  .op-method.post    { background: #61affe; }
  .op-method.put     { background: #fca130; color: #1c2130; }
  .op-method.patch   { background: #50e3c2; color: #1c2130; }
  .op-method.delete  { background: #f93e3e; }
  .op-method.head,
  .op-method.options { background: #9012fe; }
  .op-method.tool      { background: #5848d6; }
  .op-method.resource  { background: #2f6fb0; }
  .op-method.prompt    { background: #b06a00; }
  .op-method.other     { background: #6a7488; }
  .op-main {
    flex: 1; display: flex; align-items: center; gap: 12px;
    padding: 8px 14px; min-width: 0;
  }
  .op-path {
    font-family: var(--mono); font-size: 14px; font-weight: 600;
    color: var(--fg); word-break: break-all;
  }
  .op-sub {
    font-family: var(--mono); font-size: 11px; color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    max-width: 280px;
  }
  .op-chevron {
    display: flex; align-items: center; padding: 0 14px;
    color: var(--muted); font-size: 12px; flex: none;
  }
  details[open] .op-chevron { transform: rotate(90deg); }
  .swagger-op-body {
    border-top: 1px solid var(--line-soft);
    padding: 14px 16px 16px; background: var(--surface-2);
  }
  .op-section { margin-bottom: 14px; }
  .op-section:last-child { margin-bottom: 0; }
  .op-section-h {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .07em; color: var(--muted); margin: 0 0 8px;
  }
  table.op-params { font-size: 12.5px; }
  table.op-params td:first-child { font-family: var(--mono); font-weight: 600; width: 28%; }
  table.op-params td:nth-child(2) { font-family: var(--mono); color: var(--muted); width: 18%; }
  .op-try {
    display: flex; align-items: flex-start; gap: 10px; flex-wrap: wrap;
  }
  .op-try pre.call-shape { flex: 1; min-width: 200px; margin: 0; }
  .http-try {
    display: flex; flex-direction: column; gap: 8px; width: 100%;
  }
  .http-try-row {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  }
  .http-cors-note {
    font-size: 11.5px; color: var(--muted); line-height: 1.45; max-width: 72ch; margin: 0;
  }
  button.http-exec {
    appearance: none; cursor: pointer; font: inherit;
    font-size: 12px; font-weight: 650; padding: 6px 14px; border-radius: 7px;
    background: var(--accent); color: var(--accent-fg); border: 0;
    transition: opacity .12s ease;
  }
  button.http-exec:hover { opacity: .92; }
  button.http-exec:disabled { opacity: .55; cursor: wait; }
  .http-response {
    font-family: var(--mono); font-size: 11px; color: var(--fg-soft);
    margin: 0; padding: 9px 11px; white-space: pre-wrap; word-break: break-word;
    background: var(--surface-2); border: 1px solid var(--line-soft); border-radius: var(--radius-sm);
    max-height: 220px; overflow: auto;
  }
  .http-response.ok { border-color: color-mix(in srgb, var(--ok) 35%, transparent); }
  .http-response.err { border-color: color-mix(in srgb, var(--danger) 35%, transparent); }
  .swagger-toolbar .base-url-wrap {
    display: flex; align-items: center; gap: 8px; flex: 1; min-width: 240px;
  }
  .swagger-toolbar .base-url-wrap label {
    font-size: 11px; font-weight: 650; color: var(--muted); white-space: nowrap;
  }
  .swagger-toolbar .base-url-wrap input {
    flex: 1; min-width: 160px; font-family: var(--mono); font-size: 12px;
    padding: 9px 12px; border-radius: 9px;
    border: 1px solid var(--line); background: var(--surface);
  }

  /* Gaps */
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 8px; }
  .kpi {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow-sm);
  }
  .kpi .n { font-size: 24px; font-weight: 700; letter-spacing: -.02em; font-variant-numeric: tabular-nums; line-height: 1; }
  .kpi .k { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-top: 6px; }
  .kpi.flag .n { color: var(--warn); }
  .kpi.good .n { color: var(--ok); }
  .orphan-list {
    list-style: none; margin: 0; padding: 6px;
    columns: 2; column-gap: 10px;
  }
  @media (max-width: 760px) { .orphan-list { columns: 1; } }
  .orphan-list li {
    break-inside: avoid; font-family: var(--mono); font-size: 11.5px;
    color: var(--fg-soft); padding: 5px 9px; border-radius: 7px;
    border-left: 2px solid var(--warn); background: var(--surface-2);
    margin-bottom: 5px; word-break: break-all;
  }
  .lead { color: var(--muted); font-size: 13px; max-width: 70ch; margin: -4px 0 18px; }

  /* Topology */
  .topo-bar {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    margin-bottom: 16px;
  }
  .legend { display: flex; gap: 16px; flex-wrap: wrap; }
  .legend .item { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--muted); }
  .legend .swatch { width: 22px; height: 13px; border-radius: 4px; flex: none; }
  .legend .swatch.linked { background: var(--accent-weak); border: 1.5px solid var(--accent); }
  .legend .swatch.indep  { background: var(--surface-2); border: 1.5px dashed var(--faint); }
  #mermaid-graph {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 20px; box-shadow: var(--shadow-sm);
    overflow: auto;
  }
  #mermaid-graph svg { max-width: 100%; height: auto; }
  #mermaid-graph pre {
    font-family: var(--mono); font-size: 12px; color: var(--fg-soft);
    margin: 0; white-space: pre-wrap;
  }

  .empty { color: var(--muted); font-style: italic; padding: 10px 0; font-size: 13px; }

  /* ---- Uncovered symbols drill-down (Spec detail) ---- */
  details.uncovered {
    margin-top: 16px; border: 1px solid color-mix(in srgb, var(--warn) 32%, var(--line));
    border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow-sm);
    overflow: hidden;
  }
  details.uncovered > summary {
    cursor: pointer; list-style: none; padding: 12px 15px;
    font-size: 12.5px; font-weight: 700; color: var(--warn);
    display: flex; align-items: center; gap: 9px; background: var(--warn-weak);
  }
  details.uncovered > summary::-webkit-details-marker { display: none; }
  details.uncovered > summary::before {
    content: "▸"; color: var(--warn); font-size: 11px; transition: transform .15s ease;
  }
  details.uncovered[open] > summary::before { transform: rotate(90deg); }
  details.uncovered > summary .hint { font-weight: 500; color: var(--muted); font-size: 11.5px; }
  .uncovered-body { padding: 6px 15px 14px; }
  ul.uncovered-list {
    list-style: none; margin: 8px 0 0; padding: 0; columns: 2; column-gap: 10px;
  }
  @media (max-width: 760px) { ul.uncovered-list { columns: 1; } }
  ul.uncovered-list li {
    break-inside: avoid; font-family: var(--mono); font-size: 11.5px;
    color: var(--fg-soft); padding: 5px 9px; border-radius: 7px;
    border-left: 2px solid var(--warn); background: var(--surface-2);
    margin-bottom: 5px; word-break: break-all;
  }
  .uncovered-more { font-size: 11.5px; color: var(--muted); margin-top: 8px; font-style: italic; }

  /* ---- Changes (git diff Spec impact) ---- */
  .chg-head {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px;
  }
  .chg-range {
    font-family: var(--mono); font-size: 13px; font-weight: 650;
    padding: 4px 12px; border-radius: 999px;
    background: var(--accent-weak); color: var(--accent-ink); border: 1px solid var(--accent-line);
  }
  .chg-range .sep { color: var(--muted); margin: 0 4px; }
  .ratio-bar {
    display: inline-flex; align-items: center; gap: 7px;
  }
  .ratio-bar .track {
    width: 70px; height: 6px; border-radius: 4px; overflow: hidden;
    background: color-mix(in srgb, var(--muted) 24%, transparent);
  }
  .ratio-bar .fill { height: 100%; border-radius: 4px; background: var(--accent); }
  .ratio-bar.high .fill { background: var(--ok); }
  .ratio-bar.mid  .fill { background: var(--warn); }
  .ratio-bar.low  .fill { background: var(--danger); }
  .ratio-bar .v { font-size: 12px; font-weight: 650; font-variant-numeric: tabular-nums; color: var(--fg-soft); }
  td .files-list { display: flex; flex-direction: column; gap: 3px; }
  td .files-list .f { font-family: var(--mono); font-size: 11px; color: var(--muted); word-break: break-all; }

  /* ---- Coverage trend (sparkline + snapshot bars) ---- */
  .trend-panel {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 16px 18px; box-shadow: var(--shadow-sm);
    margin: 4px 0 8px;
  }
  .trend-h { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
  .trend-h .t { font-size: 13px; font-weight: 650; color: var(--fg-soft); }
  .trend-h .sub { font-size: 11.5px; color: var(--muted); }
  .spark { display: block; width: 100%; max-width: 560px; height: 60px; }
  .spark .area { fill: var(--accent-weak); }
  .spark .line { fill: none; stroke: var(--accent); stroke-width: 2; }
  .spark .dot  { fill: var(--accent); }
  .trend-bars { display: flex; align-items: flex-end; gap: 6px; height: 80px; margin-top: 6px; }
  .trend-bars .col { display: flex; flex-direction: column; align-items: center; gap: 4px; flex: 1; min-width: 0; }
  .trend-bars .col .bar {
    width: 100%; max-width: 34px; border-radius: 4px 4px 0 0; background: var(--accent);
    min-height: 2px; transition: none;
  }
  .trend-bars .col .lbl { font-size: 9.5px; color: var(--faint); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .trend-meta {
    display: flex; gap: 18px; flex-wrap: wrap; margin-top: 12px;
    font-size: 12px; color: var(--muted);
  }
  .trend-meta b { color: var(--fg); font-variant-numeric: tabular-nums; }
  .trend-single {
    font-size: 12.5px; color: var(--muted); font-style: italic; margin-top: 8px;
  }
  .trend-table {
    width: 100%; max-width: 480px; border-collapse: collapse; margin-top: 8px;
    font-size: 12px;
  }
  .trend-table th, .trend-table td {
    text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line-soft);
  }
  .trend-table th { color: var(--muted); font-weight: 600; font-size: 11px; }
  .trend-table td b { font-variant-numeric: tabular-nums; }

  /* ---- Motion: opt-out ---- */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      transition: none !important;
      animation: none !important;
      scroll-behavior: auto !important;
    }
  }
  @media (prefers-reduced-motion: no-preference) {
    .panel.active { animation: fade .22s ease; }
    @keyframes fade { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
  }
</style>
</head>
<body>
<header class="app">
  <div class="brand">
    <span class="mark" aria-hidden="true">Spec</span>
    <h1>
      <span class="kicker">Specs Status</span>
      <span class="proj">__PROJECT__</span>
    </h1>
  </div>
  <div class="stats" id="stats" aria-label="Project totals"></div>
  <nav class="tabs" role="tablist" aria-label="Explorer views">
    <button role="tab" data-route="landing" aria-current="page">Overview</button>
    <button role="tab" data-route="specs">Specs<span class="pill" id="pill-spec"></span></button>
    <button role="tab" data-route="topology">Dependencies<span class="pill" id="pill-topo"></span></button>
    <button role="tab" data-route="endpoints">API<span class="pill" id="pill-ep"></span></button>
    <button role="tab" data-route="changes">Changes<span class="pill" id="pill-chg"></span></button>
    <button role="tab" data-route="gaps">Coverage gaps<span class="pill" id="pill-gap"></span></button>
  </nav>
</header>

<section class="panel active" data-panel="landing" role="tabpanel" aria-label="Overview">
  <main class="scroll pad" id="landing-main"></main>
</section>
<section class="panel" data-panel="specs" role="tabpanel" aria-label="Specs" hidden>
  <div class="split">
    <aside class="spine" aria-label="Spec spine">
      <div class="search">
        <label for="spec-filter" class="sr-only" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">Search specs</label>
        <input id="spec-filter" type="search" placeholder="Search specs…" autocomplete="off" spellcheck="false">
      </div>
      <div class="ds-filters" id="ds-filters" role="group" aria-label="Filter by development state"></div>
      <div class="list" id="specnav"></div>
    </aside>
    <main class="scroll pad" id="specmain">
      <section class="dash" id="dashboard" aria-label="Project status overview"></section>
      <div id="specdetail"><div class="empty">Select a spec to see what it is and where it stands.</div></div>
    </main>
  </div>
</section>
<section class="panel" data-panel="topology" role="tabpanel" aria-label="Dependencies" hidden>
  <main class="scroll pad">
    <p class="lead">How specs depend on one another. Each box is colour-coded by its development state.</p>
    <div class="topo-bar">
      <div class="legend" id="topo-legend" aria-hidden="false"></div>
      <span class="chip muted" id="topo-counts"></span>
    </div>
    <div class="mermaid" id="mermaid-graph"></div>
  </main>
</section>
<section class="panel" data-panel="endpoints" role="tabpanel" aria-label="Endpoints" hidden>
  <main class="scroll pad" id="epmain"></main>
</section>
<section class="panel" data-panel="changes" role="tabpanel" aria-label="Changes" hidden>
  <main class="scroll pad" id="chgmain"></main>
</section>
<section class="panel" data-panel="gaps" role="tabpanel" aria-label="Coverage gaps" hidden>
  <main class="scroll pad" id="gapmain"></main>
</section>

<script id="explorer-data" type="application/json">
__DATA__
</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById('explorer-data').textContent);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const el = id => document.getElementById(id);

// ---- Client-side router (/explorer, /explorer/endpoints, …) ----
// Served over HTTP: History API paths under meta.base_path (default /explorer).
// file:// fallback: hash routes (#/endpoints) so local opens still work.
const ROUTES = ['landing', 'specs', 'topology', 'endpoints', 'changes', 'gaps'];
const ROUTE_LABEL = {
  landing: 'Overview', specs: 'Specs', topology: 'Dependencies',
  endpoints: 'API', changes: 'Changes', gaps: 'Coverage gaps',
};
function routerBase() {
  const configured = (DATA.meta.base_path || '/explorer').replace(/\\/+$/, '');
  if (location.protocol === 'file:') return { mode: 'hash', base: configured };
  const idx = location.pathname.indexOf('/explorer');
  if (idx >= 0) return { mode: 'history', base: location.pathname.slice(0, idx + 9) };
  // python -m http.server at bundle root: / or /index.html — no /explorer prefix
  if (/^\\/(index\\.html)?$/.test(location.pathname)) {
    return { mode: 'hash', base: configured };
  }
  return { mode: 'history', base: configured };
}
const ROUTER = routerBase();
function routeFromPath(pathOrHash) {
  let p = String(pathOrHash || '');
  if (ROUTER.mode === 'hash') {
    p = p.replace(/^#\\/?/, '').replace(/^\\//, '');
    const base = ROUTER.base.replace(/^\\//, '').replace(/\\/+$/, '');
    if (!p || p === base) return 'landing';
    if (p.startsWith(base + '/')) p = p.slice(base.length + 1);
    const seg = (p.split('/')[0] || 'landing').toLowerCase();
    return ROUTES.includes(seg) ? seg : 'landing';
  }
  const base = ROUTER.base.replace(/\\/+$/, '');
  if (p.endsWith('/index.html')) p = p.replace(/\\/index\\.html$/, '');
  if (!p || p === base || p === base + '/') return 'landing';
  if (p.startsWith(base + '/')) p = p.slice(base.length + 1);
  else p = p.replace(/^\\/+/, '');
  const seg = (p.split('/')[0] || 'landing').toLowerCase();
  return ROUTES.includes(seg) ? seg : 'landing';
}
function hrefForRoute(route) {
  const base = ROUTER.base.replace(/\\/+$/, '');
  const suffix = route === 'landing' ? '' : '/' + route;
  if (ROUTER.mode === 'hash') return '#' + base + suffix;
  return base + suffix;
}
function currentRoute() {
  if (ROUTER.mode === 'hash') return routeFromPath(location.hash || hrefForRoute('landing'));
  return routeFromPath(location.pathname);
}
let activeRoute = currentRoute();
function navigateTo(route, replace) {
  if (!ROUTES.includes(route)) route = 'landing';
  activeRoute = route;
  const href = hrefForRoute(route);
  if (ROUTER.mode === 'hash') {
    if (replace) history.replaceState({ route }, '', href);
    else history.pushState({ route }, '', href);
  } else {
    if (replace) history.replaceState({ route }, '', href);
    else history.pushState({ route }, '', href);
  }
  showRoute(route);
}
function showRoute(route) {
  document.querySelectorAll('nav.tabs button').forEach(b => {
    const on = b.dataset.route === route;
    b.setAttribute('aria-current', on ? 'page' : 'false');
  });
  document.querySelectorAll('.panel').forEach(p => {
    const on = p.dataset.panel === route;
    p.classList.toggle('active', on);
    p.hidden = !on;
  });
  if (route === 'topology') renderTopology();
  if (route === 'specs' && DATA.specs.length && !activeSpec) {
    selectSpec(DATA.specs[0].id);
  }
  if (route === 'landing') buildLanding();
}
window.addEventListener('popstate', () => showRoute(currentRoute()));
window.addEventListener('hashchange', () => showRoute(currentRoute()));

// ---- Header stats + tab count pills ----
const counts = DATA.meta.counts;
el('stats').innerHTML = [
  ['specs', 'Specs'],
  ['symbols', 'Linked symbols'],
  ['endpoints', 'Endpoints'],
  ['files', 'Files'],
].map(([k, label]) =>
  `<div class="stat"><span class="n">${counts[k]}</span><span class="l">${label}</span></div>`
).join('');

const edgeCount = DATA.spec_topology.edges.length;
const CHANGES = DATA.changes || { base: null, head: null, files_changed: [], specs_touched: [] };
const TREND = DATA.trend || [];
el('pill-spec').textContent = DATA.specs.length;
el('pill-topo').textContent = DATA.spec_topology.nodes.length;
el('pill-ep').textContent = DATA.endpoints.length;
el('pill-chg').textContent = (CHANGES.specs_touched || []).length;
el('pill-gap').textContent =
  DATA.coverage.orphan_modules.length + DATA.coverage.orphan_endpoints.length;

// ---- dev_state vocabulary (PO-facing) ----
const DEV_STATES = ['not_started', 'in_progress', 'implemented', 'verified'];
const DS_LABEL = {
  not_started: 'Not started',
  in_progress: 'In progress',
  implemented: 'Implemented',
  verified: 'Verified',
};
const DS_BLURB = {
  not_started: 'No code is attributed to this spec yet.',
  in_progress: 'Code is being attributed; attribution confidence is still building.',
  implemented: 'Code implements this spec with high-confidence links.',
  verified: 'Backed by real test coverage — tests reach the implementing code (call-graph-derived) or are explicitly linked.',
};
const dsClass = s => 'ds-' + (DEV_STATES.includes(s) ? s : 'not_started');

// ---- Spec kind taxonomy (v0.20) ----
const KIND_LABEL = {
  functional_requirement: 'FR',
  non_functional_requirement: 'NFR',
  adr: 'ADR',
  design: 'Design',
  constraint: 'Constraint',
  epic: 'Epic',
  other: 'Other',
};
function kindChip(k) {
  const label = KIND_LABEL[k] || k || 'FR';
  return `<span class="chip kind muted" title="Spec kind">${esc(label)}</span>`;
}
function statePill(s, big) {
  const cls = dsClass(s);
  return `<span class="state-pill ${big ? 'big ' : ''}${cls}">` +
    `<span class="dot"></span>${esc(DS_LABEL[s] || s)}</span>`;
}
// Coverage = AVERAGE LINK CONFIDENCE of the Spec's code links (not test
// coverage, not call-graph reachability). Labelled precisely everywhere.
const COVERAGE_LABEL = 'link confidence';
function covPct(v) { return v == null ? null : Math.round(v * 100); }

// test_coverage_ratio = REAL test coverage (call-graph-derived + explicit
// test links), DISTINCT from link confidence above. coverage_source records
// HOW that coverage is known — the transparency a PO/stakeholder needs.
const TEST_COVERAGE_LABEL = 'test coverage';
const SOURCE_BADGE = {
  derived:  { label: 'auto-derived (call graph)', cls: 'info' },
  explicit: { label: 'explicit test links',       cls: 'accent' },
  both:     { label: 'derived + explicit',        cls: 'ok' },
  none:     { label: 'no test coverage',          cls: 'muted' },
};
function sourceBadge(src) {
  const b = SOURCE_BADGE[src] || SOURCE_BADGE.none;
  let html = `<span class="chip ${b.cls}" title="How test coverage is known">${esc(b.label)}</span>`;
  if (src === 'derived') {
    html += '<span class="chip warn" title="Covered via call-graph reachability only — no explicit test link">integration-only</span>';
  }
  return html;
}

// A declared status looks stale when evidence has clearly moved past it:
// dev_state is implemented/verified but the human still marks it draft/
// in-review/proposed (i.e. not an "advanced" declared status).
const ADVANCED_STATUS = new Set(['approved', 'done', 'implemented', 'verified', 'released', 'complete']);
function statusLooksStale(spec) {
  if (spec.dev_state !== 'implemented' && spec.dev_state !== 'verified') return false;
  const declared = String(spec.status || '').toLowerCase();
  return !ADVANCED_STATUS.has(declared);
}
const statusClass = s => 'st-' + String(s || 'draft').replace(/[^a-z_]/gi, '_').toLowerCase();

// ---- Project dashboard (PO reads this first) ----
function renderDashboard() {
  const d = DATA.dashboard;
  if (!d || !d.specs) {
    el('dashboard').innerHTML =
      '<div class="empty">No specs defined yet — see the Endpoints &amp; Coverage gaps tabs for what exists in code.</div>';
    return;
  }
  const c = d.dev_state_counts;
  const avgCov = d.avg_coverage == null ? '—' : Math.round(d.avg_coverage * 100) + '<span class="unit">%</span>';
  const avgTestCov = d.avg_test_coverage == null ? '—' : Math.round(d.avg_test_coverage * 100) + '<span class="unit">%</span>';
  const tiles = [
    { n: d.specs, k: 'Specs', cls: '' },
    { n: d.implemented_pct + '<span class="unit">%</span>', k: 'Have implementation', cls: 'accent' },
    { n: d.verified, k: 'Verified by tests', cls: d.verified > 0 ? 'good' : '' },
    { n: avgTestCov, k: 'Avg ' + TEST_COVERAGE_LABEL, cls: (d.avg_test_coverage || 0) > 0 ? 'good' : '' },
    { n: d.with_endpoints, k: 'Expose endpoints', cls: '' },
    { n: d.with_dependencies, k: 'Have dependencies', cls: '' },
    { n: avgCov, k: 'Avg ' + COVERAGE_LABEL, cls: '' },
  ];
  let h = '<div class="dash-tiles">' + tiles.map(t =>
    `<div class="dash-tile ${t.cls}"><div class="n">${t.n}</div><div class="k">${esc(t.k)}</div></div>`
  ).join('') + '</div>';

  // status breakdown bar
  const total = d.specs || 1;
  h += '<div class="breakdown"><div class="bar" role="img" aria-label="Development state breakdown">';
  DEV_STATES.forEach(s => {
    const n = c[s] || 0;
    if (n > 0) h += `<span class="seg-${s}" style="flex:${n}" title="${esc(DS_LABEL[s])}: ${n}"></span>`;
  });
  h += '</div><div class="keys">';
  DEV_STATES.forEach(s => {
    h += `<span class="item"><span class="sw ${s}"></span>${esc(DS_LABEL[s])} <b>${c[s] || 0}</b></span>`;
  });
  h += '</div></div>';
  el('dashboard').innerHTML = h;
}

// ---- dev_state filter ----
let dsFilter = 'all';  // 'all' or one of DEV_STATES
function renderDsFilters() {
  const box = el('ds-filters');
  if (!DATA.specs.length) { box.style.display = 'none'; return; }
  const counts = { all: DATA.specs.length };
  DEV_STATES.forEach(s => counts[s] = 0);
  DATA.specs.forEach(spec => { counts[spec.dev_state] = (counts[spec.dev_state] || 0) + 1; });
  const chips = [['all', 'All']].concat(
    DEV_STATES.filter(s => counts[s] > 0).map(s => [s, DS_LABEL[s]]));
  box.innerHTML = chips.map(([k, label]) =>
    `<button type="button" class="ds-chip" data-ds="${k}" aria-pressed="${k === dsFilter}">` +
    `<span class="sw ${k}"></span>${esc(label)}<span class="ct">${counts[k]}</span></button>`
  ).join('');
  box.querySelectorAll('.ds-chip').forEach(btn =>
    btn.addEventListener('click', () => {
      dsFilter = btn.getAttribute('data-ds');
      box.querySelectorAll('.ds-chip').forEach(b =>
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'));
      renderSpine(el('spec-filter').value);
    }));
}

// ---- Specs spine + detail ----
const nav = el('specnav');
const detail = el('specdetail');
const specmain = el('specmain');
let activeSpec = null;

function renderSpine(filter) {
  const q = (filter || '').trim().toLowerCase();
  nav.innerHTML = '';
  const matches = DATA.specs.filter(spec => {
    if (dsFilter !== 'all' && spec.dev_state !== dsFilter) return false;
    return !q || spec.id.toLowerCase().includes(q) || (spec.title || '').toLowerCase().includes(q) ||
      (spec.description || '').toLowerCase().includes(q) ||
      (KIND_LABEL[spec.kind] || spec.kind || '').toLowerCase().includes(q);
  });
  if (!DATA.specs.length) {
    nav.innerHTML = '<div class="none">No specs linked yet.<br>See the Endpoints &amp; Coverage gaps tabs.</div>';
    return;
  }
  if (!matches.length) {
    nav.innerHTML = '<div class="none">No specs match this filter.</div>';
    return;
  }
  matches.forEach(spec => {
    const b = document.createElement('button');
    b.className = 'spec ' + dsClass(spec.dev_state);
    b.type = 'button';
    b.setAttribute('aria-current', spec.id === activeSpec ? 'true' : 'false');
    const pct = covPct(spec.coverage);
    const summary = DS_BLURB[spec.dev_state] || '';
    b.innerHTML =
      `<div class="top"><span class="rid">${esc(spec.id)}</span>` +
      kindChip(spec.kind) +
      statePill(spec.dev_state, false) + '</div>' +
      `<div class="ti">${esc(spec.title)}</div>` +
      `<div class="sub">${esc(summary)}</div>` +
      (pct != null ? `<div class="covbar" title="${pct}% ${COVERAGE_LABEL}"><span style="width:${pct}%"></span></div>` : '');
    b.addEventListener('click', () => selectSpec(spec.id));
    nav.appendChild(b);
  });
}

function selectSpec(id) {
  activeSpec = id;
  nav.querySelectorAll('.spec').forEach(n => {
    const on = n.querySelector('.rid') && n.querySelector('.rid').textContent === id;
    n.setAttribute('aria-current', on ? 'true' : 'false');
  });
  const spec = DATA.specs.find(r => r.id === id);
  if (!spec) return;

  const pct = covPct(spec.coverage);
  const testPct = Math.round((spec.test_coverage_ratio || 0) * 100);
  const covSrc = spec.coverage_source || 'none';

  // --- Plain-language head: title + prominent dev_state ---
  let h = '<div class="detail-head">' +
    `<div class="eyebrow">${esc(spec.id)}</div>` +
    `<h2 class="title">${esc(spec.title)}</h2></div>`;

  h += '<div class="meta-row">' + statePill(spec.dev_state, true) +
    kindChip(spec.kind) +
    `<span class="chip status muted" title="Declared status (manually maintained)"><span class="dot ${statusClass(spec.status)}"></span>declared: ${esc(spec.status || 'none')}</span>` +
    (statusLooksStale(spec) ? '<span class="stale-flag" title="The declared status has not caught up with the code evidence">⚠ declared status not updated</span>' : '') +
    '</div>';

  // Description leads the content.
  h += spec.description
    ? `<p class="desc">${esc(spec.description)}</p>`
    : '<p class="desc"><span class="empty">No description provided for this spec.</span></p>';

  // --- How this state was derived (transparency) ---
  h += `<p class="derive-note ${dsClass(spec.dev_state)}">` +
    `<span class="lbl">${esc(DS_LABEL[spec.dev_state])}</span> — ${esc(DS_BLURB[spec.dev_state])} ` +
    'States are derived from <b>code evidence</b> (implementation links + their attribution confidence), not a manually-maintained field. ' +
    '“Verified” means <b>real test coverage</b> exists — a test reaches the implementing code (auto-derived from the call graph) or an explicit test↔spec link is present.</p>';

  // --- Two DISTINCT coverage meters: real test coverage + link confidence ---
  const testCovCls = testPct >= 70 ? 'high' : (testPct >= 30 ? 'mid' : 'low');
  const linkCovCls = pct == null ? '' : (pct >= 70 ? 'high' : (pct >= 30 ? 'mid' : 'low'));
  h += '<div class="cov-meters">' +
    // REAL test coverage (call-graph-derived + explicit) — the headline meter.
    '<div class="cov-meter">' +
      `<div class="cov-meter-h"><span class="cov-meter-l">${esc(TEST_COVERAGE_LABEL)}</span>` +
      sourceBadge(covSrc) + '</div>' +
      `<div class="cov ${testCovCls}" title="${spec.tested_symbols}/${spec.total_symbols} implementing symbols reached by tests">` +
      `<span class="track"><span class="fill" style="width:${testPct}%"></span></span>` +
      `<span class="v">${testPct}%</span></div>` +
      `<div class="cov-meter-hint">${spec.tested_symbols}/${spec.total_symbols} symbols reached by tests · auto-derived from the call graph, unioned with explicit test links</div>` +
    '</div>' +
    // LINK CONFIDENCE — separate signal, distinctly labelled.
    '<div class="cov-meter">' +
      `<div class="cov-meter-h"><span class="cov-meter-l">${esc(COVERAGE_LABEL)}</span></div>` +
      (pct == null
        ? '<div class="cov"><span class="track"><span class="fill" style="width:0%"></span></span><span class="v">—</span></div>'
        : `<div class="cov ${linkCovCls}"><span class="track"><span class="fill" style="width:${pct}%"></span></span><span class="v">${pct}%</span></div>`) +
      '<div class="cov-meter-hint">how confident the Spec↔code attribution links are — NOT test coverage</div>' +
    '</div>' +
    '</div>';

  // --- Uncovered symbols drill-down (the actionable gap) ---
  // Lists implementing symbols that no test reaches (call-graph) and that
  // carry no explicit test link. Shown only when there is a gap to act on.
  const uncovered = spec.uncovered_symbols || [];
  const uncoveredCount = spec.uncovered_symbols_count || 0;
  if (uncoveredCount > 0) {
    h += '<details class="uncovered"><summary>Uncovered symbols ' +
      `<span class="ct" style="font-family:var(--mono)">${uncoveredCount}</span>` +
      '<span class="hint">implementing code no test reaches — the gap to close</span></summary>' +
      '<div class="uncovered-body">';
    h += '<ul class="uncovered-list">' +
      uncovered.map(q => `<li>${esc(q)}</li>`).join('') + '</ul>';
    if (uncovered.length < uncoveredCount) {
      h += `<div class="uncovered-more">… and ${uncoveredCount - uncovered.length} more (list capped at ${uncovered.length}).</div>`;
    }
    h += '</div></details>';
  }

  // --- Metrics row ---
  h += '<div class="metrics">' +
    `<div class="metric"><div class="mv">${testPct}%</div>` +
      `<div class="ml">${esc(TEST_COVERAGE_LABEL)}</div><div class="hint">real coverage (derived + explicit)</div></div>` +
    `<div class="metric"><div class="mv">${pct == null ? '—' : pct + '%'}</div>` +
      `<div class="ml">${esc(COVERAGE_LABEL)}</div><div class="hint">avg confidence of code links</div></div>` +
    `<div class="metric"><div class="mv">${spec.endpoints.length}</div><div class="ml">Endpoints exposed</div></div>` +
    `<div class="metric"><div class="mv">${spec.depends_on.length}</div><div class="ml">Dependencies</div></div>` +
    `<div class="metric"><div class="mv">${spec.symbols.length}</div><div class="ml">Implementing symbols</div></div>` +
    '</div>';

  // --- Dependencies as clickable chips ---
  h += '<div class="sec"><h3 class="sec-h">Depends on' +
    `<span class="ct">${spec.depends_on.length}</span></h3>`;
  if (spec.depends_on.length) {
    h += '<div class="clusterbox">' +
      spec.depends_on.map(d =>
        `<button type="button" class="chip dep" data-goto="${esc(d)}">${esc(d)}</button>`
      ).join('') + '</div>';
  } else {
    h += '<div class="empty">No dependencies — this spec stands on its own.</div>';
  }
  h += '</div>';

  // --- Technical detail (collapsed by default) ---
  h += '<details class="tech"><summary>Technical detail ' +
    `<span class="hint">${spec.symbols.length} symbol${spec.symbols.length === 1 ? '' : 's'} · ${spec.endpoints.length} endpoint${spec.endpoints.length === 1 ? '' : 's'} (for developers)</span></summary>` +
    '<div class="tech-body">';

  h += '<div class="sec" style="margin-top:8px"><h3 class="sec-h">Implementing symbols' +
    `<span class="ct">${spec.symbols.length}</span></h3>`;
  if (spec.symbols.length) {
    h += '<div class="card"><table><thead><tr>' +
      '<th>Symbol</th><th>Signature</th><th>Location</th></tr></thead><tbody>';
    spec.symbols.forEach(s => {
      const file = esc(s.file), line = s.line;
      h += '<tr>' +
        `<td><span class="qname">${esc(s.qname)}</span></td>` +
        `<td>${s.signature ? `<span class="sig">${esc(s.signature)}</span>` : '<span class="empty">—</span>'}</td>` +
        `<td><span class="loc">${file}:<b>${line}</b></span></td></tr>`;
    });
    h += '</tbody></table></div>';
  } else {
    h += '<div class="empty">No linked symbols.</div>';
  }
  h += '</div>';

  h += '<div class="sec"><h3 class="sec-h">Owned endpoints' +
    `<span class="ct">${spec.endpoints.length}</span></h3>`;
  h += spec.endpoints.length
    ? '<div class="clusterbox">' +
      spec.endpoints.map(e => `<span class="chip mono accent">${esc(e)}</span>`).join('') + '</div>'
    : '<div class="empty">None.</div>';
  h += '</div></div></details>';

  detail.innerHTML = h;
  specmain.scrollTop = 0;
  detail.querySelectorAll('[data-goto]').forEach(btn =>
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-goto');
      if (DATA.specs.some(r => r.id === target)) {
        el('spec-filter').value = '';
        dsFilter = 'all';
        renderDsFilters();
        renderSpine('');
        selectSpec(target);
      }
    }));
}

renderDashboard();
renderDsFilters();
el('spec-filter').addEventListener('input', e => renderSpine(e.target.value));
renderSpine('');

// ---- Landing (/explorer) ----
function buildLanding() {
  const box = el('landing-main');
  if (!box) return;
  const d = DATA.dashboard || {};
  const c = counts;
  let h = '<div class="landing-hero">' +
    `<h2>${esc(DATA.meta.project)}</h2>` +
    '<p class="sub">Live map of specs, API surface, dependencies and coverage — ' +
    'auto-generated from the indexed codebase.</p></div>';
  h += '<div id="landing-dash"></div>';
  if (TREND.length) h += buildTrendOverview();
  h += '<div class="nav-cards">';
  const cards = [
    ['specs', 'Specs', 'Browse Specs, implementation evidence and test coverage.', d.specs || c.specs],
    ['endpoints', 'API', 'Swagger-style view of tools, routes and MCP entry points.', c.endpoints],
    ['topology', 'Dependencies', 'How specs depend on each other.', DATA.spec_topology.nodes.length],
    ['changes', 'Changes', 'Git diff impact on specs in the current range.', (CHANGES.specs_touched || []).length],
    ['gaps', 'Coverage gaps', 'Orphan modules and endpoints without a Spec link.',
      DATA.coverage.orphan_modules.length + DATA.coverage.orphan_endpoints.length],
  ];
  cards.forEach(([route, title, desc, n]) => {
    h += `<a class="nav-card" href="${esc(hrefForRoute(route))}" data-goto-route="${route}">` +
      `<div class="nc-title">${esc(title)}</div>` +
      `<p class="nc-desc">${esc(desc)}</p>` +
      `<span class="nc-count">${n}</span></a>`;
  });
  h += '</div>';
  box.innerHTML = h;
  // Reuse the dashboard tiles inside the landing hero area.
  const dashSlot = el('landing-dash');
  renderDashboard();
  if (dashSlot) dashSlot.innerHTML = el('dashboard').innerHTML;
  box.querySelectorAll('[data-goto-route]').forEach(a => {
    a.addEventListener('click', ev => {
      ev.preventDefault();
      navigateTo(a.getAttribute('data-goto-route'));
    });
  });
}

// ---- Dependencies graph (Mermaid, themed, coloured by dev_state) ----
const safeId = s => s.replace(/[^A-Za-z0-9_]/g, '_');
// Sanitize a node label for Mermaid v10's quoted-string ["..."] syntax.
// Spec titles routinely contain &, <, >, " which break the flowchart parser
// ("Syntax error in text"). HTML-entity-encode exactly those four chars
// (& FIRST so it doesn't double-encode the entities we add). Mermaid renders
// the entities back as glyphs, so the label stays readable. Parentheses, ↔
// and / are safe INSIDE a quoted string and pass through unchanged.
function mermaidLabel(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function buildMermaid() {
  const t = DATA.spec_topology;
  let src = 'graph TD\\n';
  if (!t.nodes.length) { return src + '  empty["No specs indexed"]\\n'; }
  t.nodes.forEach(n => {
    const nid = safeId(n.id);
    const label = mermaidLabel(n.id + ': ' + (n.title || ''));
    const ds = DEV_STATES.includes(n.dev_state) ? n.dev_state : 'not_started';
    src += `  ${nid}["${label}"]\\n`;
    src += `  class ${nid} ds_${ds}\\n`;
  });
  t.edges.forEach(e => {
    // Quote + sanitize the edge label too — kinds are a controlled vocab
    // today, but a quoted label keeps the parser safe if that ever changes.
    src += `  ${safeId(e.from)} -->|"${mermaidLabel(e.kind || 'requires')}"| ${safeId(e.to)}\\n`;
  });
  // classDef per dev_state — RESOLVED colours, not CSS var() references.
  // Mermaid v10's classDef parser rejects `fill:var(--x)` ("Parse error …
  // got '(-'"), which surfaced as the "Syntax error in text" on this tab.
  // We resolve each palette var to a concrete colour at build time (the
  // page is already themed light/dark, so this still tracks the palette)
  // and emit literal values the parser accepts. Falls back to safe hexes
  // if a var is empty (e.g. computed-style unavailable).
  const C = (name, fallback) => {
    const v = cssVar(name);
    return v && !/var\\(/.test(v) ? v : fallback;
  };
  const ink = C('--fg', '#1c2130'), inkSoft = C('--muted', '#6a7488');
  const defs = [
    ['ds_not_started', C('--surface-2', '#f1f3f8'), C('--faint', '#9aa3b5'), '1.2px', inkSoft, ',stroke-dasharray:4 3'],
    ['ds_in_progress', C('--info-weak', '#e4eef8'), C('--info', '#2f6fb0'), '1.5px', ink, ''],
    ['ds_implemented', C('--accent-weak', '#ece9fb'), C('--accent', '#5848d6'), '1.5px', ink, ''],
    ['ds_verified', C('--ok-weak', '#e3f4ec'), C('--ok', '#1f8a5b'), '1.8px', ink, ''],
  ];
  defs.forEach(([cls, fill, stroke, sw, color, extra]) => {
    src += `  classDef ${cls} fill:${fill},stroke:${stroke},stroke-width:${sw},color:${color}${extra};\\n`;
  });
  return src;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
let mermaidRendered = false;
// dev_state legend for the Dependencies tab.
function renderTopoLegend() {
  const leg = el('topo-legend');
  if (!leg) return;
  leg.innerHTML = DEV_STATES.map(s =>
    `<span class="item"><span class="swatch ${s}" style="background:var(--legend-${s})"></span> ${esc(DS_LABEL[s])}</span>`
  ).join('');
  // colour the swatches from the live palette
  leg.querySelectorAll('.swatch').forEach((sw, i) => {
    const map = { not_started: '--faint', in_progress: '--info', implemented: '--accent', verified: '--ok' };
    sw.style.background = cssVar(map[DEV_STATES[i]]);
    sw.style.border = 'none';
  });
}
async function renderTopology() {
  const t = DATA.spec_topology;
  const linkedCount = new Set();
  t.edges.forEach(e => { linkedCount.add(e.from); linkedCount.add(e.to); });
  const indep = t.nodes.length - linkedCount.size;
  el('topo-counts').textContent =
    `${t.nodes.length} specs · ${edgeCount} dependency edges · ${indep} independent`;
  renderTopoLegend();
  if (mermaidRendered) return;
  mermaidRendered = true;

  // dev_state classDef colours are resolved to literal values inside
  // buildMermaid() (Mermaid v10 rejects var() in classDef) — no CSS-var
  // bridging needed here. themeVariables below still read live palette vars.
  try {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      fontFamily: cssVar('--font') || 'system-ui, sans-serif',
      flowchart: { curve: 'basis', nodeSpacing: 42, rankSpacing: 52, padding: 10 },
      themeVariables: {
        background: cssVar('--surface'),
        primaryColor: cssVar('--accent-weak'),
        primaryBorderColor: cssVar('--accent'),
        primaryTextColor: cssVar('--fg'),
        lineColor: cssVar('--accent'),
        edgeLabelBackground: cssVar('--surface'),
        tertiaryColor: cssVar('--surface-2'),
        fontSize: '13px',
      },
    });
    const { svg } = await mermaid.render('specgraph', buildMermaid());
    el('mermaid-graph').innerHTML = svg;
  } catch (err) {
    // Offline / CDN unavailable: show readable graph source, never blank.
    el('mermaid-graph').innerHTML =
      '<pre>' + esc(buildMermaid()) + '</pre>';
  }
}

// ---- API surface (Swagger UI-style) ----
const EP_KIND_ORDER = ['tool', 'resource', 'prompt', 'other'];
const EP_KIND_LABEL = {
  tool: 'Tools', resource: 'Resources', prompt: 'Prompts', other: 'HTTP & other',
};
const EP_KIND_DESC = {
  tool: 'MCP tools and framework route handlers',
  resource: 'MCP resources',
  prompt: 'MCP prompts',
  other: 'HTTP routes and unclassified entry points',
};

function shortName(qname) {
  const s = String(qname || '');
  const i = s.lastIndexOf('.');
  return i === -1 ? s : s.slice(i + 1);
}

function sigArgs(signature) {
  const s = String(signature || '');
  const open = s.indexOf('('), close = s.lastIndexOf(')');
  if (open === -1 || close === -1 || close <= open) return [];
  const inner = s.slice(open + 1, close).trim();
  if (!inner) return [];
  return inner.split(',').map(p => {
    let name = p.trim();
    if (!name || name === '*' || name === '/') return '';
    name = name.replace(/^\\*+/, '');
    name = name.split('=')[0];
    name = name.split(':')[0];
    return name.trim();
  }).filter(a => a && a !== 'self' && a !== 'cls');
}

function sigArgTypes(signature) {
  const s = String(signature || '');
  const open = s.indexOf('('), close = s.lastIndexOf(')');
  if (open === -1 || close === -1 || close <= open) return [];
  const inner = s.slice(open + 1, close).trim();
  if (!inner) return [];
  return inner.split(',').map(p => {
    let part = p.trim();
    if (!part || part === '*' || part === '/') return null;
    part = part.replace(/^\\*+/, '');
    const name = part.split('=')[0].split(':')[0].trim();
    if (!name || name === 'self' || name === 'cls') return null;
    const ann = part.includes(':') ? part.split(':').slice(1).join(':').split('=')[0].trim() : 'any';
    return { name, type: ann || 'any' };
  }).filter(Boolean);
}

function callShape(ep) {
  const name = shortName(ep.handler);
  const args = sigArgs(ep.signature);
  if (ep.kind === 'tool') {
    const argObj = args.map(a => `    "${a}": null`).join(',\\n');
    return '{\\n  "tool": "' + name + '",\\n  "arguments": {' +
      (argObj ? '\\n' + argObj + '\\n  ' : '') + '}\\n}';
  }
  return name + '(' + args.join(', ') + ')';
}

function opMethodClass(ep) {
  if (ep.method) return String(ep.method).toLowerCase();
  return ep.kind || 'other';
}

function opDisplayPath(ep) {
  if (ep.path) return ep.path;
  return shortName(ep.handler);
}

const HTTP_TRY_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']);

function isHttpTryIt(ep) {
  if (!ep.method || !ep.path) return false;
  return HTTP_TRY_METHODS.has(String(ep.method).toUpperCase());
}

function defaultApiBaseUrl() {
  if (location.protocol === 'file:') return 'http://127.0.0.1:8000';
  return location.origin || 'http://127.0.0.1:8000';
}

function joinBasePath(base, path) {
  const b = String(base || '').replace(/\\/+$/, '');
  const p = String(path || '');
  if (!p.startsWith('/')) return b + '/' + p;
  return b + p;
}

function buildEndpoints() {
  const eps = DATA.endpoints || [];
  const fixtures = DATA.fixtures || [];
  const groups = {};
  eps.forEach(ep => { (groups[ep.kind] = groups[ep.kind] || []).push(ep); });
  const keys = Object.keys(groups).sort(
    (a, b) => EP_KIND_ORDER.indexOf(a) - EP_KIND_ORDER.indexOf(b));

  let h = '<p class="ep-note">Static spec — <b>expand an operation</b> to see parameters. ' +
    'MCP tools: copy a call shape. HTTP routes (GET/POST/PUT/PATCH/DELETE): use <b>Try it</b> ' +
    'to call your API (requires CORS or same origin).</p>';
  h += '<div class="swagger-toolbar">' +
    '<input type="search" id="ep-filter" placeholder="Filter by name, path or handler…" autocomplete="off" spellcheck="false">' +
    '<div class="base-url-wrap"><label for="ep-base-url">Base URL</label>' +
    `<input type="url" id="ep-base-url" placeholder="http://127.0.0.1:8000" value="${esc(defaultApiBaseUrl())}"></div>` +
    `<span class="chip muted">${eps.length} operation${eps.length === 1 ? '' : 's'}</span></div>`;
  h += '<div id="swagger-root">';

  if (!keys.length) {
    h += '<div class="empty">No API entry points found.</div>';
  }
  keys.forEach(k => {
    const rows = groups[k];
    const label = EP_KIND_LABEL[k] || k;
    h += `<section class="swagger-tag" data-tag="${esc(k)}">` +
      `<div class="swagger-tag-h"><span class="name">${esc(label)}</span>` +
      `<span class="ct">${rows.length}</span>` +
      `<span class="desc">${esc(EP_KIND_DESC[k] || '')}</span></div>` +
      '<div class="swagger-ops">';
    rows.forEach((ep, i) => {
      const mcls = opMethodClass(ep);
      const path = opDisplayPath(ep);
      const args = sigArgTypes(ep.signature);
      const shape = callShape(ep);
      const cid = `cc-${k}-${i}`;
      const specs = ep.spec_ids.length
        ? ep.spec_ids.map(r => `<span class="chip accent" style="font-size:11px">${esc(r)}</span>`).join(' ')
        : '<span class="chip muted">unlinked</span>';
      const searchHay = [ep.handler, path, ep.method, ep.kind, shortName(ep.handler)].join(' ').toLowerCase();
      h += `<details class="swagger-op" data-search="${esc(searchHay)}">` +
        '<summary class="swagger-op-summary">' +
        `<span class="op-method ${esc(mcls)}">${esc(ep.method || ep.kind || 'other')}</span>` +
        '<span class="op-main">' +
        `<span class="op-path">${esc(path)}</span>` +
        `<span class="op-sub">${esc(ep.handler)}</span></span>` +
        '<span class="op-chevron" aria-hidden="true">▸</span></summary>' +
        '<div class="swagger-op-body">' +
        '<div class="op-section"><div class="op-section-h">Handler</div>' +
        `<code class="mono">${esc(ep.handler)}</code></div>`;
      if (ep.signature) {
        h += '<div class="op-section"><div class="op-section-h">Signature</div>' +
          `<code class="mono sig">${esc(ep.signature)}</code></div>`;
      }
      if (args.length) {
        h += '<div class="op-section"><div class="op-section-h">Parameters</div>' +
          '<div class="card"><table class="op-params"><thead><tr><th>Name</th><th>Type</th><th>Description</th></tr></thead><tbody>';
        args.forEach(a => {
          h += `<tr><td>${esc(a.name)}</td><td>${esc(a.type)}</td><td class="muted">—</td></tr>`;
        });
        h += '</tbody></table></div></div>';
      }
      const mcpKinds = new Set(['tool', 'resource', 'prompt']);
      if (mcpKinds.has(ep.kind)) {
        h += '<div class="op-section"><div class="op-section-h">Try it out (MCP)</div>' +
          '<div class="op-try">' +
          `<button type="button" class="copy-call" data-copy="${cid}">Copy call</button>` +
          `<pre class="call-shape" id="${cid}">${esc(shape)}</pre></div></div>';
      } else if (isHttpTryIt(ep)) {
        const eid = `http-${k}-${i}`;
        const method = String(ep.method).toUpperCase();
        h += '<div class="op-section"><div class="op-section-h">Try it (HTTP)</div>' +
          '<div class="http-try">' +
          `<p class="http-cors-note">Calls <code class="mono">${esc(method)} ${esc(path)}</code> against the base URL above. ` +
          'Cross-origin requests fail unless your API sends CORS headers; same-origin works when Explorer is mounted on the app.</p>' +
          `<div class="http-try-row">` +
          `<button type="button" class="http-exec" data-http-exec="${eid}" data-method="${esc(method)}" data-path="${esc(path)}">Execute</button>` +
          `<span class="chip muted">${esc(method)}</span></div>` +
          `<pre class="http-response" id="${eid}" hidden aria-live="polite"></pre></div></div>`;
      }
      h += '<div class="op-section"><div class="op-section-h">Specs</div>' +
        `<div class="clusterbox">${specs}</div></div>` +
        '</div></details>';
    });
    h += '</div></section>';
  });
  h += '</div>';

  if (fixtures.length) {
    h += '<details class="tech ep-fixtures"><summary>Test fixtures ' +
      `<span class="hint">${fixtures.length} pytest fixture${fixtures.length === 1 ? '' : 's'} — not API surface</span></summary>` +
      '<div class="tech-body"><div class="swagger-ops">';
    fixtures.forEach(fx => {
      h += `<details class="swagger-op"><summary class="swagger-op-summary">` +
        '<span class="op-method other">FIXTURE</span><span class="op-main">' +
        `<span class="op-path">${esc(shortName(fx.handler))}</span>` +
        `<span class="op-sub">${esc(fx.handler)}</span></span>` +
        '<span class="op-chevron">▸</span></summary>' +
        '<div class="swagger-op-body">' +
        (fx.signature ? `<code class="mono sig">${esc(fx.signature)}</code>` : '<span class="empty">—</span>') +
        '</div></details>';
    });
    h += '</div></div></details>';
  }

  el('epmain').innerHTML = h;

  const filter = el('ep-filter');
  if (filter) {
    filter.addEventListener('input', () => {
      const q = filter.value.trim().toLowerCase();
      el('epmain').querySelectorAll('details.swagger-op[data-search]').forEach(op => {
        const hay = op.getAttribute('data-search') || '';
        op.style.display = !q || hay.includes(q) ? '' : 'none';
      });
      el('epmain').querySelectorAll('.swagger-tag').forEach(tag => {
        const visible = [...tag.querySelectorAll('details.swagger-op[data-search]')]
          .some(op => op.style.display !== 'none');
        tag.style.display = visible ? '' : 'none';
      });
    });
  }

  el('epmain').querySelectorAll('.copy-call').forEach(btn =>
    btn.addEventListener('click', () => {
      const pre = el(btn.getAttribute('data-copy'));
      const text = pre ? pre.textContent : '';
      const done = () => {
        const old = btn.textContent;
        btn.textContent = 'Copied';
        btn.classList.add('copied');
        setTimeout(() => { btn.textContent = old; btn.classList.remove('copied'); }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, () => {});
      }
    }));

  el('epmain').querySelectorAll('.http-exec').forEach(btn =>
    btn.addEventListener('click', async () => {
      const rid = btn.getAttribute('data-http-exec');
      const pre = rid ? el(rid) : null;
      const method = (btn.getAttribute('data-method') || 'GET').toUpperCase();
      const path = btn.getAttribute('data-path') || '/';
      const baseInput = el('ep-base-url');
      const base = baseInput ? baseInput.value.trim() : defaultApiBaseUrl();
      const url = joinBasePath(base, path);
      btn.disabled = true;
      if (pre) {
        pre.hidden = false;
        pre.className = 'http-response';
        pre.textContent = `${method} ${url}\\n…`;
      }
      try {
        const opts = { method, headers: { Accept: 'application/json, text/plain, */*' } };
        if (method !== 'GET' && method !== 'HEAD') {
          opts.headers['Content-Type'] = 'application/json';
          opts.body = '{}';
        }
        const res = await fetch(url, opts);
        const ct = res.headers.get('content-type') || '';
        let body = '';
        try {
          body = ct.includes('json') ? JSON.stringify(await res.json(), null, 2) : await res.text();
        } catch (_) {
          body = '(could not read response body)';
        }
        const snippet = body.length > 4000 ? body.slice(0, 4000) + '\\n… (truncated)' : body;
        if (pre) {
          pre.className = 'http-response ' + (res.ok ? 'ok' : 'err');
          pre.textContent = `${method} ${url}\\nHTTP ${res.status} ${res.statusText}\\n\\n${snippet}`;
        }
      } catch (err) {
        if (pre) {
          pre.className = 'http-response err';
          const msg = err && err.message ? err.message : String(err);
          pre.textContent = `${method} ${url}\\nFailed: ${msg}\\n\\nTip: CORS blocks browser calls from another origin — mount Explorer on the same app or enable CORS on the API.`;
        }
      } finally {
        btn.disabled = false;
      }
    }));
}

// ---- Gaps ----
const TOTAL_LABELS = {
  modules_without_spec: 'Modules without Spec',
  modules_implicitly_covered: 'Implicitly covered',
  modules_truly_orphan: 'Truly orphan',
  modules_unsupported_language: 'Unsupported language',
  specs_without_implementation: 'Specs w/o impl',
  specs_low_confidence: 'Specs low confidence',
  specs_with_test_coverage: 'Specs w/ tests',
};
function buildGaps() {
  const g = DATA.coverage;
  let h = '<p class="lead">Code not yet attributed to any spec. ' +
    'Orphan modules and endpoints are candidates for a new spec link — ' +
    'they represent functionality the spec map does not yet account for.</p>';

  // KPI row from totals
  h += '<div class="kpis">';
  Object.entries(g.totals).forEach(([k, v]) => {
    const label = TOTAL_LABELS[k] || k.replace(/_/g, ' ');
    let cls = '';
    if (k === 'modules_truly_orphan' || k === 'modules_without_spec') cls = ' flag';
    if (k === 'specs_with_test_coverage' && v > 0) cls = ' good';
    if (k === 'specs_without_implementation' && v > 0) cls = ' flag';
    h += `<div class="kpi${cls}"><div class="n">${esc(v)}</div><div class="k">${esc(label)}</div></div>`;
  });
  h += '</div>';

  h += '<div class="sec"><h3 class="sec-h">Orphan modules' +
    `<span class="ct">${g.orphan_modules.length}</span></h3>`;
  h += g.orphan_modules.length
    ? '<ul class="orphan-list">' +
      g.orphan_modules.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>'
    : '<div class="empty">None — every module is reachable from a spec.</div>';
  h += '</div>';

  h += '<div class="sec"><h3 class="sec-h">Orphan endpoints' +
    `<span class="ct">${g.orphan_endpoints.length}</span></h3>`;
  h += g.orphan_endpoints.length
    ? '<ul class="orphan-list">' +
      g.orphan_endpoints.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>'
    : '<div class="empty">None — every endpoint is linked to a spec.</div>';
  h += '</div>';

  el('gapmain').innerHTML = h;
}

// ---- Coverage trend (sparkline over recorded audit snapshots) ----
// Renders the chronological avg_test_coverage series as a tiny SVG
// sparkline + a per-snapshot bar strip. Degrades gracefully: 0 snapshots ->
// a note; 1 snapshot -> a single bar + "history starts accumulating".
function ratioCls(pct) { return pct >= 70 ? 'high' : (pct >= 30 ? 'mid' : 'low'); }
function shortTs(ts) {
  // ISO-ish ts -> compact "MM-DD HH:MM" for the bar label, best-effort.
  const s = String(ts || '');
  const m = s.match(/(\\d{4})-(\\d{2})-(\\d{2})[T ](\\d{2}):(\\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : s.slice(0, 16);
}
// Compact trend block for the Overview landing (sparkline + last 3 snapshots).
function buildTrendOverview() {
  const series = TREND;
  if (!series.length) return '';
  const pts = series.map(s => (s.avg_test_coverage == null ? 0 : s.avg_test_coverage));
  const last = series[series.length - 1];
  const lastPct = Math.round((last.avg_test_coverage == null ? 0 : last.avg_test_coverage) * 100);
  let h = '<div class="trend-panel"><div class="trend-h">' +
    '<span class="t">Coverage trend</span>' +
    `<span class="sub">${series.length} snapshot${series.length === 1 ? '' : 's'} · avg ${esc(TEST_COVERAGE_LABEL)}</span></div>`;
  if (series.length >= 2) {
    const W = 100, H = 28, n = pts.length;
    const xs = pts.map((_, i) => (n === 1 ? 0 : (i / (n - 1)) * W));
    const ys = pts.map(v => H - v * H);
    const linePts = xs.map((x, i) => `${x.toFixed(2)},${ys[i].toFixed(2)}`).join(' ');
    const areaPts = `0,${H} ` + linePts + ` ${W},${H}`;
    h += `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Coverage trend over ${n} snapshots">` +
      `<polygon class="area" points="${areaPts}"></polygon>` +
      `<polyline class="line" points="${linePts}"></polyline>` +
      `<circle class="dot" cx="${xs[n - 1].toFixed(2)}" cy="${ys[n - 1].toFixed(2)}" r="1.8"></circle></svg>`;
  }
  const tail = series.slice(-3);
  h += '<table class="trend-table"><thead><tr>' +
    '<th>When</th><th>Avg test coverage</th><th>Verified Specs</th></tr></thead><tbody>';
  tail.forEach(s => {
    const pct = Math.round((s.avg_test_coverage == null ? 0 : s.avg_test_coverage) * 100);
    h += '<tr>' +
      `<td>${esc(shortTs(s.ts))}</td>` +
      `<td><b>${pct}%</b></td>` +
      `<td>${esc(s.verified_count)}</td></tr>`;
  });
  h += '</tbody></table>';
  h += '<div class="trend-meta">' +
    `<span>Latest: <b>${lastPct}%</b> avg ${esc(TEST_COVERAGE_LABEL)}</span>` +
    `<span>Verified Specs: <b>${last.verified_count}</b></span></div>';
  h += '</div>';
  return h;
}

function buildTrend() {
  const series = TREND;
  if (!series.length) {
    return '<div class="trend-panel"><div class="trend-h">' +
      '<span class="t">Coverage trend</span></div>' +
      '<div class="empty">No coverage snapshots recorded yet — run an audit to start the history.</div></div>';
  }
  // y values in [0,1]; null avg (no Specs at that snapshot) treated as 0.
  const pts = series.map(s => (s.avg_test_coverage == null ? 0 : s.avg_test_coverage));
  const last = series[series.length - 1];
  const lastPct = Math.round((last.avg_test_coverage == null ? 0 : last.avg_test_coverage) * 100);

  let h = '<div class="trend-panel"><div class="trend-h">' +
    '<span class="t">Coverage trend</span>' +
    `<span class="sub">${series.length} snapshot${series.length === 1 ? '' : 's'} · avg ${esc(TEST_COVERAGE_LABEL)}</span></div>`;

  if (series.length === 1) {
    // Single snapshot: one bar, friendly note, no sparkline (no line to draw).
    h += '<div class="trend-bars"><div class="col">' +
      `<div class="bar" style="height:${Math.max(2, lastPct)}%"></div>` +
      `<div class="lbl">${esc(shortTs(last.ts))}</div></div></div>`;
    h += '<div class="trend-single">History starts accumulating — one snapshot so far ' +
      `(${lastPct}% avg ${esc(TEST_COVERAGE_LABEL)}, ${last.verified_count} verified). ` +
      'Each audit adds a point.</div>';
    h += '</div>';
    return h;
  }

  // Sparkline: map points across a 0..W viewBox; W tracks point count.
  const W = 100, H = 30, n = pts.length;
  const xs = pts.map((_, i) => (n === 1 ? 0 : (i / (n - 1)) * W));
  const ys = pts.map(v => H - v * H);
  const linePts = xs.map((x, i) => `${x.toFixed(2)},${ys[i].toFixed(2)}`).join(' ');
  const areaPts = `0,${H} ` + linePts + ` ${W},${H}`;
  h += `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Coverage trend over ${n} snapshots">` +
    `<polygon class="area" points="${areaPts}"></polygon>` +
    `<polyline class="line" points="${linePts}"></polyline>` +
    `<circle class="dot" cx="${xs[n - 1].toFixed(2)}" cy="${ys[n - 1].toFixed(2)}" r="1.8"></circle></svg>`;

  // Per-snapshot bar strip (cap the rendered bars to keep it compact).
  const MAX_BARS = 24;
  const shown = series.slice(-MAX_BARS);
  h += '<div class="trend-bars">';
  shown.forEach(s => {
    const pct = Math.round((s.avg_test_coverage == null ? 0 : s.avg_test_coverage) * 100);
    h += '<div class="col" title="' +
      `${esc(shortTs(s.ts))} · ${pct}% avg · ${s.verified_count} verified">` +
      `<div class="bar" style="height:${Math.max(2, pct)}%"></div>` +
      `<div class="lbl">${esc(shortTs(s.ts))}</div></div>`;
  });
  h += '</div>';

  const first = series[0];
  const firstPct = Math.round((first.avg_test_coverage == null ? 0 : first.avg_test_coverage) * 100);
  const delta = lastPct - firstPct;
  const deltaStr = (delta >= 0 ? '+' : '') + delta + '%';
  h += '<div class="trend-meta">' +
    `<span>Latest: <b>${lastPct}%</b> avg ${esc(TEST_COVERAGE_LABEL)}</span>` +
    `<span>Verified Specs: <b>${last.verified_count}</b></span>` +
    `<span>Since first snapshot: <b>${esc(deltaStr)}</b></span></div>`;
  h += '</div>';
  return h;
}

// ---- Changes (Spec-centric git diff impact for the resolved range) ----
function buildChanges() {
  const c = CHANGES;
  const touched = c.specs_touched || [];
  const filesChanged = c.files_changed || [];
  const hasRange = c.base != null && c.head != null;

  // The coverage trend leads (movement over time), then this range's impact.
  let h = buildTrend();

  h += '<div class="sec"><h3 class="sec-h">Changes in range' +
    `<span class="ct">${touched.length}</span></h3>`;

  if (!hasRange) {
    h += '<div class="empty">No git range available — this workspace is not a git ' +
      'repository (or has no history), so there are no changes to attribute to specs.</div>';
    h += '</div>';
    el('chgmain').innerHTML = h;
    return;
  }

  h += '<div class="chg-head">' +
    `<span class="chg-range">${esc(c.base)}<span class="sep">..</span>${esc(c.head)}</span>` +
    `<span class="chip muted">${filesChanged.length} file${filesChanged.length === 1 ? '' : 's'} changed</span>` +
    `<span class="chip muted">${touched.length} spec${touched.length === 1 ? '' : 's'} touched</span></div>`;

  if (!filesChanged.length) {
    h += '<div class="empty">No changes in this range — base and head point to the same code.</div>';
  } else if (!touched.length) {
    h += '<div class="empty">Files changed, but none of them map to a tracked spec ' +
      '(no Spec links reach the changed code). See the Coverage gaps tab for unattributed code.</div>';
  } else {
    h += '<div class="card"><table><thead><tr>' +
      '<th>Spec</th><th>Title</th><th>Changed files</th><th>Test coverage</th></tr></thead><tbody>';
    touched.forEach(r => {
      const pct = Math.round((r.test_coverage_ratio || 0) * 100);
      const cls = ratioCls(pct);
      const files = (r.files || []).length
        ? '<div class="files-list">' + r.files.map(f => `<span class="f">${esc(f)}</span>`).join('') + '</div>'
        : '<span class="empty">—</span>';
      h += '<tr>' +
        `<td><span class="qname">${esc(r.spec_id)}</span></td>` +
        `<td>${esc(r.title || '')}</td>` +
        `<td>${files}</td>` +
        `<td><span class="ratio-bar ${cls}"><span class="track"><span class="fill" style="width:${pct}%"></span></span>` +
          `<span class="v">${pct}%</span></span></td></tr>`;
    });
    h += '</tbody></table></div>';
  }
  h += '</div>';
  el('chgmain').innerHTML = h;
}

// ---- Navigation (routes under /explorer) ----
document.querySelectorAll('nav.tabs button').forEach(btn => {
  btn.addEventListener('click', () => navigateTo(btn.dataset.route));
});

buildEndpoints();
buildChanges();
buildGaps();
navigateTo(activeRoute, true);
</script>
</body>
</html>
"""
