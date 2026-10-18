"""Queries behind `livespec ui`, over read-only connections.

Every function here takes a registry entry (or an id resolved to one), opens
that project's index with `connect_readonly`, and returns plain JSON-able
dicts. The numbers come from the same functions the MCP tools use
(`compute_coverage`, `compute_spec_test_coverage`, `compute_endpoints`,
`filter_api_endpoints`) called in-process on an `AppState` built around the
read-only connection — never from MCP tools over the wire, and never with
``record=True`` (the coverage trend snapshot is a write).

A failure in one project is returned as data (``{"error": ...}``) so one
broken index never takes the whole page down.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from livespec_mcp import registry
from livespec_mcp.config import Settings
from livespec_mcp.state import AppState
from livespec_mcp.storage.readonly import connect_readonly

# Same threshold the MCP surface uses for "implemented" vs "in progress".
_CONFIDENCE_THRESHOLD = 0.7
_NEIGHBOUR_LIMIT = 200
_SEARCH_LIMIT = 50
_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class NotFound(LookupError):
    """An id, spec or symbol the UI was asked for does not exist."""


def stable_id(value: str) -> str:
    """Short, URL-safe, stable id for a path (project root or group DB)."""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def group_display_name(db_path: str) -> str:
    p = Path(db_path)
    generic = {"docs", "group", "db", "index", "livespec", "shared"}
    if p.stem.lower() in generic and p.parent.name:
        return p.parent.name.lstrip(".") or p.stem
    return p.stem


# --------------------------------------------------------------------------
# Project catalogue (registry + members discovered inside group DBs)
# --------------------------------------------------------------------------


def _latest_run(conn: sqlite3.Connection, pid: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM index_run WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    return dict(row) if row else None


def _project_row(conn: sqlite3.Connection, root: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT id, name, root FROM project WHERE root=?", (root,)).fetchone()
    if row is not None:
        return row
    rows = conn.execute("SELECT id, name, root FROM project").fetchall()
    return rows[0] if len(rows) == 1 else None


def catalogue() -> list[dict[str, Any]]:
    """Every project the UI knows about, each with a stable ``id``.

    Registered workspaces first; then members of a group DB that were indexed
    from another machine or before the registry existed (``registered:
    false``) — a shared group DB already names its repos, so they belong in
    the group view even if this user never indexed them here.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in registry.list_projects():
        item = dict(entry)
        item["id"] = stable_id(item["path"])
        item["registered"] = True
        out.append(item)
        seen.add(item["path"])

    group_dbs = sorted({e["group"] for e in out if e.get("group")})
    for gdb in group_dbs:
        if not Path(gdb).is_file():
            continue
        try:
            conn = connect_readonly(gdb)
        except (OSError, sqlite3.Error):
            continue
        try:
            for row in conn.execute("SELECT id, name, root FROM project ORDER BY id"):
                root = str(row["root"])
                if root in seen:
                    continue
                seen.add(root)
                run = _latest_run(conn, int(row["id"]))
                snap = registry.snapshot_from_db(conn, int(row["id"]))
                item = {
                    "path": root,
                    "name": Path(root).name or root,
                    "db_path": gdb,
                    "group": gdb,
                    "last_indexed": (run or {}).get("finished_at") or (run or {}).get("started_at"),
                    "languages": snap["languages"],
                    "counts": snap["counts"],
                    "registered": False,
                    "id": stable_id(root),
                }
                item["status"] = "ok" if Path(root).is_dir() else "missing_workspace"
                out.append(item)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return out


def find_entry(project_id: str) -> dict[str, Any]:
    for entry in catalogue():
        if entry["id"] == project_id:
            return entry
    raise NotFound(f"unknown project id {project_id!r}")


