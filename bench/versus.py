#!/usr/bin/env python3
"""EXPERIMENTAL versus: SQLite (what livespec ships) vs Kerno (kernodb).

Indexes a real repo with the normal SQLite pipeline, mirrors the same symbols,
edges and chunks into a Kerno database (storage/kerno_store.py), then runs the
same questions against both and checks that the answers agree before timing
them. See docs/experiments/kernodb-versus.md for the reading of the numbers.

Usage:
    uv run python bench/versus.py --workdir /tmp/versus            # requests
    uv run python bench/versus.py --workdir /tmp/versus --repos requests fastapi django
    uv run python bench/versus.py --workdir /tmp/versus --json bench/versus-results.json

Needs the optional dependency: `uv pip install --pre kernodb`.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from livespec_mcp.config import Settings
from livespec_mcp.domain import graph as graph_mod
from livespec_mcp.domain.indexer import index_project
from livespec_mcp.domain.rag import fts_search, rebuild_chunks
from livespec_mcp.storage.db import connect
from livespec_mcp.storage.kerno_store import KernoMirror, kerno

REPOS = {
    "requests": ("https://github.com/psf/requests.git", "v2.32.3"),
    "fastapi": ("https://github.com/fastapi/fastapi.git", "0.118.0"),
    "rich": ("https://github.com/Textualize/rich.git", "v13.9.4"),
    "django": ("https://github.com/django/django.git", "5.1.4"),
}

# The subset of livespec's schema the mirror covers, with the same indexes, for
# the like-for-like ingest row: "write these rows into an empty database".
_SUBSET_SCHEMA = """
CREATE TABLE symbol (id INTEGER PRIMARY KEY, name TEXT NOT NULL, qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    body_hash TEXT, path TEXT NOT NULL);
CREATE INDEX idx_symbol_qname ON symbol(qualified_name);
CREATE INDEX idx_symbol_name ON symbol(name);
CREATE INDEX idx_symbol_path ON symbol(path);
CREATE TABLE symbol_edge (id INTEGER PRIMARY KEY, src_symbol_id INTEGER NOT NULL,
    dst_symbol_id INTEGER NOT NULL, edge_type TEXT NOT NULL, weight REAL NOT NULL,
    origin TEXT NOT NULL, UNIQUE(src_symbol_id, dst_symbol_id, edge_type));
CREATE INDEX idx_edge_src ON symbol_edge(src_symbol_id, edge_type);
CREATE INDEX idx_edge_dst ON symbol_edge(dst_symbol_id, edge_type);
CREATE TABLE chunk (id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, source_id INTEGER,
    text_kind TEXT NOT NULL, file_path TEXT, start_line INTEGER, end_line INTEGER,
    text TEXT NOT NULL);
CREATE VIRTUAL TABLE chunk_fts USING fts5(text, content='chunk', content_rowid='id',
    tokenize='unicode61');
