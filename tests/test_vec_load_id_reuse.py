"""Regression: sqlite-vec load tracking must not be keyed by id(conn).

`have_sqlite_vec` memoized loaded connections in a `set[int]` of `id(conn)`.
CPython reuses object ids after collection and a `sqlite3.Connection` cannot be
weak-referenced, so a freshly opened connection landing on a freed address was
reported as "already loaded" and never had the extension loaded. Downstream that
surfaced as `OperationalError: no such module: vec0` from `ensure_vec_tables` —
which reads as a missing dependency and is really a stale cache.

It is not hypothetical: 399 of 400 sequentially opened-and-closed connections
reused an id, and the full suite failed roughly one run in four once enough
connection churn existed to trigger it. In a long-running server the LRU in
state.py closes connections on eviction, so the same churn happens in
production and silently disables vector search.
"""

from __future__ import annotations

import gc
import sqlite3

import pytest

from livespec_mcp.domain.rag import ensure_vec_tables, have_sqlite_vec

sqlite_vec = pytest.importorskip(
    "sqlite_vec", reason="sqlite-vec extra not installed"
)


def test_id_reuse_is_real():
    """Documents the premise; if CPython ever stops reusing ids this can go."""
    seen: set[int] = set()
    reused = 0
    for _ in range(200):
        conn = sqlite3.connect(":memory:")
        if id(conn) in seen:
            reused += 1
        seen.add(id(conn))
        conn.close()
        del conn
    gc.collect()
    assert reused > 0, "id reuse did not occur — this test's premise is stale"


def test_fresh_connection_on_a_reused_id_still_loads_the_extension():
    """The exact failure: churn connections so an id is reused, then confirm the
    new connection can actually create vec0 tables."""
    seen: set[int] = set()
    for _ in range(200):
        conn = sqlite3.connect(":memory:")
        reused = id(conn) in seen
        seen.add(id(conn))

        # Call it on EVERY connection: the old memo only went stale once it had
        # actually recorded an id, so probing solely the reused one would leave
        # the memo empty and the test would pass against the bug.
        assert have_sqlite_vec(conn) is True

        if reused:
            # The real assertion: this raised "no such module: vec0" whenever
            # have_sqlite_vec answered from the stale memo instead of loading.
            ensure_vec_tables(conn)
            conn.close()
            return

        conn.close()
        del conn

    pytest.skip("no id reuse observed in 200 connections")


def test_repeated_calls_on_one_connection_are_stable():
    """The probe replaced a memo; calling it repeatedly must stay correct."""
    conn = sqlite3.connect(":memory:")
    try:
        assert have_sqlite_vec(conn) is True
        assert have_sqlite_vec(conn) is True
        ensure_vec_tables(conn)
    finally:
        conn.close()
