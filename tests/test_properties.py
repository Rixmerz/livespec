"""Property-based tests with hypothesis (P2.3).

These cover invariants that example-based tests don't reach: arbitrary
docstrings shouldn't crash the matcher, the indexer should be idempotent
on any state, etc.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

from livespec_mcp.config import Settings
from livespec_mcp.domain.indexer import index_project
from livespec_mcp.domain.matcher import parse_annotations
from livespec_mcp.storage.db import connect


# ---------- Matcher ----------


@given(st.text(max_size=2000))
@settings(max_examples=200, deadline=None)
def test_parse_annotations_never_crashes(text: str):
    """No matter what garbage we throw at the matcher, it returns a list of
    AnnotationHit (possibly empty) and never raises."""
    out = parse_annotations(text)
    assert isinstance(out, list)
    for hit in out:
        assert hit.rf_id.startswith("RF-")
        assert hit.confidence in (0.7, 1.0)
        assert hit.relation in ("implements", "tests", "references")


@given(st.integers(min_value=0, max_value=999))
@settings(max_examples=50, deadline=None)
def test_parse_annotations_normalizes_rf_ids(n: int):
    """`@rf:RF-N` and `@rf:RF-00N` produce the same normalized id."""
    text_short = f"@rf:RF-{n}"
    text_padded = f"@rf:RF-{n:03d}"
    a = parse_annotations(text_short)
    b = parse_annotations(text_padded)
    assert len(a) == 1 and len(b) == 1
    assert a[0].rf_id == b[0].rf_id == f"RF-{n:03d}"


# ---------- Indexer idempotence ----------


def _bootstrap_settings(tmp_path: Path) -> Settings:
    state_dir = tmp_path / ".mcp-docs"
    return Settings(
        workspace=tmp_path,
        state_dir=state_dir,
        db_path=state_dir / "docs.db",
        docs_dir=state_dir / "docs",
        models_dir=state_dir / "models",
    )


@given(
    n_files=st.integers(min_value=0, max_value=8),
    n_funcs=st.integers(min_value=0, max_value=6),
)
@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_index_project_is_idempotent(tmp_path_factory, n_files: int, n_funcs: int):
    """Re-indexing without file changes must leave edges and symbols unchanged."""
    tmp_path = tmp_path_factory.mktemp("idem")
    # Generate a small synthetic project
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for i in range(n_files):
        body = ['"""Auto."""', "", "def fn_0():", "    return 0", ""]
        for j in range(1, n_funcs + 1):
            body += [f"def fn_{j}():", f"    return fn_{j - 1}()", ""]
        (pkg / f"mod_{i:02d}.py").write_text("\n".join(body))

    settings = _bootstrap_settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    first = index_project(settings, conn)
    second = index_project(settings, conn)
    assert second.files_changed == 0, (
        f"Re-index without edits changed {second.files_changed} files"
    )
    assert second.symbols_total == first.symbols_total
    assert second.edges_total == first.edges_total
    conn.close()


# ---------- Resolver: edges never decrease without file delete ----------


@given(
    n_funcs=st.integers(min_value=2, max_value=8),
    edits=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_partial_reindex_does_not_lose_edges(tmp_path_factory, n_funcs: int, edits: int):
    """Touching one file repeatedly should not cause edge counts to drift down."""
    tmp_path = tmp_path_factory.mktemp("partial")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    body = ['"""Auto."""', "", "def fn_0():", "    return 0", ""]
    for j in range(1, n_funcs):
        body += [f"def fn_{j}():", f"    return fn_{j - 1}()", ""]
    target = pkg / "mod.py"
    target.write_text("\n".join(body))

    settings = _bootstrap_settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    first = index_project(settings, conn)
    baseline = first.edges_total

    for _ in range(edits):
        target.write_text(target.read_text() + "\n# touched\n")
        out = index_project(settings, conn)
        # Allow growth (new # touched line might add nothing) but never shrink
        # below the baseline beyond a small tolerance.
        assert out.edges_total >= baseline - 1, (
            f"edges shrank: baseline={baseline} now={out.edges_total}"
        )
    conn.close()
