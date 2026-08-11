"""Cross-repo Flow Explorer — Spec/API map over a ``group_db``.

Writes ``flow-explorer/data.json`` + ``index.html`` next to the shared
group database (or under ``.mcp-docs/flow-explorer/`` for a solo repo).

Spec edges are driven by mirrored ``xrepo-*`` ids + ``spec_dependency``;
resolved HTTP route hops come from ``invokes_route`` graph edges.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from livespec_mcp.config import Settings
from livespec_mcp.domain.legacy_flows import is_infra_route_path
from livespec_mcp.state import AppState
from livespec_mcp.tools.analysis import compute_endpoints, group_fields

_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _ephemeral_project_state(st: AppState, root: Path, project_id: int) -> AppState:
    """AppState for one group member that reuses ``st.conn`` (no LRU get_state).

    Calling ``get_state`` per repo would open N connections to the same
    ``group_db`` and evict the caller's state after ``_LRU_MAX`` (8), closing
    the connection mid-export ("Cannot operate on a closed database").
    """
    settings = Settings(
        workspace=root,
        state_dir=root / ".mcp-docs",
        db_path=st.settings.db_path,
        docs_dir=root / ".mcp-docs" / "docs",
        grouped=st.settings.grouped,
    )
    # Do not ensure_dirs / cache — read-only endpoint scan over existing index.
    return AppState(
        settings=settings,
        conn=st.conn,
        _lock=st._lock,
        _project_id=project_id,
    )


def _project_rows(st: AppState) -> list[dict[str, Any]]:
    return [
        {"id": int(r["id"]), "name": r["name"], "root": r["root"]}
        for r in st.conn.execute("SELECT id, name, root FROM project ORDER BY id")
    ]


def _endpoint_http_fields(ep: dict[str, Any]) -> tuple[str | None, str | None]:
    method = (
        ep.get("http_method")
        or ep.get("hono_method")
        or ep.get("express_method")
    )
    path = ep.get("http_path") or ep.get("hono_path") or ep.get("express_path")
    if isinstance(method, str):
        method = method.upper()
    else:
        method = None
    if not isinstance(path, str):
        path = None
    return method, path


def _endpoint_entry(ep: dict[str, Any], project: str) -> dict[str, Any]:
    """Normalize compute_endpoints rows for the flow viewer."""
    method, path = _endpoint_http_fields(ep)
    return {
        "project": project,
        "kind": ep.get("kind") or ep.get("ts_framework") or "other",
        "framework": (
            ep.get("http_framework")
            or ep.get("ts_framework")
            or ep.get("django_cbv_base")
        ),
        "handler": ep.get("qualified_name"),
        "file_path": ep.get("file_path"),
        "start_line": ep.get("start_line"),
        "method": method,
        "path": path,
        "signature": ep.get("signature"),
        "decorators": ep.get("decorators") or [],
    }


def _is_http_endpoint(ep: dict[str, Any]) -> bool:
    """Keep only endpoint rows with a concrete HTTP route (not ``*`` catch-alls)."""
    method, path = _endpoint_http_fields(ep)
    if method and path and path.startswith("/") and path != "*":
        return method in _HTTP_VERBS
    # Normalized viewer shape (already through _endpoint_entry).
    m2, p2 = ep.get("method"), ep.get("path")
    return (
        isinstance(m2, str)
        and m2.upper() in _HTTP_VERBS
        and isinstance(p2, str)
        and p2.startswith("/")
        and p2 != "*"
    )


def _iter_project_endpoints(pst: AppState, framework: str | None):
    """Union decorator endpoints with Express/Hono call-style scanners.

    ``compute_endpoints(framework=None)`` includes Express/Hono call-style
    routes (same as ``find_endpoints`` default).
    Flow Explorer always needs those for Node composers.
    """
    seen: set[tuple[Any, ...]] = set()
    frameworks: list[str | None]
    if framework is None:
        frameworks = [None, "express", "hono", "spring"]
    else:
        frameworks = [framework]
    for fw in frameworks:
        for ep in compute_endpoints(pst, framework=fw):
            key = (
                ep.get("qualified_name"),
                ep.get("http_method") or ep.get("express_method") or ep.get("hono_method"),
                ep.get("http_path") or ep.get("express_path") or ep.get("hono_path"),
                ep.get("file_path"),
                ep.get("start_line"),
            )
            if key in seen:
                continue
            seen.add(key)
            yield ep


def _route_edges(
    conn: Any, name_by_id: dict[int, str]
) -> list[dict[str, str | None]]:
    """Return resolved HTTP hops with endpoint metadata from route_ref.

    Drops infra paths (``/health``, ``/liveness``, …) that cross-match every
    service and drown the product flow in the Mermaid view.
    """
    try:
        rows = conn.execute(
            """SELECT src_project.id AS from_pid,
                      src.qualified_name AS from_symbol,
                      client.method AS method, client.path AS path,
                      dst_project.id AS to_pid,
                      dst.qualified_name AS to_symbol
               FROM symbol_edge edge
               JOIN symbol src ON src.id=edge.src_symbol_id
               JOIN file src_file ON src_file.id=src.file_id
               JOIN project src_project ON src_project.id=src_file.project_id
               JOIN symbol dst ON dst.id=edge.dst_symbol_id
               JOIN file dst_file ON dst_file.id=dst.file_id
               JOIN project dst_project ON dst_project.id=dst_file.project_id
               JOIN route_ref client
                 ON client.symbol_id=src.id AND client.role='client'
               JOIN route_ref server
                 ON server.symbol_id=dst.id AND server.role='server'
                    AND server.norm_path=client.norm_path
               WHERE edge.edge_type='invokes_route'
               ORDER BY src_project.id, src.qualified_name, client.path,
                        dst_project.id, dst.qualified_name"""
        )
    except Exception:
        return []
    out: list[dict[str, str | None]] = []
    for row in rows:
        path = row["path"] or ""
        if is_infra_route_path(path):
            continue
        out.append(
            {
                "from_project": name_by_id.get(int(row["from_pid"])),
                "from_symbol": row["from_symbol"],
                "method": row["method"],
                "path": row["path"],
                "to_project": name_by_id.get(int(row["to_pid"])),
                "to_symbol": row["to_symbol"],
            }
        )
    return out


def compute_flow_explorer_data(
    st: AppState,
    *,
    generated_at: str | None = None,
    framework: str | None = None,
) -> dict[str, Any]:
    """Aggregate projects + xrepo specs + endpoints across the group DB."""
    conn = st.conn
    projects = _project_rows(st)
    name_by_id = {p["id"]: Path(p["root"]).name for p in projects}

    project_summaries: list[dict[str, Any]] = []
    all_endpoints: list[dict[str, Any]] = []

    for p in projects:
        pid = p["id"]
        pname = name_by_id[pid]
        root = Path(p["root"])
        n_files = conn.execute(
            "SELECT COUNT(*) FROM file WHERE project_id=?", (pid,)
        ).fetchone()[0]
        n_sym = conn.execute(
            """SELECT COUNT(*) FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=?""",
            (pid,),
        ).fetchone()[0]
        n_spec = conn.execute(
            "SELECT COUNT(*) FROM spec WHERE project_id=?", (pid,)
        ).fetchone()[0]
        n_link = conn.execute(
            """SELECT COUNT(*) FROM spec_symbol ss
               JOIN spec s ON s.id=ss.spec_id WHERE s.project_id=?""",
            (pid,),
        ).fetchone()[0]
        n_xrepo = conn.execute(
            """SELECT COUNT(*) FROM spec
               WHERE project_id=? AND spec_id LIKE 'xrepo-%'""",
            (pid,),
        ).fetchone()[0]

        eps: list[dict[str, Any]] = []
        if root.is_dir():
            try:
                pst = _ephemeral_project_state(st, root, pid)
                for ep in _iter_project_endpoints(pst, framework):
                    if not _is_http_endpoint(ep):
                        continue
                    entry = _endpoint_entry(ep, pname)
                    if not _is_http_endpoint(entry):
                        continue
                    eps.append(entry)
                    all_endpoints.append(entry)
            except Exception as e:
                eps = [{"project": pname, "error": str(e)}]

        local_explorer = root / ".mcp-docs" / "explorer" / "index.html"
        project_summaries.append(
            {
                "id": pid,
                "name": pname,
                "root": str(root),
                "counts": {
                    "files": int(n_files),
                    "symbols": int(n_sym),
                    "specs": int(n_spec),
                    "links": int(n_link),
                    "xrepo_specs": int(n_xrepo),
                    "endpoints": len([e for e in eps if "error" not in e]),
                },
                "local_explorer": str(local_explorer) if local_explorer.is_file() else None,
            }
        )

    # --- xrepo specs (union across projects) ---
    xrepo_ids = [
        r[0]
        for r in conn.execute(
            """SELECT DISTINCT spec_id FROM spec
               WHERE spec_id LIKE 'xrepo-%' ORDER BY 1"""
        )
    ]
    xrepo_specs: list[dict[str, Any]] = []
    for xid in xrepo_ids:
        meta = conn.execute(
            """SELECT title, description, status, kind FROM spec
               WHERE spec_id=? ORDER BY project_id LIMIT 1""",
            (xid,),
        ).fetchone()
        by_project: list[dict[str, Any]] = []
        for r in conn.execute(
            """SELECT s.project_id AS pid, p.root AS root,
                      COUNT(ss.id) AS links
               FROM spec s
               JOIN project p ON p.id = s.project_id
               LEFT JOIN spec_symbol ss ON ss.spec_id = s.id
               WHERE s.spec_id=?
               GROUP BY s.project_id
               ORDER BY links DESC, s.project_id""",
            (xid,),
        ):
            pid = int(r["pid"])
            pname = name_by_id.get(pid, Path(r["root"]).name)
            symbols = [
                {
                    "qname": row["qname"],
                    "kind": row["kind"],
                    "file": row["path"],
                    "line": row["start_line"],
                    "relation": row["relation"],
                    "signature": row["signature"],
                }
                for row in conn.execute(
                    """SELECT sym.qualified_name AS qname, sym.kind, sym.signature,
                              sym.start_line, f.path, ss.relation
                       FROM spec_symbol ss
                       JOIN spec sp ON sp.id = ss.spec_id
                       JOIN symbol sym ON sym.id = ss.symbol_id
                       JOIN file f ON f.id = sym.file_id
                       WHERE sp.spec_id=? AND sp.project_id=?
                       ORDER BY ss.relation, sym.qualified_name
                       LIMIT 40""",
                    (xid, pid),
                )
            ]
            by_project.append(
                {
                    "project": pname,
                    "links": int(r["links"]),
                    "symbols": symbols,
                }
            )

        # deps: take from first project that has this spec row with edges
        depends_on: list[str] = []
        depended_by: list[str] = []
        for r in conn.execute(
            """SELECT b.spec_id AS child
               FROM spec_dependency d
               JOIN spec a ON a.id = d.parent_spec_id
               JOIN spec b ON b.id = d.child_spec_id
               WHERE a.spec_id=?
               GROUP BY b.spec_id""",
            (xid,),
        ):
            depends_on.append(r["child"])
        for r in conn.execute(
            """SELECT a.spec_id AS parent
               FROM spec_dependency d
               JOIN spec a ON a.id = d.parent_spec_id
               JOIN spec b ON b.id = d.child_spec_id
               WHERE b.spec_id=?
               GROUP BY a.spec_id""",
            (xid,),
        ):
            depended_by.append(r["parent"])

        xrepo_specs.append(
            {
                "id": xid,
                "title": meta["title"] if meta else xid,
                "description": meta["description"] if meta else "",
                "status": meta["status"] if meta else "active",
                "kind": meta["kind"] if meta else "functional_requirement",
                "projects": by_project,
                "depends_on": sorted(set(depends_on)),
                "depended_by": sorted(set(depended_by)),
            }
        )

    route_edges = _route_edges(conn, name_by_id)

    # --- topology for Mermaid ---
    nodes: list[dict[str, str]] = []
    edges: list[dict[str, str]] = []
    for p in project_summaries:
        nodes.append({"id": f"proj:{p['name']}", "label": p["name"], "kind": "project"})
    for xs in xrepo_specs:
        nodes.append({"id": f"spec:{xs['id']}", "label": xs["id"], "kind": "spec"})
        for dep in xs["depends_on"]:
            edges.append(
                {
                    "from": f"spec:{xs['id']}",
                    "to": f"spec:{dep}",
                    "kind": "requires",
                }
            )
        for bp in xs["projects"]:
            if bp["links"] > 0:
                edges.append(
                    {
                        "from": f"proj:{bp['project']}",
                        "to": f"spec:{xs['id']}",
                        "kind": "implements",
                    }
                )

    for route in route_edges:
        source, target = route["from_project"], route["to_project"]
        if source is None or target is None:
            continue
        method = f"{route['method']} " if route["method"] else ""
        edges.append(
            {
                "from": f"proj:{source}",
                "to": f"proj:{target}",
                "kind": "route",
                "label": f"{method}{route['path']}",
            }
        )

    # de-dupe edges
    seen: set[tuple[str, str, str]] = set()
    uniq_edges: list[dict[str, str]] = []
    for e in edges:
        key = (e["from"], e["to"], e["kind"])
        if key in seen:
            continue
        seen.add(key)
        uniq_edges.append(e)

    route_ref_n = 0
    try:
        route_ref_n = int(conn.execute("SELECT COUNT(*) FROM route_ref").fetchone()[0])
    except Exception:
        route_ref_n = 0

    return {
        "meta": {
            "kind": "flow",
            **group_fields(st),
            "caller_workspace": str(st.settings.workspace),
            "generated_at": generated_at,
            "counts": {
                "projects": len(project_summaries),
                "xrepo_specs": len(xrepo_specs),
                "endpoints": len(all_endpoints),
                "route_ref": route_ref_n,
            },
            "bridge_note": (
                (
                    "This workspace is not using group_db — Flow Explorer shows "
                    "this project only. Set [workspace] group_db for cross-repo hops. "
                )
                if not st.settings.grouped
                else ""
            )
            + (
                "Cross-repo links include resolved HTTP route hops from the "
                "indexer."
                if route_edges
                else "No HTTP route hops resolved yet (need literal/env-resolvable "
                "client URLs matching server routes."
            ),
        },
        "projects": project_summaries,
        "xrepo_specs": xrepo_specs,
        "flow_topology": {"nodes": nodes, "edges": uniq_edges},
        "endpoints": all_endpoints,
        "route_edges": route_edges,
    }


def flow_out_dir(st: AppState) -> Path:
    if st.settings.grouped:
        return st.settings.db_path.parent / "flow-explorer"
    return st.settings.state_dir / "flow-explorer"


def write_flow_explorer_bundle(
    st: AppState,
    *,
    generated_at: str | None = None,
    framework: str | None = None,
) -> dict[str, Any]:
    data = compute_flow_explorer_data(
        st, generated_at=generated_at, framework=framework
    )
    out = flow_out_dir(st)
    out.mkdir(parents=True, exist_ok=True)
    data_path = out / "data.json"
    html_path = out / "index.html"
    data_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(_render_flow_html(data), encoding="utf-8")
    return {
        "data": data,
        "files_written": [str(data_path), str(html_path)],
        "out_dir": str(out),
    }


def create_flow_host_app(flow_dir: Path, projects: list[dict[str, Any]] | None = None):
    """HTTP app: Flow Explorer at ``/`` + each repo Spec Explorer at ``/repos/<name>/explorer/``.

    Spec Explorer bundles need the ``/explorer`` path segment so their History
    router resolves (same as ``livespec explorer serve``).
    """
    from starlette.applications import Starlette
    from starlette.responses import RedirectResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    flow_dir = Path(flow_dir).resolve()
    if projects is None:
        data_path = flow_dir / "data.json"
        projects = []
        if data_path.is_file():
            projects = json.loads(data_path.read_text(encoding="utf-8")).get(
                "projects"
            ) or []

    repo_routes: list[Any] = []
    mounted: list[str] = []
    for p in projects:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        # Unreleased: the bundle on disk is the authority, `local_explorer` only a
        # hint. It records whether a bundle existed at *export* time, so a repo
        # whose Spec Explorer was generated after the last flow export used to
        # stay unmountable until the flow bundle was re-exported.
        candidates: list[Path] = []
        local = p.get("local_explorer")
        if local:
            candidates.append(Path(str(local)))
        root = p.get("root")
        if root:
            candidates.append(Path(str(root)) / ".mcp-docs" / "explorer" / "index.html")

        bundle: Path | None = None
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file():
                resolved = resolved.parent
            if resolved.is_dir() and (resolved / "index.html").is_file():
                bundle = resolved
                break
        if bundle is None:
            continue
        prefix = f"/repos/{name}/explorer"

        async def _redir(_request: Any, _prefix: str = prefix) -> Any:
            return RedirectResponse(url=_prefix + "/", status_code=307)

        repo_routes.append(Route(prefix, _redir, methods=["GET", "HEAD"]))
        repo_routes.append(
            Mount(
                prefix,
                app=StaticFiles(directory=str(bundle), html=True),
                name=f"repo-{name}",
            )
        )
        mounted.append(name)

    async def _root_index(_request: Any) -> Any:
        return RedirectResponse(url="/index.html", status_code=307)

    routes: list[Any] = [
        *repo_routes,
        Route("/", _root_index, methods=["GET", "HEAD"]),
        Mount("/", app=StaticFiles(directory=str(flow_dir), html=True), name="flow"),
    ]
    return Starlette(routes=routes), mounted


def serve_flow_explorer(
    flow_dir: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
) -> None:
    """Serve Flow Explorer with embedded per-repo Spec Explorer mounts."""
    import uvicorn

    flow_dir = Path(flow_dir).resolve()
    app, mounted = create_flow_host_app(flow_dir)
    print(f"Flow Explorer: http://{host}:{port}/", flush=True)
    print(
        f"Spec Explorers: /repos/<name>/explorer/ ({len(mounted)} mounted)",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


def _render_flow_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    # Escape </script> so inlined JSON cannot break out of the data block.
    payload = payload.replace("<", "\\u003c")
    title = data["meta"].get("caller_workspace") or "flow"
    group_name = Path(str(title)).name
    return _FLOW_HTML.replace("__TITLE__", group_name).replace("__DATA__", payload)


# Keep JS free of broken `...'; template bugs (see explorer.py call-shape fix).
_FLOW_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flow Explorer · __TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
:root {
  color-scheme: light dark;
  --bg:#f4f6fb; --surface:#fff; --line:#e2e6f0; --fg:#1a2030; --muted:#6b7488;
  --accent:#4c3dcf; --accent-weak:#ece9fb; --ok:#1f8a5b; --warn:#b06a00;
  --font: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0e1117; --surface:#161b24; --line:#2a3140; --fg:#e8ebf2; --muted:#8a93a6;
    --accent:#9b93f5; --accent-weak:#22233a; --ok:#4cc38a; --warn:#e0a445;
  }
}
* { box-sizing: border-box; }
body { margin:0; font:14px/1.5 var(--font); background:var(--bg); color:var(--fg); }
header { background:var(--surface); border-bottom:1px solid var(--line); padding:16px 20px 0; }
header h1 { margin:0 0 4px; font-size:20px; }
header .sub { color:var(--muted); margin:0 0 12px; max-width:900px; }
.stats { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:12px; }
.stat { background:var(--accent-weak); padding:8px 12px; border-radius:8px; }
.stat b { display:block; font-size:18px; }
nav { display:flex; gap:4px; }
nav button {
  border:0; background:transparent; padding:10px 14px; cursor:pointer;
  color:var(--muted); border-bottom:2px solid transparent; font:inherit;
}
nav button[aria-current="page"] { color:var(--accent); border-bottom-color:var(--accent); }
main { padding:20px; max-width:1200px; margin:0 auto; }
.panel { display:none; }
.panel.active { display:block; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px; }
.card {
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:14px; cursor:pointer;
}
.card:hover { border-color:var(--accent); }
.card h3 { margin:0 0 6px; font-size:15px; }
.card .meta, .meta { color:var(--muted); font-size:12px; }
.note {
  background:var(--accent-weak); border-radius:8px; padding:10px 12px;
  margin-bottom:16px; color:var(--fg); font-size:13px;
}
.mermaid, .mermaid-host { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:16px; overflow:auto; }
.legend {
  display:flex; flex-wrap:wrap; gap:10px 16px; margin:12px 0 0; font-size:12px; color:var(--muted);
}
.legend .swatch {
  display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px;
  vertical-align:-1px; border:1px solid transparent;
}
table { width:100%; border-collapse:collapse; background:var(--surface); }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; }
.mono { font-family:var(--mono); font-size:12px; }
.chip {
  display:inline-block; padding:2px 8px; border-radius:999px; background:var(--accent-weak);
  font-size:11px; margin-right:4px; margin-bottom:4px;
}
.chip.req { background:#e8eefc; color:#3b6fd8; }
.chip.used { background:#e3f4ec; color:#1f8a5b; }
@media (prefers-color-scheme: dark) {
  .chip.req { background:#1e2a44; color:#7aa2ff; }
  .chip.used { background:#243028; color:#4cc38a; }
}
.btn {
  appearance:none; border:1px solid var(--line); background:var(--surface); color:var(--accent);
  font:inherit; font-size:12px; padding:6px 10px; border-radius:8px; cursor:pointer;
}
.btn:hover { border-color:var(--accent); }
.btn.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
.btn.primary:hover { filter:brightness(1.08); }

.frame-overlay {
  position:fixed; inset:0; z-index:40; display:none; flex-direction:column;
  background:var(--bg);
}
.frame-overlay.open { display:flex; }
.frame-bar {
  display:flex; align-items:center; gap:10px; padding:10px 14px;
  background:var(--surface); border-bottom:1px solid var(--line); flex-shrink:0;
}
.frame-bar .title { font-weight:600; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.frame-overlay iframe { flex:1; width:100%; border:0; background:var(--surface); }

/* Cross-repo Specs: master-detail */
.specs-layout {
  display:grid; grid-template-columns:minmax(260px,340px) 1fr; gap:16px; align-items:start;
}
@media (max-width:900px) { .specs-layout { grid-template-columns:1fr; } }
.spec-list { display:flex; flex-direction:column; gap:8px; max-height:calc(100vh - 220px); overflow:auto; }
.spec-item {
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:12px 14px; cursor:pointer; text-align:left;
}
.spec-item:hover { border-color:var(--accent); }
.spec-item[aria-current="true"] {
  border-color:var(--accent); box-shadow:0 0 0 1px var(--accent);
}
.spec-item .slug { font-family:var(--mono); font-size:12px; color:var(--accent); margin:0 0 4px; }
.spec-item h3 { margin:0 0 6px; font-size:14px; font-weight:600; line-height:1.35; }
.spec-item .row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-top:8px; }
.spec-item .badge {
  display:inline-block; padding:2px 8px; border-radius:999px;
  background:var(--accent-weak); color:var(--accent); font-size:11px; font-weight:600;
}
.spec-item .badge.zero { opacity:0.55; }
.spec-item .repos { font-size:11px; color:var(--muted); }

.detail {
  background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:20px 22px;
  min-height:280px;
}
.detail .spec-head { margin-bottom:16px; padding-bottom:14px; border-bottom:1px solid var(--line); }
.detail .spec-head .slug {
  font-family:var(--mono); font-size:12px; color:var(--accent); letter-spacing:0.02em;
}
.detail .spec-head h2 { margin:6px 0 8px; font-size:1.35rem; line-height:1.3; font-weight:650; }
.detail .spec-head .desc {
  margin:10px 0 0; color:var(--muted); font-size:13px; line-height:1.55; white-space:pre-wrap;
}
.detail .chips { margin-top:12px; }
.detail .section-label {
  font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted);
  margin:18px 0 8px; font-weight:600;
}
.impl-block {
  margin-top:10px; padding:14px; border:1px solid var(--line); border-radius:10px;
  background:var(--bg);
}
.impl-block h3 { margin:0 0 10px; font-size:13px; display:flex; justify-content:space-between; gap:8px; }
.impl-block h3 .n { color:var(--accent); font-family:var(--mono); font-weight:600; }
.sym {
  font-family:var(--mono); font-size:12px; display:grid;
  grid-template-columns:72px 1fr; gap:6px 10px; padding:5px 0;
  border-top:1px solid var(--line);
}
.sym:first-of-type { border-top:0; }
.sym .rel { color:var(--accent); font-weight:600; }
.sym .loc { color:var(--muted); grid-column:2; font-size:11px; }
.mirror-note { margin-top:16px; font-size:12px; color:var(--muted); line-height:1.45; }
.empty { color:var(--muted); }
a { color:var(--accent); }
.repo-path { font-family:var(--mono); font-size:11px; color:var(--muted); word-break:break-all; margin:8px 0; }

</style>
</head>
<body>
<header>
  <h1>Flow Explorer · __TITLE__</h1>
  <p class="sub" id="bridge-note"></p>
  <div class="stats" id="stats"></div>
  <nav>
    <button type="button" data-tab="flow" aria-current="page">Flow</button>
    <button type="button" data-tab="specs">Cross-repo specs</button>
    <button type="button" data-tab="repos">Repos</button>
    <button type="button" data-tab="api">API surface</button>
  </nav>
</header>
<main>
  <section class="panel active" data-panel="flow">
    <div class="note">Project → Spec edges mean the repo has linked symbols for that xrepo id. Spec → Spec edges are <code>spec_dependency</code>. Solid route edges are resolved HTTP hops.</div>
    <div id="flow-mermaid" class="mermaid-host">Loading diagram…</div>
    <div class="legend" id="flow-legend" aria-label="Diagram legend"></div>
  </section>
  <section class="panel" data-panel="specs">
    <div class="note">Shared Specs across the group (<code>xrepo-*</code>). Left: pick a Spec. Right: title, deps, and <strong>only repos with code links</strong>.</div>
    <div class="specs-layout">
      <div class="spec-list" id="spec-grid" role="listbox" aria-label="Cross-repo Specs"></div>
      <div class="detail" id="spec-detail"><p class="empty">Select a Spec →</p></div>
    </div>
  </section>
  <section class="panel" data-panel="repos">
    <div class="note">Open each repo's Spec Explorer <strong>inside</strong> Flow (full UI). Served at <code>/repos/&lt;name&gt;/explorer/</code>.</div>
    <div class="grid" id="repo-grid"></div>
  </section>
  <section class="panel" data-panel="api">
    <p class="note">Endpoints discovered per repo (Express/decorators/etc.). Not a live hop graph.</p>
    <div style="overflow:auto"><table>
      <thead><tr><th>Project</th><th>Method</th><th>Path / kind</th><th>Handler</th><th>File</th></tr></thead>
      <tbody id="api-body"></tbody>
    </table></div>
  </section>
</main>
<div id="frame-overlay" class="frame-overlay" hidden>
  <div class="frame-bar">
    <button type="button" class="btn" id="frame-back">← Back to Flow</button>
    <span class="title" id="frame-title">Spec Explorer</span>
    <a class="btn" id="frame-popout" href="#" target="_blank" rel="noopener">Pop out</a>
  </div>
  <iframe id="explorer-frame" title="Spec Explorer"></iframe>
</div>
<script id="flow-data" type="application/json">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("flow-data").textContent);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({
  "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"
}[c]));

document.getElementById("bridge-note").textContent = DATA.meta.bridge_note || "";
const c = DATA.meta.counts || {};
document.getElementById("stats").innerHTML = [
  ["projects", "Projects"],
  ["xrepo_specs", "Cross-repo specs"],
  ["endpoints", "Endpoints"],
  ["route_ref", "route_ref rows"],
].map(([k, label]) => "<div class=\"stat\"><b>" + esc(c[k] ?? 0) + "</b>" + esc(label) + "</div>").join("");

function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => {
    b.setAttribute("aria-current", b.dataset.tab === name ? "page" : "false");
  });
  document.querySelectorAll(".panel").forEach((p) => {
    p.classList.toggle("active", p.dataset.panel === name);
  });
  if (name === "flow") renderMermaid();
}
document.querySelectorAll("nav button").forEach((b) => {
  b.addEventListener("click", () => showTab(b.dataset.tab));
});

// Mirror Spec Explorer sanitizers: Mermaid v10 breaks on `:` in IDs and on
// raw `{}` / `&<>"` inside edge/node labels (path templates like /list/{}/{}).
function safeId(s) {
  return String(s == null ? "" : s).replace(/[^A-Za-z0-9_]/g, "_");
}
function mermaidLabel(s) {
  return String(s == null ? "" : s)
    .replace(/\{([^}]{0,64})\}/g, ":$1")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v && !/^var\(/.test(v) ? v : fallback;
}
function flowPalette() {
  // Literal hex only — Mermaid classDef rejects CSS var().
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (dark) {
    return {
      projectFill: "#1e2a44", projectStroke: "#7aa2ff", projectInk: "#e8ebf2",
      specFill: "#243028", specStroke: "#4cc38a", specInk: "#e8ebf2",
      route: "#9b93f5", implements: "#7aa2ff", requires: "#8a93a6",
    };
  }
  return {
    projectFill: "#e8eefc", projectStroke: "#3b6fd8", projectInk: "#1a2030",
    specFill: "#e3f4ec", specStroke: "#1f8a5b", specInk: "#1a2030",
    route: "#4c3dcf", implements: "#3b6fd8", requires: "#8a93a6",
  };
}
function renderMermaid() {
  const box = document.getElementById("flow-mermaid");
  const legend = document.getElementById("flow-legend");
  const nodes = (DATA.flow_topology && DATA.flow_topology.nodes) || [];
  const edges = (DATA.flow_topology && DATA.flow_topology.edges) || [];
  if (!nodes.length) {
    box.textContent = "No topology nodes.";
    if (legend) legend.innerHTML = "";
    return;
  }
  const P = flowPalette();
  if (legend) {
    legend.innerHTML = [
      ["projectFill", "projectStroke", "Repo / project"],
      ["specFill", "specStroke", "Cross-repo Spec"],
      [null, "route", "HTTP hop (route)"],
      [null, "implements", "implements"],
      [null, "requires", "requires"],
    ].map(([fillKey, strokeKey, label]) => {
      const fill = fillKey ? P[fillKey] : "transparent";
      const stroke = P[strokeKey];
      return "<span><span class=\"swatch\" style=\"background:" + fill +
        ";border-color:" + stroke + "\"></span>" + esc(label) + "</span>";
    }).join("");
  }
  const lines = ["flowchart LR"];
  nodes.forEach((n) => {
    const sid = safeId(n.id);
    const label = mermaidLabel(n.label || n.id);
    lines.push("  " + sid + "[\"" + label + "\"]");
  });
  edges.forEach((e) => {
    const a = safeId(e.from);
    const b = safeId(e.to);
    const arrow = e.kind === "route" ? "==>" : (e.kind === "implements" ? "-->" : "-.->");
    let label = mermaidLabel(e.label || e.kind || "");
    label = label.replace(/\/+$/g, "");
    lines.push("  " + a + " " + arrow + "|\"" + label + "\"| " + b);
  });
  lines.push(
    "  classDef node_project fill:" + P.projectFill + ",stroke:" + P.projectStroke +
      ",stroke-width:2px,color:" + P.projectInk + ";"
  );
  lines.push(
    "  classDef node_spec fill:" + P.specFill + ",stroke:" + P.specStroke +
      ",stroke-width:2px,color:" + P.specInk + ";"
  );
  nodes.forEach((n) => {
    lines.push(
      "  class " + safeId(n.id) + " " + (n.kind === "project" ? "node_project" : "node_spec")
    );
  });
  edges.forEach((e, i) => {
    const stroke = e.kind === "route" ? P.route
      : (e.kind === "implements" ? P.implements : P.requires);
    const width = e.kind === "route" ? "2.5px" : "1.5px";
    lines.push("  linkStyle " + i + " stroke:" + stroke + ",stroke-width:" + width);
  });
  const src = lines.join("\n");
  box.className = "mermaid";
  box.removeAttribute("data-processed");
  box.textContent = src;
  if (window.mermaid) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: "base",
      flowchart: { curve: "basis", nodeSpacing: 36, rankSpacing: 48, padding: 12 },
      themeVariables: {
        primaryColor: P.projectFill,
        primaryTextColor: P.projectInk,
        primaryBorderColor: P.projectStroke,
        lineColor: P.implements,
        secondaryColor: P.specFill,
        tertiaryColor: cssVar("--bg", "#f4f6fb"),
        fontFamily: "system-ui, sans-serif",
      },
    });
    window.mermaid.run({ nodes: [box] }).catch((err) => {
      box.innerHTML =
        "<p class=\"empty\">Diagram failed to render — raw Mermaid below.</p>" +
        "<pre class=\"mono\">" + esc(src) + "</pre>" +
        (err && err.message ? "<p class=\"empty\">" + esc(err.message) + "</p>" : "");
    });
  }
}

function explorerUrl(projectName) {
  return "/repos/" + encodeURIComponent(projectName) + "/explorer/";
}
function openExplorer(projectName) {
  const url = explorerUrl(projectName);
  const overlay = document.getElementById("frame-overlay");
  const frame = document.getElementById("explorer-frame");
  const title = document.getElementById("frame-title");
  const pop = document.getElementById("frame-popout");
  if (!overlay || !frame) {
    window.open(url, "_blank");
    return;
  }
  title.textContent = "Spec Explorer · " + projectName;
  pop.href = url;
  frame.src = url;
  overlay.hidden = false;
  overlay.classList.add("open");
}
function closeExplorer() {
  const overlay = document.getElementById("frame-overlay");
  const frame = document.getElementById("explorer-frame");
  if (!overlay) return;
  overlay.classList.remove("open");
  overlay.hidden = true;
  if (frame) frame.src = "about:blank";
}
const _frameBack = document.getElementById("frame-back");
if (_frameBack) _frameBack.addEventListener("click", closeExplorer);
if (window.mermaid) window.mermaid.initialize({ startOnLoad: false, securityLevel: "loose" });

function renderSpecs() {
  const grid = document.getElementById("spec-grid");
  const specs = DATA.xrepo_specs || [];
  if (!specs.length) {
    grid.innerHTML = "<p class=\"empty\">No xrepo-* Specs in this group.</p>";
    return;
  }
  const ordered = specs.slice().sort((a, b) => {
    const la = (a.projects || []).filter((p) => p.links > 0).length;
    const lb = (b.projects || []).filter((p) => p.links > 0).length;
    return lb - la;
  });
  grid.innerHTML = ordered.map((s, i) => {
    const linked = (s.projects || []).filter((p) => p.links > 0);
    const implNames = linked.map((p) => p.project).slice(0, 3).join(", ");
    const more = linked.length > 3 ? " +" + (linked.length - 3) : "";
    const zero = linked.length === 0;
    return "<button type=\"button\" class=\"spec-item\" role=\"option\" data-spec=\"" +
      esc(s.id) + "\"" + (i === 0 ? " aria-current=\"true\"" : "") + ">" +
      "<div class=\"slug\">" + esc(s.id) + "</div>" +
      "<h3>" + esc(s.title || s.id) + "</h3>" +
      "<div class=\"row\">" +
      "<span class=\"badge" + (zero ? " zero" : "") + "\">" + linked.length +
      " with code</span>" +
      (implNames ? "<span class=\"repos\">" + esc(implNames + more) + "</span>" : "") +
      "</div></button>";
  }).join("");
  grid.querySelectorAll(".spec-item").forEach((card) => {
    card.addEventListener("click", () => {
      grid.querySelectorAll(".spec-item").forEach((c) => c.removeAttribute("aria-current"));
      card.setAttribute("aria-current", "true");
      showSpec(card.getAttribute("data-spec"));
    });
  });
  showSpec(ordered[0].id);
}

function showSpec(id) {
  const s = (DATA.xrepo_specs || []).find((x) => x.id === id);
  const box = document.getElementById("spec-detail");
  if (!s) { box.innerHTML = "<p class=\"empty\">Not found.</p>"; return; }
  const projects = s.projects || [];
  const linked = projects.filter((p) => p.links > 0);
  const mirrored = projects.filter((p) => !(p.links > 0));
  let h = "<div class=\"spec-head\">";
  h += "<div class=\"slug\">" + esc(s.id) + "</div>";
  h += "<h2>" + esc(s.title || s.id) + "</h2>";
  h += "<div class=\"meta mono\">" + esc(s.kind || "spec") + " · " + esc(s.status || "active") + "</div>";
  if (s.description) {
    h += "<p class=\"desc\">" + esc(String(s.description).slice(0, 900)) + "</p>";
  }
  if ((s.depends_on || []).length || (s.depended_by || []).length) {
    h += "<div class=\"chips\">";
    (s.depends_on || []).forEach((d) => {
      h += "<span class=\"chip req\">requires " + esc(d) + "</span>";
    });
    (s.depended_by || []).forEach((d) => {
      h += "<span class=\"chip used\">used by " + esc(d) + "</span>";
    });
    h += "</div>";
  }
  h += "</div>";

  h += "<div class=\"section-label\">Implementation</div>";
  if (!linked.length) {
    h += "<p class=\"empty\">No repo in this group has symbol links for this Spec yet.</p>";
  } else {
    linked.forEach((p) => {
      h += "<div class=\"impl-block\"><h3><span>" + esc(p.project) +
        "</span><span class=\"n\">" + p.links + " link" + (p.links === 1 ? "" : "s") +
        "</span></h3>";
      (p.symbols || []).forEach((sym) => {
        h += "<div class=\"sym\"><span class=\"rel\">" + esc(sym.relation) +
          "</span><span>" + esc(sym.qname) + "</span>" +
          "<span class=\"loc\">" + esc(sym.file) + ":" + esc(sym.line) + "</span></div>";
      });
      h += "<p style=\"margin-top:10px\"><button type=\"button\" class=\"btn primary\" data-open-explorer=\"" +
        esc(p.project) + "\">Open Spec Explorer</button></p>";
      h += "</div>";
    });
  }
  if (mirrored.length) {
    h += "<p class=\"mirror-note\">Mirrored without code in " + mirrored.length +
      " repo" + (mirrored.length === 1 ? "" : "s") + ": " +
      esc(mirrored.map((p) => p.project).join(", ")) + ".</p>";
  }
  box.innerHTML = h;
  box.querySelectorAll("[data-open-explorer]").forEach((btn) => {
    btn.addEventListener("click", () => openExplorer(btn.getAttribute("data-open-explorer")));
  });
}

function renderRepos() {
  const grid = document.getElementById("repo-grid");
  const byName = {};
  (DATA.projects || []).forEach((p) => { byName[p.name] = p; });
  grid.innerHTML = (DATA.projects || []).map((p) => {
    const ct = p.counts || {};
    const has = !!p.local_explorer;
    const actions = has
      ? "<p style=\"margin-top:10px\"><button type=\"button\" class=\"btn primary\" data-open-explorer=\"" +
        esc(p.name) + "\">Open Spec Explorer</button></p>"
      : "<p class=\"meta empty\">No Spec Explorer bundle for this repo</p>";
    return "<div class=\"card\" style=\"cursor:default\">" +
      "<h3>" + esc(p.name) + "</h3>" +
      "<div class=\"meta\">" + esc(ct.symbols) + " symbols · " + esc(ct.specs) +
      " specs · " + esc(ct.links) + " links · " + esc(ct.endpoints) + " endpoints</div>" +
      actions + "</div>";
  }).join("");
  grid.querySelectorAll("[data-open-explorer]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openExplorer(btn.getAttribute("data-open-explorer"));
    });
  });
}

function renderApi() {
  const body = document.getElementById("api-body");
  const rows = DATA.endpoints || [];
  if (!rows.length) {
    body.innerHTML = "<tr><td colspan=\"5\" class=\"empty\">No endpoints discovered.</td></tr>";
    return;
  }
  body.innerHTML = rows.map((e) => {
    if (e.error) {
      return "<tr><td>" + esc(e.project) + "</td><td colspan=\"4\" class=\"empty\">" +
        esc(e.error) + "</td></tr>";
    }
    const pathOrKind = e.path || e.kind || "—";
    return "<tr>" +
      "<td>" + esc(e.project) + "</td>" +
      "<td class=\"mono\">" + esc(e.method || "—") + "</td>" +
      "<td class=\"mono\">" + esc(pathOrKind) + "</td>" +
      "<td class=\"mono\">" + esc(e.handler || "") + "</td>" +
      "<td class=\"mono\">" + esc(e.file_path || "") +
      (e.start_line ? (":" + e.start_line) : "") + "</td></tr>";
  }).join("");
}

renderSpecs();
renderRepos();
renderApi();
showTab("flow");
</script>
</body>
</html>
"""