@contextmanager
def open_project(entry: dict[str, Any]) -> Iterator[AppState]:
    """Read-only `AppState` for one catalogue entry, closed on exit."""
    if entry.get("status") == "missing_db":
        raise NotFound(f"index database is missing: {entry.get('db_path')}")
    conn = connect_readonly(entry["db_path"])
    try:
        row = _project_row(conn, entry["path"])
        if row is None:
            raise NotFound(f"{entry['path']} has no project row in {entry['db_path']}")
        root = Path(entry["path"])
        settings = Settings(
            workspace=root,
            state_dir=root / ".mcp-docs",
            db_path=Path(entry["db_path"]),
            docs_dir=root / ".mcp-docs" / "docs",
            grouped=bool(entry.get("group")),
        )
        # No ensure_dirs(): this state must never create anything.
        yield AppState(
            settings=settings, conn=conn, _lock=threading.Lock(), _project_id=int(row["id"])
        )
    finally:
        conn.close()


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "path",
        "db_path",
        "group",
        "status",
        "registered",
        "last_indexed",
        "languages",
        "counts",
    )
    out = {k: entry.get(k) for k in keys}
    out["group_id"] = stable_id(entry["group"]) if entry.get("group") else None
    out["group_name"] = group_display_name(entry["group"]) if entry.get("group") else None
    return out


def home() -> dict[str, Any]:
    """Projects grouped by polyrepo group, plus the ungrouped ones.

    Counts and languages are re-read from each live index (cheap COUNTs); the
    registry snapshot is only the fallback for an index that is gone.
    """
    projects: list[dict[str, Any]] = []
    for entry in catalogue():
        item = _public_entry(entry)
        if entry.get("status") != "missing_db":
            try:
                with open_project(entry) as st:
                    snap = registry.snapshot_from_db(st.conn, st.project_id)
                    item["languages"] = snap["languages"]
                    item["counts"] = snap["counts"]
                    run = _latest_run(st.conn, st.project_id)
                    if run:
                        item["last_indexed"] = run.get("finished_at") or run.get("started_at")
            except (NotFound, OSError, sqlite3.Error) as e:
                item["error"] = str(e)
        projects.append(item)

    groups: dict[str, dict[str, Any]] = {}
    ungrouped: list[dict[str, Any]] = []
    for p in projects:
        if p["group"]:
            g = groups.setdefault(
                p["group_id"],
                {
                    "id": p["group_id"],
                    "name": p["group_name"],
                    "db_path": p["group"],
                    "projects": [],
                },
            )
            g["projects"].append(p["id"])
        else:
            ungrouped.append(p)
    return {
        "registry_path": str(registry.registry_path()),
        "projects": projects,
        "groups": sorted(groups.values(), key=lambda g: g["name"].lower()),
        "ungrouped": [p["id"] for p in ungrouped],
    }


# --------------------------------------------------------------------------
# Coverage (the audit_coverage logic, never recorded)
# --------------------------------------------------------------------------

_COVERAGE_CACHE: dict[tuple, dict[str, Any]] = {}
_COVERAGE_LOCK = threading.Lock()


