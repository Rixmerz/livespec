"""Cross-repo Flow Explorer — Spec/API map over a ``group_db``.

Writes ``flow-explorer/data.json`` + ``index.html`` next to the shared
group database (or under ``.mcp-docs/flow-explorer/`` for a solo repo).

Spec edges are driven by mirrored ``xrepo-*`` ids + ``spec_dependency``;
resolved HTTP route hops come from ``invokes_route`` graph edges.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from livespec_mcp.config import Settings
from livespec_mcp.state import AppState
from livespec_mcp.tools.analysis import compute_endpoints

_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# Spring @XxxMapping("…") — bound lengths (ReDoS-safe).
_SPRING_MAPPING = re.compile(
    r"@(Get|Post|Put|Patch|Delete)Mapping\s*\(\s*"
    r"(?:value\s*=\s*|path\s*=\s*)?"
    r"[\"'](/[^\"']{0,200})[\"']",
)
_SPRING_VERB = {
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Patch": "PATCH",
    "Delete": "DELETE",
}


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


def _spring_route_from_source(
    root: Path, file_path: str | None, start_line: int | None
) -> tuple[str | None, str | None]:
    """Best-effort method/path from Java ``@GetMapping("/x")`` near ``start_line``."""
    if not file_path or not start_line:
        return None, None
    abs_path = root / file_path
    if abs_path.suffix != ".java" or not abs_path.is_file():
        return None, None
    try:
        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    # Scan a small window above the method (annotations live there).
    lo = max(0, int(start_line) - 25)
    hi = min(len(lines), int(start_line))
    window = "\n".join(lines[lo:hi])[:4000]
    m = _SPRING_MAPPING.search(window)
    if not m:
        return None, None
    return _SPRING_VERB.get(m.group(1)), m.group(2)


def _endpoint_http_fields(
    ep: dict[str, Any], *, root: Path | None = None
) -> tuple[str | None, str | None]:
    method = (
        ep.get("http_method")
        or ep.get("hono_method")
        or ep.get("express_method")
    )
    path = ep.get("http_path") or ep.get("hono_path") or ep.get("express_path")
    if (not method or not path) and root is not None:
        sm, sp = _spring_route_from_source(
            root, ep.get("file_path"), ep.get("start_line")
        )
        method = method or sm
        path = path or sp
    if isinstance(method, str):
        method = method.upper()
    else:
        method = None
    if not isinstance(path, str):
        path = None
    return method, path


def _endpoint_entry(
    ep: dict[str, Any], project: str, *, root: Path | None = None
) -> dict[str, Any]:
    """Normalize compute_endpoints rows for the flow viewer."""
    method, path = _endpoint_http_fields(ep, root=root)
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


def _is_http_endpoint(ep: dict[str, Any], *, root: Path | None = None) -> bool:
    """Keep only endpoint rows with a concrete HTTP route (not ``*`` catch-alls)."""
    method, path = _endpoint_http_fields(ep, root=root)
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

    ``compute_endpoints(framework=None)`` skips Express/Hono (opt-in scanners).
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
    """Return resolved HTTP hops with endpoint metadata from route_ref."""
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
    except Exception:  # noqa: BLE001 — route migration may be absent
        return []
    return [
        {
            "from_project": name_by_id.get(int(row["from_pid"])),
            "from_symbol": row["from_symbol"],
            "method": row["method"],
            "path": row["path"],
            "to_project": name_by_id.get(int(row["to_pid"])),
            "to_symbol": row["to_symbol"],
        }
        for row in rows
    ]


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
                    if not _is_http_endpoint(ep, root=root):
                        continue
                    entry = _endpoint_entry(ep, pname, root=root)
                    if not _is_http_endpoint(entry):
                        continue
                    eps.append(entry)
                    all_endpoints.append(entry)
            except Exception as e:  # noqa: BLE001 — keep bundle resilient
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
    except Exception:  # noqa: BLE001
        route_ref_n = 0

    return {
        "meta": {
            "kind": "flow",
            "grouped": bool(st.settings.grouped),
            "group_db": str(st.settings.db_path) if st.settings.grouped else None,
            "caller_workspace": str(st.settings.workspace),
            "generated_at": generated_at,
            "counts": {
                "projects": len(project_summaries),
                "xrepo_specs": len(xrepo_specs),
                "endpoints": len(all_endpoints),
                "route_ref": route_ref_n,
            },
            "bridge_note": (
                "Cross-repo links include resolved HTTP route hops from the "
                "indexer."
                if route_edges
                else "No HTTP route hops resolved yet (need literal/env-resolvable "
                "client URLs matching server routes)."
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
.card .meta { color:var(--muted); font-size:12px; }
.note {
  background:var(--accent-weak); border-radius:8px; padding:10px 12px;
  margin-bottom:16px; color:var(--fg); font-size:13px;
}
.mermaid { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:16px; }
table { width:100%; border-collapse:collapse; background:var(--surface); }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.03em; }
.mono { font-family:var(--mono); font-size:12px; }
.chip {
  display:inline-block; padding:2px 8px; border-radius:999px; background:var(--accent-weak);
  font-size:11px; margin-right:4px;
}
.detail { margin-top:16px; background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:16px; }
.detail h2 { margin-top:0; font-size:16px; }
.sym { font-family:var(--mono); font-size:12px; display:block; padding:2px 0; }
.empty { color:var(--muted); }
a { color:var(--accent); }
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
    <div class="mermaid" id="flow-mermaid">Loading diagram…</div>
  </section>
  <section class="panel" data-panel="specs">
    <div class="grid" id="spec-grid"></div>
    <div class="detail" id="spec-detail"><p class="empty">Select a cross-repo spec.</p></div>
  </section>
  <section class="panel" data-panel="repos">
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

function renderMermaid() {
  const box = document.getElementById("flow-mermaid");
  const nodes = (DATA.flow_topology && DATA.flow_topology.nodes) || [];
  const edges = (DATA.flow_topology && DATA.flow_topology.edges) || [];
  if (!nodes.length) {
    box.textContent = "No topology nodes.";
    return;
  }
  const safe = (id) => String(id).replace(/[^a-zA-Z0-9_:]/g, "_");
  const lines = ["flowchart LR"];
  nodes.forEach((n) => {
    const sid = safe(n.id);
    const label = (n.label || n.id).replace(/"/g, "'");
    if (n.kind === "project") lines.push("  " + sid + "[\"" + label + "\"]");
    else lines.push("  " + sid + "(\"" + label + "\")");
  });
  edges.forEach((e) => {
    const a = safe(e.from);
    const b = safe(e.to);
    const arrow = e.kind === "route" ? "==>" : (e.kind === "implements" ? "-->" : "-.->");
    lines.push("  " + a + " " + arrow + "|" + (e.label || e.kind || "") + "| " + b);
  });
  const src = lines.join("\n");
  box.removeAttribute("data-processed");
  box.textContent = src;
  if (window.mermaid) {
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: "neutral" });
    window.mermaid.run({ nodes: [box] }).catch(() => {
      box.innerHTML = "<pre class=\"mono\">" + esc(src) + "</pre>";
    });
  }
}

function renderSpecs() {
  const grid = document.getElementById("spec-grid");
  grid.innerHTML = (DATA.xrepo_specs || []).map((s) => {
    const nProj = (s.projects || []).filter((p) => p.links > 0).length;
    return "<div class=\"card\" data-spec=\"" + esc(s.id) + "\">" +
      "<h3>" + esc(s.id) + "</h3>" +
      "<div class=\"meta\">" + esc(s.title || "") + "</div>" +
      "<div class=\"meta\">" + nProj + " repos linked · deps " +
      esc((s.depends_on || []).length) + "</div></div>";
  }).join("") || "<p class=\"empty\">No xrepo-* specs.</p>";
  grid.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", () => showSpec(card.getAttribute("data-spec")));
  });
}

function showSpec(id) {
  const s = (DATA.xrepo_specs || []).find((x) => x.id === id);
  const box = document.getElementById("spec-detail");
  if (!s) { box.innerHTML = "<p class=\"empty\">Not found.</p>"; return; }
  let h = "<h2>" + esc(s.id) + "</h2>";
  h += "<p>" + esc(s.title || "") + "</p>";
  h += "<p class=\"meta\">" + esc((s.description || "").slice(0, 500)) + "</p>";
  h += "<p>";
  (s.depends_on || []).forEach((d) => { h += "<span class=\"chip\">requires " + esc(d) + "</span>"; });
  (s.depended_by || []).forEach((d) => { h += "<span class=\"chip\">used by " + esc(d) + "</span>"; });
  h += "</p>";
  (s.projects || []).forEach((p) => {
    h += "<h3>" + esc(p.project) + " <span class=\"meta\">(" + p.links + " links)</span></h3>";
    if (!(p.symbols || []).length) {
      h += "<p class=\"empty\">Mirrored spec, no symbol links in this repo.</p>";
      return;
    }
    (p.symbols || []).forEach((sym) => {
      h += "<span class=\"sym\">" + esc(sym.relation) + " · " + esc(sym.qname) +
        " <span class=\"meta\">" + esc(sym.file) + ":" + esc(sym.line) + "</span></span>";
    });
  });
  box.innerHTML = h;
}

function renderRepos() {
  const grid = document.getElementById("repo-grid");
  grid.innerHTML = (DATA.projects || []).map((p) => {
    const ct = p.counts || {};
    const link = p.local_explorer
      ? "<p><a href=\"file://" + esc(p.local_explorer) + "\">Open local Spec Explorer</a></p>"
      : "";
    return "<div class=\"card\" style=\"cursor:default\">" +
      "<h3>" + esc(p.name) + "</h3>" +
      "<div class=\"meta\">" + esc(ct.symbols) + " symbols · " + esc(ct.specs) +
      " specs · " + esc(ct.links) + " links · " + esc(ct.endpoints) + " endpoints</div>" +
      link + "</div>";
  }).join("");
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
