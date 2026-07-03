"""export_explorer: static Spec Explorer bundle generation.

Asserts the tool writes data.json + index.html, the JSON schema is
complete, the 0-Spec case still populates endpoints + coverage, and two
runs are byte-identical modulo meta.generated_at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp

_TOP_KEYS = {
    "meta", "dashboard", "specs", "spec_topology", "endpoints",
    "fixtures", "coverage", "trend", "changes",
}


def _write_flask_app(workspace: Path) -> None:
    """A tiny Flask app (decorated endpoints) + a plain helper module."""
    pkg = workspace / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "routes.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/login', methods=['POST'])\n"
        "def login(user, password):\n"
        '    """Login handler.\n\n    @spec:SPEC-001\n    """\n'
        "    return verify(user, password)\n"
        "\n"
        "@app.route('/health')\n"
        "def health():\n"
        '    """Health check."""\n'
        "    return 'ok'\n"
    )
    (pkg / "lib.py").write_text(
        '"""Auth helpers."""\n'
        "def verify(user, password):\n"
        "    return True\n"
    )


@pytest.mark.asyncio
async def test_export_explorer_writes_both_files(workspace: Path):
    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("export_explorer", {})).data

    assert out["ok"] is True
    explorer_dir = workspace / ".mcp-docs" / "explorer"
    data_path = explorer_dir / "data.json"
    html_path = explorer_dir / "index.html"
    assert data_path.exists(), "data.json not written"
    assert html_path.exists(), "index.html not written"
    assert {str(data_path), str(html_path)} == set(out["files_written"])

    # index.html inlines the data and loads Mermaid from a single CDN script.
    html = html_path.read_text(encoding="utf-8")
    assert 'id="explorer-data"' in html
    assert "mermaid" in html.lower()
    # v0.15 viewer: a real test-coverage meter + coverage_source badge,
    # DISTINCT from the existing link-confidence meter.
    assert "TEST_COVERAGE_LABEL" in html
    assert "test coverage" in html
    assert "cov-meters" in html
    assert "sourceBadge" in html
    assert "auto-derived (call graph)" in html
    assert "Avg ' + TEST_COVERAGE_LABEL" in html  # dashboard KPI tile
    # Endpoints tab: Swagger-style operations + client router at /explorer.
    assert "swagger-op" in html
    assert "swagger-toolbar" in html
    assert "buildLanding" in html
    assert "navigateTo" in html
    assert '"base_path": "/explorer"' in html or "base_path" in html
    assert 'data-route="changes"' in html
    assert "copy-call" in html
    assert "callShape" in html
    assert "http-exec" in html
    assert "ep-base-url" in html
    assert "isHttpTryIt" in html
    # Endpoints grouped by MCP kind (Tools / Resources / Prompts), and the
    # Mermaid label sanitizer for the Dependencies tab is present.
    assert "EP_KIND_LABEL" in html
    assert "mermaidLabel" in html
    # v0.16 B: the per-Spec uncovered-symbols drill-down (the actionable gap).
    assert "Uncovered symbols" in html
    assert "uncovered_symbols_count" in html
    # v0.16 A: a Changes tab driven by the git diff Spec impact section.
    assert "buildChanges" in html
    assert "specs_touched" in html
    # v0.16 D: a coverage trend view (sparkline + per-snapshot bars).
    assert "buildTrend" in html
    assert "buildTrendOverview" in html
    assert "trend-table" in html
    assert "Coverage trend" in html
    assert "History starts accumulating" in html
    # FIXED Mermaid (preserved): classDef colours are resolved to concrete
    # values via the C(...) palette resolver and emitted as a template literal
    # (`fill:${fill}`), NEVER a literal `fill:var(--x)` (Mermaid v10's classDef
    # parser rejects var()). The build emits the template form, not the var().
    assert "classDef ${cls} fill:${fill}" in html
    assert "const C = (name, fallback)" in html


@pytest.mark.asyncio
async def test_export_explorer_data_schema(workspace: Path):
    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Make SPEC-001 real and link it, plus a second Spec + a dependency edge.
        await c.call_tool("create_spec", {"spec_id": "SPEC-001", "title": "Login"})
        await c.call_tool("create_spec", {"spec_id": "SPEC-002", "title": "Auth lib"})
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-001", "symbol_qname": "app.routes.login"},
        )
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-002", "symbol_qname": "app.lib.verify"},
        )
        await c.call_tool(
            "link_spec_dependency",
            {"parent_spec_id": "SPEC-001", "child_spec_id": "SPEC-002"},
        )
        await c.call_tool("export_explorer", {})

    data = json.loads(
        (workspace / ".mcp-docs" / "explorer" / "data.json").read_text(encoding="utf-8")
    )
    assert set(data.keys()) == _TOP_KEYS

    # meta + counts shape
    assert set(data["meta"].keys()) == {"project", "generated_at", "base_path", "counts"}
    assert set(data["meta"]["counts"].keys()) == {
        "specs", "symbols", "endpoints", "files",
    }
    assert data["meta"]["counts"]["specs"] == 2

    # dashboard rollup (PO headline) shape + correctness
    dash = data["dashboard"]
    assert set(dash.keys()) == {
        "specs", "dev_state_counts", "with_endpoints",
        "with_dependencies", "implemented_pct", "verified", "avg_coverage",
        "avg_test_coverage",
    }
    assert dash["specs"] == 2
    assert set(dash["dev_state_counts"].keys()) == {
        "not_started", "in_progress", "implemented", "verified",
    }
    # dev_state counts sum to the spec total.
    assert sum(dash["dev_state_counts"].values()) == 2
    # Both Specs have a linked symbol -> both count as having implementation.
    assert dash["implemented_pct"] == 100.0
    # No test coverage exists here (no test symbols reach the impl, no
    # explicit test links), so verified is truthfully 0.
    assert dash["verified"] == 0
    assert dash["dev_state_counts"]["verified"] == 0
    assert dash["avg_coverage"] is not None
    # avg_test_coverage present; 0.0 here (no real test coverage).
    assert dash["avg_test_coverage"] == 0.0
    # verified count == #Specs with real test coverage > 0.
    assert dash["verified"] == sum(
        1 for r in data["specs"] if r["test_coverage_ratio"] > 0
    )

    # SPEC-001 carries its implementing symbol (with signature), endpoint, dep.
    rf1 = next(r for r in data["specs"] if r["id"] == "SPEC-001")
    assert any(s["qname"] == "app.routes.login" for s in rf1["symbols"])
    sym = next(s for s in rf1["symbols"] if s["qname"] == "app.routes.login")
    assert "signature" in sym and "file" in sym and "line" in sym
    assert "app.routes.login" in rf1["endpoints"]
    assert rf1["depends_on"] == ["SPEC-002"]
    assert rf1["coverage"] is not None

    # Every spec carries a derived dev_state in the valid vocabulary.
    valid_states = {"not_started", "in_progress", "implemented", "verified"}
    valid_sources = {"derived", "explicit", "both", "none"}
    for r in data["specs"]:
        assert r["dev_state"] in valid_states
        # v0.15: REAL test-coverage fields on every spec.
        assert isinstance(r["test_coverage_ratio"], (int, float))
        assert 0.0 <= r["test_coverage_ratio"] <= 1.0
        assert r["coverage_source"] in valid_sources
        # verified iff real test coverage exists (supersedes link-only rule).
        if r["test_coverage_ratio"] > 0:
            assert r["dev_state"] == "verified"
        # link confidence and test coverage are distinct fields.
        assert "coverage" in r and "test_coverage_ratio" in r
        # v0.16 B: every Spec carries the uncovered-symbol drill-down (a list +
        # an exact count), sourced from compute_spec_test_coverage — no recompute.
        assert isinstance(r["uncovered_symbols"], list)
        assert isinstance(r["uncovered_symbols_count"], int)
        assert r["uncovered_symbols_count"] >= len(r["uncovered_symbols"])
    # SPEC-001 has a high-confidence link but no test coverage -> implemented.
    assert rf1["dev_state"] == "implemented"
    assert rf1["test_coverage_ratio"] == 0.0
    assert rf1["coverage_source"] == "none"

    # Topology nodes carry dev_state too (for colour-coding the graph).
    for n in data["spec_topology"]["nodes"]:
        assert n["dev_state"] in valid_states

    # topology has the SPEC-001 -> SPEC-002 edge
    edges = {(e["from"], e["to"]) for e in data["spec_topology"]["edges"]}
    assert ("SPEC-001", "SPEC-002") in edges
    node_ids = {n["id"] for n in data["spec_topology"]["nodes"]}
    assert {"SPEC-001", "SPEC-002"} <= node_ids

    # endpoints surface includes the login route, tagged with SPEC-001
    handlers = {e["handler"] for e in data["endpoints"]}
    assert "app.routes.login" in handlers
    login_ep = next(e for e in data["endpoints"] if e["handler"] == "app.routes.login")
    assert "SPEC-001" in login_ep["spec_ids"]
    for ep in data["endpoints"]:
        assert set(ep.keys()) == {
            "kind", "framework", "handler", "signature", "path", "method",
            "spec_ids",
        }
        # Every API-surface endpoint carries a kind in the valid vocabulary,
        # and is never a pytest fixture (those live in DATA.fixtures).
        assert ep["kind"] in {"tool", "resource", "prompt", "other"}
    # Flask @app.route with http_method/http_path is framework-routed API
    # surface (same bucket as hono/TS routes), not a bare decorator-only entry.
    assert login_ep["kind"] == "tool"
    assert login_ep["framework"] == "flask"
    assert login_ep["method"] == "POST"
    assert login_ep["path"] == "/login"

    # Fixtures are a separate collection (test infra, not API surface).
    assert "fixtures" in data
    assert isinstance(data["fixtures"], list)
    for fx in data["fixtures"]:
        assert fx["kind"] == "fixture"
        assert set(fx.keys()) == {
            "kind", "framework", "handler", "signature", "path", "method",
            "spec_ids",
        }
    # No fixture leaks into the API-surface endpoint list or its count.
    assert all(e["kind"] != "fixture" for e in data["endpoints"])
    assert data["meta"]["counts"]["endpoints"] == len(data["endpoints"])

    # coverage section shape
    assert set(data["coverage"].keys()) == {
        "orphan_modules", "orphan_endpoints", "totals",
    }

    # v0.16 D: top-level coverage trend — a chronological list of rollup
    # snapshots. audit_coverage (run inside compute_coverage) records one each
    # time, so after an export there is at least one snapshot. Each row carries
    # ts / avg_test_coverage / verified_count.
    assert isinstance(data["trend"], list)
    for snap in data["trend"]:
        assert set(snap.keys()) == {"ts", "avg_test_coverage", "verified_count"}
        assert isinstance(snap["verified_count"], int)
        assert snap["avg_test_coverage"] is None or isinstance(
            snap["avg_test_coverage"], (int, float)
        )

    # v0.16 A: top-level changes — Spec-centric git diff impact. The workspace
    # fixture is not a git repo, so the range is omitted (base/head None) and
    # the lists are empty — but the keyed shape is always present.
    assert isinstance(data["changes"], dict)
    assert set(data["changes"].keys()) == {
        "base", "head", "files_changed", "specs_touched",
    }
    assert isinstance(data["changes"]["files_changed"], list)
    assert isinstance(data["changes"]["specs_touched"], list)
    for rt in data["changes"]["specs_touched"]:
        assert set(rt.keys()) == {
            "spec_id", "title", "files", "test_coverage_ratio",
        }


@pytest.mark.asyncio
async def test_export_explorer_zero_rf_case(workspace: Path):
    """No Specs: specs==[] but endpoints + coverage still populated."""
    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("export_explorer", {})

    data = json.loads(
        (workspace / ".mcp-docs" / "explorer" / "data.json").read_text(encoding="utf-8")
    )
    assert data["specs"] == []
    assert data["spec_topology"]["nodes"] == []
    assert data["spec_topology"]["edges"] == []
    assert data["meta"]["counts"]["specs"] == 0

    # Dashboard is present and truthfully empty in the 0-Spec case.
    assert "dashboard" in data
    assert data["dashboard"]["specs"] == 0
    assert data["dashboard"]["verified"] == 0
    assert data["dashboard"]["implemented_pct"] == 0.0
    assert data["dashboard"]["avg_coverage"] is None
    # avg_test_coverage present and None (no Specs to average).
    assert data["dashboard"]["avg_test_coverage"] is None
    assert sum(data["dashboard"]["dev_state_counts"].values()) == 0

    # Endpoints + coverage are not gated on Specs existing.
    handlers = {e["handler"] for e in data["endpoints"]}
    assert "app.routes.login" in handlers
    assert "app.routes.health" in handlers
    # Every endpoint is orphan (no Spec linked) in the 0-Spec case.
    assert "app.routes.login" in data["coverage"]["orphan_endpoints"]
    assert data["meta"]["counts"]["endpoints"] >= 2
    assert isinstance(data["coverage"]["totals"], dict)
    assert data["coverage"]["totals"]

    # trend + changes stay truthful in the 0-Spec case: trend is a list (no Specs
    # means each snapshot's avg is None), changes is the keyed-empty shape.
    assert isinstance(data["trend"], list)
    assert isinstance(data["changes"], dict)
    assert data["changes"]["specs_touched"] == []
    assert set(data["changes"].keys()) == {
        "base", "head", "files_changed", "specs_touched",
    }


def _write_fixture_module(workspace: Path) -> None:
    """A tiny pytest test module declaring a @pytest.fixture (test infra)."""
    (workspace / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.fixture\n"
        "def sample_db():\n"
        '    """A test fixture — NOT API surface."""\n'
        "    return {}\n"
    )


@pytest.mark.asyncio
async def test_export_explorer_excludes_fixtures_from_surface(workspace: Path):
    """pytest.fixture entries are split into DATA.fixtures, not endpoints."""
    _write_flask_app(workspace)
    _write_fixture_module(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("export_explorer", {})

    data = json.loads(
        (workspace / ".mcp-docs" / "explorer" / "data.json").read_text(encoding="utf-8")
    )
    handlers = {e["handler"] for e in data["endpoints"]}
    fixture_handlers = {f["handler"] for f in data["fixtures"]}
    # The fixture is recognised and routed into the separate collection.
    assert "conftest.sample_db" in fixture_handlers
    # It does NOT appear in the API-surface endpoints, nor inflate the count.
    assert "conftest.sample_db" not in handlers
    assert all(e["kind"] != "fixture" for e in data["endpoints"])
    assert data["meta"]["counts"]["endpoints"] == len(data["endpoints"])
    # The Flask routes are still present as API surface.
    assert "app.routes.login" in handlers


@pytest.mark.asyncio
async def test_export_explorer_mermaid_labels_sanitized(workspace: Path):
    """Spec titles with &, <, >, " must be HTML-entity-encoded for Mermaid v10
    so the Dependencies graph parses (no 'Syntax error in text')."""
    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # A title loaded with Mermaid-breaking characters.
        await c.call_tool(
            "create_spec",
            {"spec_id": "SPEC-001", "title": 'Dead-code & coverage <x> "q" (9 langs)'},
        )
        await c.call_tool("export_explorer", {})

    data = json.loads(
        (workspace / ".mcp-docs" / "explorer" / "data.json").read_text(encoding="utf-8")
    )
    # The raw, unsanitized title is stored verbatim in the data (the viewer
    # sanitizes at render time via mermaidLabel()). This locks in that the
    # breaking chars reach the viewer and rely on JS encoding.
    node = next(n for n in data["spec_topology"]["nodes"] if n["id"] == "SPEC-001")
    assert "&" in node["title"] and "<" in node["title"]

    # Mirror the viewer's mermaidLabel() transform in Python and assert the
    # produced `graph TD` node label is safe: entities present, no raw
    # ` & ` / ` < ` / ` > ` left inside the quoted ["..."] label.
    def mermaid_label(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    label = mermaid_label(node["id"] + ": " + node["title"])
    assert "&amp;" in label and "&lt;" in label and "&gt;" in label
    assert "&quot;" in label
    # No raw breaking chars survive (the only & are entity prefixes).
    assert "<" not in label and ">" not in label and '"' not in label
    # Parentheses are safe inside a quoted Mermaid label — kept readable.
    assert "(9 langs)" in label


def _strip_volatile(d: dict) -> dict:
    """Drop the fields that are non-deterministic BY DESIGN before comparing.

    - ``meta.generated_at`` — the injectable timestamp.
    - ``trend`` — every audit records a wall-clock snapshot (and the series
      grows between two export calls), so it is inherently per-run.
    - ``changes`` — Spec-centric git diff impact for the resolved range; depends
      on the workspace's git state, so it is range-dependent, not content-pure.
    """
    d = dict(d)
    d["meta"] = {**d["meta"], "generated_at": None}
    d.pop("trend", None)
    d.pop("changes", None)
    return d


@pytest.mark.asyncio
async def test_export_explorer_idempotent(workspace: Path):
    """Two runs on an unchanged project yield identical data.json modulo
    meta.generated_at, the recorded coverage trend, and the diff range."""
    _write_flask_app(workspace)
    data_path = workspace / ".mcp-docs" / "explorer" / "data.json"
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"spec_id": "SPEC-001", "title": "Login"})
        await c.call_tool(
            "link_spec_symbol",
            {"spec_id": "SPEC-001", "symbol_qname": "app.routes.login"},
        )

        await c.call_tool("export_explorer", {"generated_at": "2026-01-01T00:00:00Z"})
        first = data_path.read_text(encoding="utf-8")
        await c.call_tool("export_explorer", {"generated_at": "2099-12-31T23:59:59Z"})
        second = data_path.read_text(encoding="utf-8")

    # Strip the non-deterministic fields and compare the rest verbatim.
    fd = json.loads(first)
    sd = json.loads(second)
    assert fd["meta"]["generated_at"] == "2026-01-01T00:00:00Z"
    assert sd["meta"]["generated_at"] == "2099-12-31T23:59:59Z"
    assert _strip_volatile(fd) == _strip_volatile(sd)

    # With the default (None) timestamp, two runs match modulo the volatile
    # fields (generated_at, the accumulating trend, the range-bound changes).
    async with Client(mcp) as c:
        await c.call_tool("export_explorer", {})
        run_a = data_path.read_text(encoding="utf-8")
        await c.call_tool("export_explorer", {})
        run_b = data_path.read_text(encoding="utf-8")
    assert _strip_volatile(json.loads(run_a)) == _strip_volatile(json.loads(run_b))


@pytest.mark.asyncio
async def test_write_explorer_bundle_no_args_backward_compat(workspace: Path):
    """write_explorer_bundle(st) — the no-arg call indexing.py's freshness
    hook makes — must still work after the base/head params were added."""
    from livespec_mcp.state import get_state
    from livespec_mcp.tools.explorer import write_explorer_bundle

    _write_flask_app(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})

    st = get_state(str(workspace))
    # No base/head passed — defaults must keep the call working unchanged.
    result = write_explorer_bundle(st)
    assert set(result["data"].keys()) == _TOP_KEYS
    assert "autowire" in result
    assert isinstance(result["data"]["trend"], list)
    assert isinstance(result["data"]["changes"], dict)
    assert {"base", "head", "files_changed", "specs_touched"} == set(
        result["data"]["changes"].keys()
    )
    # Both files land on disk.
    for p in result["files_written"]:
        assert Path(p).exists()