def _db_fingerprint(db_path: str) -> tuple:
    p = Path(db_path)
    out = []
    for f in (p, p.with_name(p.name + "-wal")):
        try:
            s = f.stat()
            out.append((s.st_mtime_ns, s.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _full_coverage(st: AppState) -> dict[str, Any]:
    from livespec_mcp.tools.analysis import compute_coverage

    run = _latest_run(st.conn, st.project_id)
    key = (
        str(st.settings.db_path),
        st.project_id,
        (run or {}).get("id"),
        _db_fingerprint(str(st.settings.db_path)),
    )
    with _COVERAGE_LOCK:
        hit = _COVERAGE_CACHE.get(key)
    if hit is not None:
        return hit
    cov = compute_coverage(st, record=False)
    with _COVERAGE_LOCK:
        if len(_COVERAGE_CACHE) > 64:
            _COVERAGE_CACHE.clear()
        _COVERAGE_CACHE[key] = cov
    return cov


def coverage_summary(st: AppState) -> dict[str, Any]:
    cov = _full_coverage(st)
    counts = cov["counts"]
    total = int(
        st.conn.execute(
            "SELECT COUNT(*) FROM spec WHERE project_id=?", (st.project_id,)
        ).fetchone()[0]
    )
    implemented = total - int(counts["specs_without_implementation"])
    return {
        "specs": total,
        "specs_implemented": implemented,
        # % of Specs with >= 1 implementing symbol (audit_coverage's
        # specs_without_implementation, inverted).
        "implemented_pct": round(implemented / total * 100, 1) if total else None,
        # Mean per-Spec REAL test coverage (call graph + explicit + reports).
        "avg_test_coverage_pct": (
            round(float(cov["avg_test_coverage"]) * 100, 1) if total else None
        ),
        "specs_with_test_coverage": int(counts["specs_with_derived_test_coverage"]),
        "specs_low_confidence": int(counts["specs_low_confidence"]),
        "modules_truly_orphan": int(counts["modules_truly_orphan"]),
        "modules_implicitly_covered": int(counts["modules_implicitly_covered"]),
        "modules_without_spec": int(counts["modules_without_spec"]),
    }


def project_coverage(project_id: str) -> dict[str, Any]:
    entry = find_entry(project_id)
    with open_project(entry) as st:
        return {"id": project_id, **coverage_summary(st)}


# --------------------------------------------------------------------------
# Project view
# --------------------------------------------------------------------------


def _dev_state(symbol_count: int, confidence: float | None, test_ratio: float) -> str:
    if symbol_count == 0:
        return "not_started"
    if test_ratio > 0:
        return "verified"
    if confidence is None or confidence < _CONFIDENCE_THRESHOLD:
        return "in_progress"
    return "implemented"


def project_overview(project_id: str) -> dict[str, Any]:
    entry = find_entry(project_id)
    out = _public_entry(entry)
    with open_project(entry) as st:
        snap = registry.snapshot_from_db(st.conn, st.project_id)
        out["languages"] = snap["languages"]
        out["counts"] = snap["counts"]
        run = _latest_run(st.conn, st.project_id)
        out["last_run"] = run
        if run:
            out["last_indexed"] = run.get("finished_at") or run.get("started_at")
        out["spec_kinds"] = {
            r[0]: int(r[1])
            for r in st.conn.execute(
                "SELECT kind, COUNT(*) FROM spec WHERE project_id=? GROUP BY kind ORDER BY 2 DESC",
                (st.project_id,),
            )
        }
        out["group_members"] = []
        if entry.get("group"):
            out["group_members"] = [
                {"name": Path(r["root"]).name, "root": r["root"], "id": stable_id(r["root"])}
                for r in st.conn.execute("SELECT root FROM project ORDER BY root")
            ]
    return out


def specs(project_id: str) -> dict[str, Any]:
    """Specs grouped by capability (module), with coverage per Spec."""
    from livespec_mcp.domain.graph import load_graph
    from livespec_mcp.tools.analysis import compute_spec_test_coverage

    entry = find_entry(project_id)
    with open_project(entry) as st:
        pid = st.project_id
        test_cov = compute_spec_test_coverage(st, load_graph(st.conn, pid))
        rows = st.conn.execute(
            """SELECT s.id, s.spec_id, s.title, s.kind, s.status, s.priority, s.source,
                      m.name AS module,
                      COUNT(ss.id) AS links,
                      AVG(ss.confidence) AS confidence,
                      (SELECT COUNT(*) FROM spec_scenario sc WHERE sc.spec_id=s.id) AS scenarios
               FROM spec s
               LEFT JOIN module m ON m.id=s.module_id
               LEFT JOIN spec_symbol ss ON ss.spec_id=s.id
               WHERE s.project_id=?
               GROUP BY s.id
               ORDER BY COALESCE(m.name, ''), s.spec_id""",
            (pid,),
        ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            tc = test_cov.get(r["spec_id"]) or {}
            ratio = float(tc.get("test_coverage_ratio") or 0.0)
            conf = round(float(r["confidence"]), 3) if r["confidence"] is not None else None
            groups.setdefault(r["module"] or "", []).append(
                {
                    "spec_id": r["spec_id"],
                    "title": r["title"],
                    "kind": r["kind"],
                    "status": r["status"],
                    "priority": r["priority"],
                    "source": r["source"],
                    "links": int(r["links"]),
                    "scenarios": int(r["scenarios"]),
                    "confidence": conf,
                    "test_coverage_pct": round(ratio * 100, 1),
                    "dev_state": _dev_state(int(r["links"]), conf, ratio),
                }
            )
        capabilities = [
            {"name": name or None, "specs": items}
            for name, items in sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0]))
        ]
        return {"id": project_id, "total": len(rows), "capabilities": capabilities}