"""


def _clone(repo: str, ref: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", ref, repo, str(dest)], check=True
    )


def _dir_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _sqlite_bytes(db: Path) -> int:
    return sum(p.stat().st_size for p in db.parent.glob(db.name + "*") if p.is_file())


def _ms(fn: Callable[[], Any]) -> tuple[float, Any]:
    t = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t) * 1000, out


def _median_ms(fn: Callable[[Any], Any], inputs: list[Any], rounds: int = 3) -> float:
    """Median per-call latency over every input, best of `rounds` passes."""
    best = float("inf")
    for _ in range(rounds):
        samples = []
        for x in inputs:
            t = time.perf_counter()
            fn(x)
            samples.append((time.perf_counter() - t) * 1000)
        best = min(best, statistics.median(samples))
    return best


def _subset_ingest(src: sqlite3.Connection, pid: int, dest: Path, synchronous: str) -> float:
    """Write the mirrored rows into an empty SQLite database, one transaction."""
    for p in dest.parent.glob(dest.name + "*"):
        p.unlink()
    symbols = src.execute(
        """SELECT s.id, s.name, s.qualified_name, s.kind, s.start_line, s.end_line,
                  s.body_hash, f.path
           FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (pid,),
    ).fetchall()
    edges = src.execute(
        """SELECT e.id, e.src_symbol_id, e.dst_symbol_id, e.edge_type, e.weight, e.origin
           FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id
           JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (pid,),
    ).fetchall()
    chunks = src.execute(
        """SELECT id, source_type, source_id, text_kind, file_path, start_line, end_line, text
           FROM chunk WHERE project_id=?""",
        (pid,),
    ).fetchall()
    t = time.perf_counter()
    out = sqlite3.connect(dest)
    out.execute("PRAGMA journal_mode = WAL")
    out.execute(f"PRAGMA synchronous = {synchronous}")
    out.executescript(_SUBSET_SCHEMA)
    with out:
        out.executemany("INSERT INTO symbol VALUES (?,?,?,?,?,?,?,?)", [tuple(r) for r in symbols])
        out.executemany("INSERT INTO symbol_edge VALUES (?,?,?,?,?,?)", [tuple(r) for r in edges])
        out.executemany("INSERT INTO chunk VALUES (?,?,?,?,?,?,?,?)", [tuple(r) for r in chunks])
        out.execute("INSERT INTO chunk_fts(rowid, text) SELECT id, text FROM chunk")
    out.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    out.close()
    return (time.perf_counter() - t) * 1000


def _sql_graph(conn: sqlite3.Connection, pid: int):
    graph_mod.invalidate_graph_cache(None)
    return graph_mod.load_graph(conn, pid)


def _edge_set(view) -> set:
    return {(u, v, d["edge_type"], d["weight"], d["origin"]) for u, v, d in view.g.edges(data=True)}


def _bench_repo(name: str, workspace: Path, scratch: Path) -> dict[str, Any]:
    state_dir = scratch / f"{name}-state"
    shutil.rmtree(state_dir, ignore_errors=True)
    settings = Settings(
        workspace=workspace,
        state_dir=state_dir,
        db_path=state_dir / "docs.db",
        docs_dir=state_dir / "docs",
    )
    settings.ensure_dirs()
    kpath = state_dir / "kerno"
    res: dict[str, Any] = {"repo": name}
    parity: dict[str, Any] = {}

    # ---- 1. the real pipeline (extraction + SQL resolution), SQLite only
    conn = connect(settings.db_path)
    cold_ms, stats = _ms(lambda: index_project(settings, conn))
    pid = int(conn.execute("SELECT id FROM project LIMIT 1").fetchone()["id"])
    chunk_ms, _ = _ms(lambda: rebuild_chunks(conn, pid))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    run_id = graph_mod._latest_run_id(conn, pid)
    n_chunks = conn.execute("SELECT COUNT(*) c FROM chunk WHERE project_id=?", (pid,)).fetchone()[
        "c"
    ]
    res["corpus"] = {
        "files": stats.files_total,
        "symbols": stats.symbols_total,
        "edges": stats.edges_total,
        "chunks": int(n_chunks),
        "index_project_ms": round(cold_ms, 1),
        "rebuild_chunks_ms": round(chunk_ms, 1),
    }

    # ---- 2. ingest: the same rows into an empty database
    subset = state_dir / "subset.db"
    ingest: dict[str, Any] = {}
    ingest["sqlite_normal_ms"] = round(_subset_ingest(conn, pid, subset, "NORMAL"), 1)
    ingest["sqlite_full_ms"] = round(_subset_ingest(conn, pid, subset, "FULL"), 1)
    mirror = KernoMirror(kpath)
    kms, kstats = _ms(lambda: mirror.sync(conn, pid, run_id=run_id))
    ingest["kerno_ms"] = round(kms, 1)
    ingest["kerno_stats"] = kstats
    noop_ms, noop_stats = _ms(lambda: mirror.sync(conn, pid, run_id=run_id))
    ingest["kerno_noop_resync_ms"] = round(noop_ms, 1)
    parity["noop_resync_wrote_nothing"] = (
        noop_stats["symbols"]["created"] + noop_stats["symbols"]["updated"] == 0
        and noop_stats["edges"]["added"] + noop_stats["edges"]["removed"] == 0
    )
    res["ingest"] = ingest

    # ---- 3. disk
    mirror.close()
    disk = {
        "sqlite_full_index_bytes": _sqlite_bytes(settings.db_path),
        "sqlite_subset_bytes": _sqlite_bytes(subset),
        "kerno_bytes": _dir_bytes(kpath),
    }
    mirror = KernoMirror(kpath)
    disk["kerno_compact"] = mirror.db.compact()
    mirror.close()
    disk["kerno_after_compact_bytes"] = _dir_bytes(kpath)
    res["disk"] = disk

    # ---- 4. reads, on a read-only Kerno handle beside the SQLite connection
    sub = sqlite3.connect(subset)
    sub.row_factory = sqlite3.Row
    cold_open: dict[str, float] = {}

    def _sqlite_cold() -> None:
        c = sqlite3.connect(subset)
        c.execute("SELECT * FROM symbol WHERE name=?", ("__init__",)).fetchall()
        c.close()

    def _kerno_cold() -> None:
        with KernoMirror(kpath, read_only=True) as m:
            m.find_symbol("__init__")

    cold_open["sqlite_ms"] = round(min(_ms(_sqlite_cold)[0] for _ in range(5)), 3)
    cold_open["kerno_ms"] = round(min(_ms(_kerno_cold)[0] for _ in range(5)), 3)
    res["open_plus_first_query"] = cold_open

    mirror = KernoMirror(kpath, read_only=True)
    reads: dict[str, dict[str, float]] = {}

    # graph load: the hot path behind who_calls / analyze_impact / find_dead_code
    s_ms, view = _ms(lambda: _sql_graph(conn, pid))
    k_ms, (k_meta, k_edges) = _ms(mirror.graph)
    reads["graph_load"] = {"sqlite_ms": round(s_ms, 2), "kerno_ms": round(k_ms, 2)}
    parity["graph_nodes_equal"] = view.sym_meta == k_meta
    parity["graph_edges_equal"] = _edge_set(view) == set(k_edges)

    rng = random.Random(7)
    all_syms = list(view.sym_meta.values())
    sample = rng.sample(all_syms, min(200, len(all_syms)))
    names = sorted({m["name"] for m in sample})
    qnames = sorted({m["qualified_name"] for m in sample})
    paths = sorted({m["file_path"] for m in sample})[:100]
    by_in = sorted(view.g.nodes, key=lambda n: view.g.in_degree(n), reverse=True)
    hubs = by_in[:50] + [m["id"] for m in sample[:150]]

    def s_name(n):
        return sub.execute(
            "SELECT id, name, qualified_name, kind, path, start_line, end_line FROM symbol WHERE name=?",
            (n,),
        ).fetchall()

    def s_qname(q):
        return sub.execute(
            "SELECT id, name, qualified_name, kind, path, start_line, end_line FROM symbol WHERE qualified_name=?",
            (q,),
        ).fetchall()

    def s_file(p):
        return sub.execute(
            "SELECT id, name, qualified_name, kind, path, start_line, end_line FROM symbol WHERE path=?",
            (p,),
        ).fetchall()

    def s_callers(sid):
        return sub.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.path, s.start_line, s.end_line
               FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id
               WHERE e.dst_symbol_id=? AND e.edge_type='calls'""",
            (sid,),
        ).fetchall()

    def ids(rows) -> set[int]:
        return {int(r["id"]) for r in rows}

    cases = [
        ("find_symbol_by_name", names, s_name, mirror.find_symbol),
        ("find_by_qualified_name", qnames, s_qname, mirror.find_qname),
        ("symbols_in_file", paths, s_file, mirror.symbols_in_file),
        ("callers_1_hop", hubs, s_callers, mirror.callers),
    ]
    for label, inputs, s_fn, k_fn in cases:
        parity[label] = all(ids(s_fn(x)) == ids(k_fn(x)) for x in inputs)
        reads[label] = {
            "sqlite_ms": round(_median_ms(s_fn, inputs), 4),
            "kerno_ms": round(_median_ms(k_fn, inputs), 4),
        }

    # keyword search: rankers differ (FTS5 bm25 vs Kerno BM25), so parity is overlap
    queries = [
        "session timeout",
        "parse header",
        "redirect",
        "encode json body",
        "authentication",
        "exception error",
        "cookie",
        "response status code",
    ]

    def s_search(q):
        return fts_search(conn, pid, q, 10, "all")[:10]

    def k_search(q):
        return mirror.search(q, 10)

    overlaps = []
    for q in queries:
        a = {cid for cid, _, _ in s_search(q)}
        b = {h["chunk_id"] for h in k_search(q)}
        if a or b:
            overlaps.append(len(a & b) / len(a | b))
    # Recall: how many chunks match a single word at all, either side.
    words = ["redirect", "cookie", "timeout", "session", "header", "return"]
    s_total = sum(
        conn.execute(
            """SELECT COUNT(*) c FROM chunk_fts JOIN chunk c ON c.id = chunk_fts.rowid
               WHERE chunk_fts MATCH ? AND c.project_id = ?""",
            (w, pid),
        ).fetchone()["c"]
        for w in words
    )
    k_total = sum(len(mirror.search(w, 10**6)) for w in words)
    parity["keyword_matches_sqlite"] = int(s_total)
    parity["keyword_matches_kerno"] = k_total
    parity["keyword_top10_jaccard_mean"] = round(statistics.mean(overlaps), 3) if overlaps else None
    reads["keyword_search_top10"] = {
        "sqlite_ms": round(_median_ms(s_search, queries), 4),
        "kerno_ms": round(_median_ms(k_search, queries), 4),
    }

    # substring: LIKE '%x%' full scan vs the trigram index
    needles = ["timeout=", "raise ValueError", "self._", "isinstance(", "return None", "TODO"]

    def s_grep(n):
        # ESCAPE: `_` and `%` are LIKE wildcards, and needles such as `self._` hold one.
        pat = n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return sub.execute(
            "SELECT id FROM chunk WHERE text LIKE ? ESCAPE '\\'", (f"%{pat}%",)
        ).fetchall()

    def k_grep(n):
        return mirror.grep(n)

    extra = missing = 0
    for n in needles:
        a, b = ids(s_grep(n)), set(k_grep(n))
        extra += len(b - a)
        missing += len(a - b)
    parity["substring_missing_in_kerno"] = missing
    parity["substring_extra_in_kerno"] = extra
    reads["substring_search"] = {
        "sqlite_ms": round(_median_ms(s_grep, needles), 4),
        "kerno_ms": round(_median_ms(k_grep, needles), 4),
    }
    res["reads"] = reads
    mirror.close()
    sub.close()

    # ---- 5. incremental: edit one file, re-index, re-sync
    target = max(
        (p for p in workspace.rglob("*.py") if ".git" not in p.parts and "test" not in str(p)),
        key=lambda p: p.stat().st_size,
    )
    original = target.read_text(encoding="utf-8", errors="replace")
    inc: dict[str, Any] = {"file": str(target.relative_to(workspace))}
    try:
        target.write_text(original + "\n\ndef _versus_probe(x):\n    return x + 1\n")
        s_ms, _ = _ms(lambda: (index_project(settings, conn), rebuild_chunks(conn, pid)))
        conn.commit()
        inc["sqlite_reindex_ms"] = round(s_ms, 1)
        run2 = graph_mod._latest_run_id(conn, pid)
        mirror = KernoMirror(kpath)
        k_ms, kst = _ms(lambda: mirror.sync(conn, pid, run_id=run2))
        inc["kerno_resync_ms"] = round(k_ms, 1)
        inc["kerno_stats"] = kst
        view2 = _sql_graph(conn, pid)
        k_meta2, k_edges2 = mirror.graph()
        parity["graph_equal_after_incremental"] = view2.sym_meta == k_meta2 and _edge_set(
            view2
        ) == set(k_edges2)
        # What only Kerno answers: the mirror kept the previous revision.
        seq = mirror.db.last_commit_seq()
        inc["kerno_commit_seq"] = seq
        probe = mirror.find_symbol("_versus_probe")
        inc["kerno_history_of_new_symbol"] = mirror.symbol_history(probe[0]["id"]) if probe else []
        mirror.close()
    finally:
        target.write_text(original)
    res["incremental"] = inc
    res["parity"] = parity
    conn.close()
    return res


