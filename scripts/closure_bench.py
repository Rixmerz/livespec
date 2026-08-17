#!/usr/bin/env python3
"""Measure what reading by closure costs against reading by file.

F1 of the CodeLayer plan asks for "-30% tokens per task with no drop in success
rate". Success rate needs a human running real tasks; the token half can be
measured exactly, and this measures it.

**The sample is frozen, and that is the point.** The first version of this
comparison selected its 20 symbols by call fan-out, so the sample changed as
soon as the resolver changed and two runs were not comparable — the exact
methodology failure §11 of the plan warns about. Here the sample is ordered by
qualified name and pinned to a file, so a number from today can be set against
a number from next month.

**The baseline is what an agent actually does**, not a strawman. Asked to
change `charge_card`, an agent does not read the one file: it reads that file,
then the files its callees live in, because it needs their signatures. That is
the comparison — closure versus the file set an honest reader would open — and
it is why the numbers here are smaller than a file-count ratio would suggest.

Usage:
    python scripts/closure_bench.py [--sample scripts/closure_bench_sample.json]
                                    [--refresh] [--json]

`--refresh` re-picks and rewrites the frozen sample. Do it deliberately, and
never in the same change as a measurement you intend to compare.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livespec_mcp.domain.contract_closure import (
    build_closure,
    estimate_tokens,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = ROOT / "scripts" / "closure_bench_sample.json"
DEFAULT_DB = ROOT / ".mcp-docs" / "docs.db"
SAMPLE_SIZE = 20


def _connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        sys.exit(f"no index at {db} — run index_project first")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def pick_sample(conn: sqlite3.Connection, n: int = SAMPLE_SIZE) -> list[str]:
    """Deterministic sample: real functions with callees, ordered by name.

    Ordered by name rather than by size or fan-out so that re-running after an
    indexer change compares the same symbols. Bounded to bodies between 15 and
    120 lines: below that a closure proves nothing, above it the symbol has
    problems a reading strategy cannot fix.
    """
    rows = conn.execute(
        "SELECT s.qualified_name FROM symbol s "
        "WHERE s.kind IN ('function', 'method') "
        "  AND (s.end_line - s.start_line) BETWEEN 15 AND 120 "
        "  AND EXISTS (SELECT 1 FROM symbol_edge e WHERE e.src_symbol_id = s.id) "
        "ORDER BY s.qualified_name LIMIT ?",
        (n,),
    ).fetchall()
    return [r["qualified_name"] for r in rows]


def _symbol(conn: sqlite3.Connection, qname: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.id, s.qualified_name, s.kind, s.signature, s.start_line, "
        "       s.end_line, f.path AS file_path "
        "FROM symbol s JOIN file f ON f.id = s.file_id "
        "WHERE s.qualified_name = ? LIMIT 1",
        (qname,),
    ).fetchone()


def _file_tokens(paths: set[str]) -> int:
    total = 0
    for rel in paths:
        try:
            total += estimate_tokens((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return total


def measure(conn: sqlite3.Connection, qnames: list[str]) -> list[dict]:
    pids = tuple(r[0] for r in conn.execute("SELECT id FROM project"))
    out: list[dict] = []
    for qname in qnames:
        sym = _symbol(conn, qname)
        if sym is None:
            out.append({"qname": qname, "missing": True})
            continue

        cl = build_closure(conn, pids, sym, ROOT, depth=1, token_budget=2000)
        payload = cl.as_dict()

        # The honest baseline: the symbol's own file plus every file a callee
        # lives in. An agent reading by path opens all of them to get the
        # signatures the closure hands over directly.
        files = {cl.file_path} | {c.file_path for c in cl.calls}
        baseline = _file_tokens(files)
        closure = payload["budget"]["estimated_tokens"]

        out.append({
            "qname": qname,
            "closure_tokens": closure,
            "file_tokens": baseline,
            "files_opened": len(files),
            "saving": round(1 - closure / baseline, 3) if baseline else None,
            "degraded": payload["budget"]["degraded"],
            "unresolved": len(payload["unresolved_types"]),
            "covered": bool(payload["covered_by"]),
        })
    return out


def report(rows: list[dict]) -> dict:
    live = [r for r in rows if not r.get("missing")]
    if not live:
        sys.exit("sample resolved to nothing — is the index stale?")
    savings = [r["saving"] for r in live if r["saving"] is not None]
    closure = [r["closure_tokens"] for r in live]
    return {
        "symbols": len(live),
        "missing_from_index": len(rows) - len(live),
        "closure_tokens_median": int(statistics.median(closure)),
        "closure_tokens_p90": sorted(closure)[int(len(closure) * 0.9) - 1],
        "closure_tokens_max": max(closure),
        "under_2k_budget": sum(1 for t in closure if t <= 2000),
        "median_saving_vs_files": round(statistics.median(savings), 3) if savings else None,
        "worst_saving": round(min(savings), 3) if savings else None,
        "degraded": sum(1 for r in live if r["degraded"]),
        "with_unresolved_types": sum(1 for r in live if r["unresolved"]),
        "without_covering_tests": sum(1 for r in live if not r["covered"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = _connect(args.db)

    if args.refresh or not args.sample.exists():
        qnames = pick_sample(conn)
        args.sample.parent.mkdir(parents=True, exist_ok=True)
        args.sample.write_text(
            json.dumps({"symbols": qnames}, indent=2) + "\n", encoding="utf-8"
        )
        print(f"froze {len(qnames)} symbols into {args.sample}", file=sys.stderr)
    else:
        qnames = json.loads(args.sample.read_text(encoding="utf-8"))["symbols"]

    rows = measure(conn, qnames)
    summary = report(rows)

    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=2))
        return 0

    print(f"sample: {args.sample.name}  ({summary['symbols']} symbols)")
    if summary["missing_from_index"]:
        print(f"  !! {summary['missing_from_index']} frozen symbols are no longer indexed")
    print()
    print(f"  closure tokens   median {summary['closure_tokens_median']}"
          f"  p90 {summary['closure_tokens_p90']}  max {summary['closure_tokens_max']}")
    print(f"  under 2k budget  {summary['under_2k_budget']}/{summary['symbols']}"
          f"   degraded: {summary['degraded']}")
    if summary["median_saving_vs_files"] is not None:
        print(f"  vs reading the files an agent would open:"
              f"  median {summary['median_saving_vs_files']:.0%} fewer tokens"
              f"  (worst case {summary['worst_saving']:.0%})")
    print()
    print(f"  closures with a real type gap:  {summary['with_unresolved_types']}")
    print(f"  symbols no test in the index covers:  "
          f"{summary['without_covering_tests']}/{summary['symbols']}")
    print()
    print("  Token count is half of F1. The other half — success rate — needs a")
    print("  human running real tasks; this cannot measure it and does not try.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