def _spec_row(st: AppState, spec_id: str) -> sqlite3.Row:
    row = st.conn.execute(
        """SELECT s.*, m.name AS module FROM spec s LEFT JOIN module m ON m.id=s.module_id
           WHERE s.project_id=? AND s.spec_id=?""",
        (st.project_id, spec_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"spec {spec_id!r} not found")
    return row


def _project_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(r["id"]): Path(r["root"]).name for r in conn.execute("SELECT id, root FROM project")
    }


def _spec_tracks(st: AppState, spec_id: str) -> list[dict[str, Any]]:
    """@spec:ui-cross-project-read-only-ui

    Tracks (from CHANGESPEC.md, via the index) that touched this Spec,
    newest first, each ``changed`` or only ``touched``. An index built before
    tracks existed has no table: empty."""
    from livespec_mcp.domain.changespec import spec_aliases, spec_history

    try:
        items = spec_history(st.conn, st.project_id, spec_aliases(st.conn, st.project_id, spec_id))
    except sqlite3.OperationalError:
        return []
    return [
        {
            "track_id": t["track_id"],
            "date": t["date"],
            "title": t["title"],
            "why": t["why"],
            "range": f"{t['base_sha'][:7]}..{t['head_sha'][:7]}",
            "tier": t["tier"],
            "spec_why": t["spec_why"],
            "deltas": t["deltas"],
            "code": [
                {"change": change, "name": x["name"], "path": x["path"]}
                for change in ("added", "modified", "removed")
                for x in t["symbols"].get(change, [])
            ],
        }
        for t in items
    ]