def _ratio(sqlite_ms: float, kerno_ms: float) -> str:
    if not sqlite_ms or not kerno_ms:
        return "-"
    r = kerno_ms / sqlite_ms
    return f"Kerno {r:.1f}x slower" if r >= 1 else f"Kerno {1 / r:.1f}x faster"


def _report(r: dict[str, Any]) -> str:
    c, i, d = r["corpus"], r["ingest"], r["disk"]
    mb = 1024 * 1024
    lines = [
        f"### {r['repo']}: {c['files']} files, {c['symbols']} symbols, {c['edges']} edges, "
        f"{c['chunks']} chunks",
        "",
        "| Round | SQLite | Kerno | Verdict |",
        "|---|---|---|---|",
        f"| Ingest the same rows (ms) | {i['sqlite_normal_ms']} (NORMAL) / {i['sqlite_full_ms']} (FULL) "
        f"| {i['kerno_ms']} | {_ratio(i['sqlite_full_ms'], i['kerno_ms'])} |",
        f"| Disk, same rows (MB) | {d['sqlite_subset_bytes'] / mb:.2f} "
        f"| {d['kerno_bytes'] / mb:.2f} ({d['kerno_after_compact_bytes'] / mb:.2f} compacted) "
        f"| {d['kerno_after_compact_bytes'] / d['sqlite_subset_bytes']:.2f}x |",
        f"| Open + first query (ms) | {r['open_plus_first_query']['sqlite_ms']} "
        f"| {r['open_plus_first_query']['kerno_ms']} "
        f"| {_ratio(r['open_plus_first_query']['sqlite_ms'], r['open_plus_first_query']['kerno_ms'])} |",
    ]
    for label, v in r["reads"].items():
        lines.append(
            f"| {label} (ms) | {v['sqlite_ms']} | {v['kerno_ms']} "
            f"| {_ratio(v['sqlite_ms'], v['kerno_ms'])} |"
        )
    inc = r["incremental"]
    lines.append(
        f"| Re-sync after a 1-file edit (ms) | {inc['sqlite_reindex_ms']} (whole re-index) "
        f"| +{inc['kerno_resync_ms']} (mirror only) | - |"
    )
    lines.append(f"| No-op re-sync (ms) | - | {i['kerno_noop_resync_ms']} | - |")
    lines += ["", "Parity: " + ", ".join(f"`{k}`={v}" for k, v in r["parity"].items()), ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workdir", required=True, help="where repos are cloned and DBs are written")
    ap.add_argument("--repos", nargs="+", default=["requests"], choices=sorted(REPOS))
    ap.add_argument("--json", help="write the raw results here")
    args = ap.parse_args()
    if kerno is None:
        print("kernodb is not installed: uv pip install --pre kernodb", file=sys.stderr)
        return 2
    work = Path(args.workdir).resolve()
    results = []
    for name in args.repos:
        url, ref = REPOS[name]
        ws = work / "repos" / name
        _clone(url, ref, ws)
        print(f"== {name}", file=sys.stderr)
        results.append(_bench_repo(name, ws, work / "state"))
    payload = {
        "kernodb": kerno.__version__,
        "sqlite": sqlite3.sqlite_version,
        "python": sys.version.split()[0],
        "results": results,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"kernodb {payload['kernodb']} vs SQLite {payload['sqlite']} (Python {payload['python']})\n"
    )
    for r in results:
        print(_report(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