def spec_detail(project_id: str, spec_id: str) -> dict[str, Any]:
    from livespec_mcp.domain.graph import load_graph
    from livespec_mcp.tools.analysis import compute_spec_test_coverage

    entry = find_entry(project_id)
    with open_project(entry) as st:
        spec = _spec_row(st, spec_id)
        names = _project_names(st.conn)
        symbols = [
            {
                "qname": r["qname"],
                "kind": r["kind"],
                "file": r["file"],
                "line": int(r["line"]),
                "signature": r["signature"],
                "relation": r["relation"],
                "confidence": round(float(r["confidence"]), 3),
                "source": r["source"],
                "project": names.get(int(r["project_id"])),
                "local": int(r["project_id"]) == st.project_id,
            }
            for r in st.conn.execute(
                """SELECT sym.qualified_name AS qname, sym.kind, sym.signature,
                          sym.start_line AS line, f.path AS file, f.project_id,
                          ss.relation, ss.confidence, ss.source
                   FROM spec_symbol ss
                   JOIN symbol sym ON sym.id=ss.symbol_id
                   JOIN file f ON f.id=sym.file_id
                   WHERE ss.spec_id=?
                   ORDER BY ss.relation, f.path, sym.start_line""",
                (int(spec["id"]),),
            )
        ]
        scenarios = []
        for sc in st.conn.execute(
            "SELECT id, name, body FROM spec_scenario WHERE spec_id=? ORDER BY ordinal, id",
            (int(spec["id"]),),
        ):
            scenarios.append(
                {
                    "name": sc["name"],
                    "body": sc["body"],
                    "symbols": [
                        {
                            "qname": r["qname"],
                            "file": r["file"],
                            "line": int(r["line"]),
                            "relation": r["relation"],
                        }
                        for r in st.conn.execute(
                            """SELECT sym.qualified_name AS qname, sym.start_line AS line,
                                      f.path AS file, x.relation
                               FROM scenario_symbol x
                               JOIN symbol sym ON sym.id=x.symbol_id
                               JOIN file f ON f.id=sym.file_id
                               WHERE x.scenario_id=? ORDER BY f.path, sym.start_line""",
                            (int(sc["id"]),),
                        )
                    ],
                }
            )
        depends_on = [
            {"spec_id": r["spec_id"], "title": r["title"], "kind": r["kind"]}
            for r in st.conn.execute(
                """SELECT c.spec_id, c.title, d.kind FROM spec_dependency d
                   JOIN spec c ON c.id=d.child_spec_id
                   WHERE d.parent_spec_id=? ORDER BY c.spec_id""",
                (int(spec["id"]),),
            )
        ]
        depended_by = [
            {"spec_id": r["spec_id"], "title": r["title"], "kind": r["kind"]}
            for r in st.conn.execute(
                """SELECT p.spec_id, p.title, d.kind FROM spec_dependency d
                   JOIN spec p ON p.id=d.parent_spec_id
                   WHERE d.child_spec_id=? ORDER BY p.spec_id""",
                (int(spec["id"]),),
            )
        ]
        tracks = _spec_tracks(st, spec_id)
        tc = compute_spec_test_coverage(st, load_graph(st.conn, st.project_id)).get(spec_id) or {}
        implements = [s for s in symbols if s["relation"] == "implements"]
        conf = round(sum(s["confidence"] for s in symbols) / len(symbols), 3) if symbols else None
        ratio = float(tc.get("test_coverage_ratio") or 0.0)
        # Same-id Specs in other repos of the group (the cross-repo mirror).
        mirrors = []
        if entry.get("group"):
            mirrors = [
                {"project": names.get(int(r["project_id"])), "id": stable_id(r["root"])}
                for r in st.conn.execute(
                    """SELECT s.project_id, p.root FROM spec s JOIN project p ON p.id=s.project_id
                       WHERE s.spec_id=? AND s.project_id != ? ORDER BY p.root""",
                    (spec_id, st.project_id),
                )
            ]
        return {
            "id": project_id,
            "spec_id": spec["spec_id"],
            "title": spec["title"],
            "description": spec["description"] or "",
            "kind": spec["kind"],
            "status": spec["status"],
            "priority": spec["priority"],
            "source": spec["source"],
            "module": spec["module"],
            "updated_at": spec["updated_at"],
            "symbols": symbols,
            "scenarios": scenarios,
            "depends_on": depends_on,
            "depended_by": depended_by,
            "mirrors": mirrors,
            "tracks": tracks,
            "coverage": {
                "implementing_symbols": len(implements),
                "confidence": conf,
                "test_coverage_pct": round(ratio * 100, 1),
                "tested_symbols": int(tc.get("tested_symbols") or 0),
                "total_symbols": int(tc.get("total_symbols") or len(implements)),
                "coverage_source": tc.get("coverage_source") or "none",
                "uncovered_symbols": list(tc.get("uncovered_symbols") or []),
                "uncovered_symbols_count": int(tc.get("uncovered_symbols_count") or 0),
                "dev_state": _dev_state(len(symbols), conf, ratio),
            },
        }


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_symbols(project_id: str, q: str, limit: int = _SEARCH_LIMIT) -> dict[str, Any]:
    q = (q or "").strip()
    entry = find_entry(project_id)
    if not q:
        return {"id": project_id, "q": q, "results": [], "total": 0}
    limit = max(1, min(int(limit), 200))
    pat = f"%{_like_escape(q)}%"
    with open_project(entry) as st:
        args = (st.project_id, pat, pat)
        where = """FROM symbol s JOIN file f ON f.id=s.file_id
                   WHERE f.project_id=? AND (s.name LIKE ? ESCAPE '\\'
                         OR s.qualified_name LIKE ? ESCAPE '\\')"""
        total = int(st.conn.execute(f"SELECT COUNT(*) {where}", args).fetchone()[0])
        rows = st.conn.execute(
            f"""SELECT s.qualified_name AS qname, s.name, s.kind, s.start_line AS line,
                       f.path AS file,
                       (SELECT COUNT(*) FROM spec_symbol ss WHERE ss.symbol_id=s.id) AS specs
                {where}
                ORDER BY (s.name = ?) DESC, (s.name LIKE ? ESCAPE '\\') DESC,
                         length(s.qualified_name), s.qualified_name
                LIMIT ?""",
            (*args, q, f"{_like_escape(q)}%", limit),
        ).fetchall()
        return {
            "id": project_id,
            "q": q,
            "total": total,
            "results": [
                {
                    "qname": r["qname"],
                    "name": r["name"],
                    "kind": r["kind"],
                    "file": r["file"],
                    "line": int(r["line"]),
                    "specs": int(r["specs"]),
                }
                for r in rows
            ],
        }


def _neighbours(
    st: AppState, symbol_id: int, *, incoming: bool, names: dict[int, str]
) -> tuple[list[dict[str, Any]], int]:
    near, far = ("dst", "src") if incoming else ("src", "dst")
    base = f"""FROM symbol_edge e
               JOIN symbol o ON o.id=e.{far}_symbol_id
               JOIN file f ON f.id=o.file_id
               WHERE e.{near}_symbol_id=?"""
    total = int(st.conn.execute(f"SELECT COUNT(*) {base}", (symbol_id,)).fetchone()[0])
    rows = st.conn.execute(
        f"""SELECT o.qualified_name AS qname, o.kind, o.start_line AS line, f.path AS file,
                   f.project_id, e.edge_type, e.origin
            {base}
            ORDER BY e.edge_type, f.path, o.start_line LIMIT ?""",
        (symbol_id, _NEIGHBOUR_LIMIT),
    ).fetchall()
    return (
        [
            {
                "qname": r["qname"],
                "kind": r["kind"],
                "file": r["file"],
                "line": int(r["line"]),
                "edge_type": r["edge_type"],
                "origin": r["origin"],
                "project": names.get(int(r["project_id"])),
                "local": int(r["project_id"]) == st.project_id,
            }
            for r in rows
        ],
        total,
    )


def symbol_detail(project_id: str, qname: str) -> dict[str, Any]:
    """One symbol and its call-graph neighbourhood (who calls / what it calls)."""
    entry = find_entry(project_id)
    with open_project(entry) as st:
        rows = st.conn.execute(
            """SELECT s.id, s.qualified_name AS qname, s.name, s.kind, s.signature, s.docstring,
                      s.decorators, s.visibility, s.start_line, s.end_line, f.path AS file,
                      f.language
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=?
               ORDER BY f.path, s.start_line""",
            (st.project_id, qname),
        ).fetchall()
        if not rows:
            raise NotFound(f"symbol {qname!r} not found")
        names = _project_names(st.conn)
        sym = rows[0]
        callers, callers_total = _neighbours(st, int(sym["id"]), incoming=True, names=names)
        callees, callees_total = _neighbours(st, int(sym["id"]), incoming=False, names=names)
        spec_links = [
            {
                "spec_id": r["spec_id"],
                "title": r["title"],
                "relation": r["relation"],
                "confidence": round(float(r["confidence"]), 3),
            }
            for r in st.conn.execute(
                """SELECT sp.spec_id, sp.title, ss.relation, ss.confidence
                   FROM spec_symbol ss JOIN spec sp ON sp.id=ss.spec_id
                   WHERE ss.symbol_id=? ORDER BY sp.spec_id""",
                (int(sym["id"]),),
            )
        ]
        return {
            "id": project_id,
            "qname": sym["qname"],
            "name": sym["name"],
            "kind": sym["kind"],
            "signature": sym["signature"],
            "docstring": sym["docstring"],
            "decorators": sym["decorators"],
            "visibility": sym["visibility"],
            "file": sym["file"],
            "language": sym["language"],
            "start_line": int(sym["start_line"]),
            "end_line": int(sym["end_line"]),
            "other_definitions": [
                {"file": r["file"], "line": int(r["start_line"])} for r in rows[1:]
            ],
            "specs": spec_links,
            "callers": callers,
            "callers_total": callers_total,
            "callees": callees,
            "callees_total": callees_total,
        }


def _endpoint_method_path(ep: dict[str, Any]) -> tuple[str | None, str | None]:
    method = ep.get("http_method") or ep.get("hono_method") or ep.get("express_method")
    path = ep.get("http_path") or ep.get("hono_path") or ep.get("express_path")
    return (method.upper() if isinstance(method, str) else None), (
        path if isinstance(path, str) else None
    )


def _framework(ep: dict[str, Any]) -> str | None:
    if ep.get("hono_method") is not None or ep.get("hono_path") is not None:
        return "hono"
    if ep.get("express_method") is not None or ep.get("express_path") is not None:
        return "express"
    if ep.get("http_method") is not None or ep.get("http_path") is not None:
        return str(ep.get("http_framework") or "http")
    if ep.get("ts_framework"):
        return str(ep["ts_framework"])
    if ep.get("django_cbv_base"):
        return "django"
    decorators = ep.get("decorators") or []
    for d in decorators:
        last = str(d).rsplit(".", 1)[-1]
        if last in {"tool", "resource", "prompt"}:
            return f"mcp {last}"
    return None


def endpoints(project_id: str) -> dict[str, Any]:
    """The project's entry points: the `find_endpoints` default sweep plus the
    call-style routers (Express, Hono, Spring) that sweep leaves to opt-in."""
    from livespec_mcp.tools.analysis import compute_endpoints, filter_api_endpoints

    entry = find_entry(project_id)
    with open_project(entry) as st:
        seen: set[tuple] = set()
        merged: list[dict[str, Any]] = []
        for fw in (None, "express", "hono", "spring"):
            try:
                found = compute_endpoints(st, framework=fw)
            except Exception:  # one framework scanner must not hide the rest
                continue
            for ep in filter_api_endpoints(found, fw):
                key = (ep.get("qualified_name"), *_endpoint_method_path(ep), ep.get("file_path"))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(ep)
        specs_by_qname: dict[str, list[str]] = {}
        for r in st.conn.execute(
            """SELECT sym.qualified_name AS qname, sp.spec_id FROM spec_symbol ss
               JOIN spec sp ON sp.id=ss.spec_id JOIN symbol sym ON sym.id=ss.symbol_id
               WHERE sp.project_id=?""",
            (st.project_id,),
        ):
            specs_by_qname.setdefault(r["qname"], []).append(r["spec_id"])
        items = []
        for ep in merged:
            method, path = _endpoint_method_path(ep)
            qn = ep.get("qualified_name") or ""
            items.append(
                {
                    "handler": qn,
                    "kind": ep.get("kind"),
                    "framework": _framework(ep),
                    "method": method,
                    "path": path,
                    "file": ep.get("file_path"),
                    "line": ep.get("start_line"),
                    "spec_ids": sorted(set(specs_by_qname.get(qn, []))),
                }
            )
        items.sort(
            key=lambda e: (e["path"] is None, e["path"] or "", e["file"] or "", e["line"] or 0)
        )
        return {"id": project_id, "total": len(items), "endpoints": items}


# --------------------------------------------------------------------------
# Group view (a shared group DB holding several repos)
# --------------------------------------------------------------------------


def _route_hops(conn: sqlite3.Connection, names: dict[int, str]) -> list[dict[str, Any]]:
    from livespec_mcp.domain.legacy_flows import is_infra_route_path

    try:
        rows = conn.execute(
            """SELECT sf.project_id AS from_pid, src.qualified_name AS from_symbol,
                      client.method AS method, client.path AS path,
                      df.project_id AS to_pid, dst.qualified_name AS to_symbol
               FROM symbol_edge e
               JOIN symbol src ON src.id=e.src_symbol_id
               JOIN file sf ON sf.id=src.file_id
               JOIN symbol dst ON dst.id=e.dst_symbol_id
               JOIN file df ON df.id=dst.file_id
               JOIN route_ref client ON client.symbol_id=src.id AND client.role='client'
               JOIN route_ref server ON server.symbol_id=dst.id AND server.role='server'
                    AND server.norm_path=client.norm_path
               WHERE e.edge_type='invokes_route'
               ORDER BY sf.project_id, src.qualified_name, client.path"""
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    seen = set()
    for r in rows:
        if is_infra_route_path(r["path"] or ""):
            continue
        key = (r["from_pid"], r["from_symbol"], r["method"], r["path"], r["to_pid"], r["to_symbol"])
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "from_project": names.get(int(r["from_pid"])),
                "from_symbol": r["from_symbol"],
                "method": r["method"],
                "path": r["path"],
                "to_project": names.get(int(r["to_pid"])),
                "to_symbol": r["to_symbol"],
                "cross_repo": int(r["from_pid"]) != int(r["to_pid"]),
            }
        )
    return out


def group(group_id: str) -> dict[str, Any]:
    entries = [e for e in catalogue() if e.get("group") and stable_id(e["group"]) == group_id]
    if not entries:
        raise NotFound(f"unknown group id {group_id!r}")
    db_path = entries[0]["group"]
    id_by_root = {e["path"]: e["id"] for e in entries}
    conn = connect_readonly(db_path)
    try:
        names = _project_names(conn)
        projects = []
        for r in conn.execute("SELECT id, root FROM project ORDER BY root"):
            pid = int(r["id"])
            snap = registry.snapshot_from_db(conn, pid)
            projects.append(
                {
                    "name": names[pid],
                    "path": r["root"],
                    "id": id_by_root.get(r["root"], stable_id(r["root"])),
                    "languages": snap["languages"],
                    "counts": snap["counts"],
                    "links": int(
                        conn.execute(
                            """SELECT COUNT(*) FROM spec_symbol ss JOIN spec s ON s.id=ss.spec_id
                               WHERE s.project_id=?""",
                            (pid,),
                        ).fetchone()[0]
                    ),
                }
            )

        # Cross-repo Specs: an xrepo-* id, or any spec id more than one repo holds.
        shared_ids = [
            r[0]
            for r in conn.execute(
                """SELECT spec_id FROM spec GROUP BY spec_id
                   HAVING spec_id LIKE 'xrepo-%' OR COUNT(DISTINCT project_id) > 1
                   ORDER BY spec_id"""
            )
        ]
        shared = []
        for sid in shared_ids:
            meta = conn.execute(
                "SELECT title, kind, status FROM spec WHERE spec_id=? ORDER BY project_id LIMIT 1",
                (sid,),
            ).fetchone()
            per_project = []
            for r in conn.execute(
                """SELECT s.project_id, p.root, COUNT(ss.id) AS links
                   FROM spec s JOIN project p ON p.id=s.project_id
                   LEFT JOIN spec_symbol ss ON ss.spec_id=s.id
                   WHERE s.spec_id=? GROUP BY s.project_id ORDER BY p.root""",
                (sid,),
            ):
                per_project.append(
                    {
                        "project": names.get(int(r["project_id"])),
                        "id": id_by_root.get(r["root"], stable_id(r["root"])),
                        "links": int(r["links"]),
                    }
                )
            depends_on = sorted(
                {
                    r[0]
                    for r in conn.execute(
                        """SELECT b.spec_id FROM spec_dependency d
                           JOIN spec a ON a.id=d.parent_spec_id
                           JOIN spec b ON b.id=d.child_spec_id WHERE a.spec_id=?""",
                        (sid,),
                    )
                }
            )
            shared.append(
                {
                    "spec_id": sid,
                    "title": meta["title"] if meta else sid,
                    "kind": meta["kind"] if meta else None,
                    "status": meta["status"] if meta else None,
                    "projects": per_project,
                    "depends_on": depends_on,
                }
            )
        hops = _route_hops(conn, names)
        cross_edges = int(
            conn.execute(
                """SELECT COUNT(*) FROM symbol_edge e
                   JOIN symbol a ON a.id=e.src_symbol_id JOIN file fa ON fa.id=a.file_id
                   JOIN symbol b ON b.id=e.dst_symbol_id JOIN file fb ON fb.id=b.file_id
                   WHERE fa.project_id != fb.project_id"""
            ).fetchone()[0]
        )
    finally:
        conn.close()
    return {
        "id": group_id,
        "name": group_display_name(db_path),
        "db_path": db_path,
        "projects": projects,
        "shared_specs": shared,
        "route_hops": hops,
        "cross_repo_edges": cross_edges,
    }


# --------------------------------------------------------------------------
# Registering an index that predates the registry
# --------------------------------------------------------------------------


def register_existing(path: str) -> dict[str, Any]:
    """Add an already-indexed workspace to the registry without re-indexing.

    Reads its index read-only; raises FileNotFoundError when it has none.
    """
    from livespec_mcp.state import workspace_db_path

    ws = Path(path).expanduser().resolve()
    if not ws.is_dir():
        raise FileNotFoundError(f"not a directory: {ws}")
    db = workspace_db_path(ws)
    if not db.is_file():
        raise FileNotFoundError(f"{ws} has no livespec index ({db}); run `livespec index {ws}`")
    grouped = db != ws / ".mcp-docs" / "docs.db"
    conn = connect_readonly(db)
    try:
        row = _project_row(conn, str(ws))
        if row is None:
            raise FileNotFoundError(f"{db} holds no project rooted at {ws}")
        snap = registry.snapshot_from_db(conn, int(row["id"]))
        run = _latest_run(conn, int(row["id"])) or {}
    finally:
        conn.close()
    return registry.record_workspace(
        ws,
        db,
        grouped=grouped,
        languages=snap["languages"],
        counts=snap["counts"],
        when=run.get("finished_at") or run.get("started_at"),
    )
