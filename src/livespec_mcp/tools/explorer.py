"""RF Explorer bundle builder (livespec-docs plugin surface).

Emits a static, self-contained "RF Explorer" — a Swagger-UI-style bundle
auto-generated from the project's Requirements + call graph + endpoints +
coverage audit. Two artifacts land under ``<workspace>/.mcp-docs/explorer/``:

    data.json   machine-readable bundle (schema below)
    index.html  single self-contained viewer (data inlined; opens via file://)

The data layer REUSES the compute logic behind ``find_endpoints`` and
``audit_coverage`` (``compute_endpoints`` / ``compute_coverage`` in
``tools.analysis``) and reads RF / rf_symbol / rf_dependency directly —
no MCP round-trips, no duplicated SQL beyond the per-RF symbol join.

data.json schema:
    {
      "meta": {"project", "generated_at"|null,
               "counts": {"requirements", "symbols", "endpoints", "files"}},
      "requirements": [{"id", "title", "status", "description",
                        "symbols": [{"qname", "signature"|null, "file", "line"}],
                        "endpoints": [str], "depends_on": [str],
                        "coverage": float|null}],
      "rf_topology": {"nodes": [{"id", "title"}],
                      "edges": [{"from", "to", "kind"}]},
      "endpoints": [{"framework"|null, "handler", "signature"|null,
                     "path"|null, "method"|null, "rf_ids": [str]}],
      "coverage": {"orphan_modules": [str], "orphan_endpoints": [str],
                   "totals": {...}}
    }

Determinism: ``generated_at`` is the ONLY non-deterministic field and is
injectable (arg ``generated_at``, default None) so two runs on an
unchanged project produce byte-identical ``data.json`` except for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from livespec_mcp.state import AppState
from livespec_mcp.tools.analysis import compute_coverage, compute_endpoints


def _framework_of_endpoint(ep: dict[str, Any]) -> str | None:
    """Derive a human framework label from a compute_endpoints entry."""
    if ep.get("hono_method") is not None or ep.get("hono_path") is not None:
        return "hono"
    if ep.get("ts_framework"):
        return str(ep["ts_framework"])
    if ep.get("django_cbv_base"):
        return "django"
    return None


def compute_explorer_data(
    st: AppState,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the full RF Explorer data bundle for ``st``'s workspace.

    Pure read; reuses ``compute_endpoints`` + ``compute_coverage``. The
    returned dict matches the data.json schema documented in the module
    docstring. ``generated_at`` is passed through verbatim (default None)
    so callers control determinism.
    """
    conn = st.conn
    pid = st.project_id

    # --- Requirements + per-RF symbols (with signatures) ---------------
    rf_rows = conn.execute(
        """SELECT id, rf_id, title, description, status, priority
           FROM rf WHERE project_id=? ORDER BY rf_id""",
        (pid,),
    ).fetchall()

    # rf.id (internal pk) -> rf_id string, for topology edge resolution
    rfid_by_pk: dict[int, str] = {int(r["id"]): r["rf_id"] for r in rf_rows}

    # symbol qname -> set of rf_ids (for endpoint -> RF mapping). Built
    # from every rf_symbol link, regardless of relation.
    qname_to_rfids: dict[str, list[str]] = {}
    for r in conn.execute(
        """SELECT rf.rf_id AS rf_id, s.qualified_name AS qname
           FROM rf_symbol rs
           JOIN rf ON rf.id = rs.rf_id
           JOIN symbol s ON s.id = rs.symbol_id
           WHERE rf.project_id=?
           ORDER BY rf.rf_id, s.qualified_name""",
        (pid,),
    ):
        qname_to_rfids.setdefault(r["qname"], [])
        if r["rf_id"] not in qname_to_rfids[r["qname"]]:
            qname_to_rfids[r["qname"]].append(r["rf_id"])

    # depends_on edges (forward): parent -> child, by rf_id string
    depends_on: dict[str, list[str]] = {}
    topo_edges: list[dict[str, str]] = []
    for r in conn.execute(
        """SELECT parent_rf_id, child_rf_id, kind FROM rf_dependency
           WHERE parent_rf_id IN (SELECT id FROM rf WHERE project_id=?)
           ORDER BY parent_rf_id, child_rf_id, kind""",
        (pid,),
    ):
        parent = rfid_by_pk.get(int(r["parent_rf_id"]))
        child = rfid_by_pk.get(int(r["child_rf_id"]))
        if parent is None or child is None:
            continue
        depends_on.setdefault(parent, [])
        if child not in depends_on[parent]:
            depends_on[parent].append(child)
        topo_edges.append({"from": parent, "to": child, "kind": r["kind"]})

    requirements: list[dict[str, Any]] = []
    total_rf_symbols = 0
    for rf in rf_rows:
        sym_rows = conn.execute(
            """SELECT s.qualified_name AS qname, s.signature, f.path AS file,
                      s.start_line AS line, rs.relation, rs.confidence
               FROM rf_symbol rs
               JOIN symbol s ON s.id = rs.symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE rs.rf_id = ?
               ORDER BY rs.confidence DESC, s.qualified_name, s.start_line""",
            (int(rf["id"]),),
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
        total_rf_symbols += len(symbols)
        # Coverage signal: avg confidence of this RF's links, or None when
        # there are no links (unimplemented).
        if sym_rows:
            coverage: float | None = round(
                sum(float(sr["confidence"]) for sr in sym_rows) / len(sym_rows), 4
            )
        else:
            coverage = None
        # Endpoints owned by this RF: endpoint handler qnames linked to it.
        rf_id = rf["rf_id"]
        owned_endpoints = sorted(
            {
                sr["qname"]
                for sr in sym_rows
                if rf_id in qname_to_rfids.get(sr["qname"], [])
            }
        )
        requirements.append(
            {
                "id": rf_id,
                "title": rf["title"],
                "status": rf["status"],
                "description": rf["description"] or "",
                "symbols": symbols,
                "endpoints": owned_endpoints,
                "depends_on": sorted(depends_on.get(rf_id, [])),
                "coverage": coverage,
            }
        )

    # --- Endpoints (full surface, framework-aware) ---------------------
    raw_endpoints = compute_endpoints(st, framework=None)
    endpoints: list[dict[str, Any]] = []
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
        endpoints.append(
            {
                "framework": _framework_of_endpoint(ep),
                "handler": handler,
                "signature": sig_by_qname.get(handler),
                "path": ep.get("hono_path"),
                "method": ep.get("hono_method"),
                "rf_ids": list(qname_to_rfids.get(handler, [])),
            }
        )

    # --- Coverage / orphans --------------------------------------------
    cov = compute_coverage(st)
    orphan_endpoints = sorted(
        {ep["handler"] for ep in endpoints if not ep["rf_ids"] and ep["handler"]}
    )
    coverage_section = {
        "orphan_modules": list(cov["modules_truly_orphan"]),
        "orphan_endpoints": orphan_endpoints,
        "totals": dict(cov["counts"]),
    }

    # --- Topology nodes ------------------------------------------------
    topology = {
        "nodes": [{"id": r["rf_id"], "title": r["title"]} for r in rf_rows],
        "edges": topo_edges,
    }

    files_count = conn.execute(
        "SELECT COUNT(*) c FROM file WHERE project_id=?", (pid,)
    ).fetchone()["c"]

    return {
        "meta": {
            "project": st.settings.workspace.name,
            "generated_at": generated_at,
            "counts": {
                "requirements": len(rf_rows),
                "symbols": total_rf_symbols,
                "endpoints": len(endpoints),
                "files": int(files_count),
            },
        },
        "requirements": requirements,
        "rf_topology": topology,
        "endpoints": endpoints,
        "coverage": coverage_section,
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
) -> dict[str, Any]:
    """Compute + write data.json and index.html under .mcp-docs/explorer/.

    Returns ``{"data": <bundle>, "files_written": [<abs paths>]}``.
    """
    data = compute_explorer_data(st, generated_at=generated_at)
    out_dir: Path = st.settings.state_dir / "explorer"
    out_dir.mkdir(parents=True, exist_ok=True)

    data_path = out_dir / "data.json"
    html_path = out_dir / "index.html"
    data_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(_render_index_html(data), encoding="utf-8")

    return {
        "data": data,
        "files_written": [str(data_path), str(html_path)],
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
<title>RF Explorer · __PROJECT__</title>
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

  /* ---- Sidebar / RF spine ---- */
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
  .spine .rf {
    display: block; width: 100%; text-align: left; appearance: none; border: 0;
    font: inherit; cursor: pointer; color: inherit;
    padding: 9px 11px; margin: 1px 0; border-radius: 9px;
    background: transparent; position: relative;
    transition: background .13s ease;
  }
  .spine .rf:hover { background: var(--surface-2); }
  .spine .rf[aria-current="true"] {
    background: var(--accent-weak);
    box-shadow: inset 0 0 0 1px var(--accent-line);
  }
  .spine .rf[aria-current="true"]::before {
    content: ""; position: absolute; left: -1px; top: 9px; bottom: 9px;
    width: 3px; border-radius: 3px; background: var(--accent);
  }
  .spine .rf .top { display: flex; align-items: center; gap: 7px; }
  .spine .rf .rid {
    font-family: var(--mono); font-size: 11px; font-weight: 650;
    color: var(--accent-ink); letter-spacing: -.01em;
  }
  .spine .rf[aria-current="true"] .rid { color: var(--accent-ink); }
  .spine .rf .ti {
    font-size: 13px; font-weight: 530; margin-top: 2px; color: var(--fg-soft);
    line-height: 1.35;
  }
  .spine .rf[aria-current="true"] .ti { color: var(--fg); }
  .spine .rf .dot {
    width: 7px; height: 7px; border-radius: 50%; flex: none; margin-left: auto;
  }
  .spine .none { padding: 18px 16px; color: var(--muted); font-style: italic; font-size: 13px; }

  /* ---- Status dots & badges ---- */
  .dot.st-draft       { background: var(--faint); }
  .dot.st-approved    { background: var(--ok); }
  .dot.st-in_progress { background: var(--info); }
  .dot.st-done        { background: var(--ok); }
  .dot.st-deprecated  { background: var(--danger); }

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
    <span class="mark" aria-hidden="true">RF</span>
    <h1>
      <span class="kicker">RF Explorer</span>
      <span class="proj">__PROJECT__</span>
    </h1>
  </div>
  <div class="stats" id="stats" aria-label="Project totals"></div>
  <nav class="tabs" role="tablist" aria-label="Explorer views">
    <button role="tab" data-tab="requirements" aria-current="page">Requirements<span class="pill" id="pill-rf"></span></button>
    <button role="tab" data-tab="topology">Topology<span class="pill" id="pill-topo"></span></button>
    <button role="tab" data-tab="endpoints">Endpoints<span class="pill" id="pill-ep"></span></button>
    <button role="tab" data-tab="gaps">Gaps<span class="pill" id="pill-gap"></span></button>
  </nav>
</header>

<section class="panel active" data-panel="requirements" role="tabpanel" aria-label="Requirements">
  <div class="split">
    <aside class="spine" aria-label="Requirement spine">
      <div class="search">
        <label for="rf-filter" class="sr-only" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">Filter requirements</label>
        <input id="rf-filter" type="search" placeholder="Filter requirements…" autocomplete="off" spellcheck="false">
      </div>
      <div class="list" id="rfnav"></div>
    </aside>
    <main class="scroll pad" id="rfmain"><div class="empty">Select a requirement.</div></main>
  </div>
</section>
<section class="panel" data-panel="topology" role="tabpanel" aria-label="Topology" hidden>
  <main class="scroll pad">
    <div class="topo-bar">
      <div class="legend" aria-hidden="false">
        <span class="item"><span class="swatch linked"></span> Linked (has dependency edge)</span>
        <span class="item"><span class="swatch indep"></span> Independent (no edges)</span>
      </div>
      <span class="chip muted" id="topo-counts"></span>
    </div>
    <div class="mermaid" id="mermaid-graph"></div>
  </main>
</section>
<section class="panel" data-panel="endpoints" role="tabpanel" aria-label="Endpoints" hidden>
  <main class="scroll pad" id="epmain"></main>
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

// ---- Header stats + tab count pills ----
const counts = DATA.meta.counts;
el('stats').innerHTML = [
  ['requirements', 'Requirements'],
  ['symbols', 'Linked symbols'],
  ['endpoints', 'Endpoints'],
  ['files', 'Files'],
].map(([k, label]) =>
  `<div class="stat"><span class="n">${counts[k]}</span><span class="l">${label}</span></div>`
).join('');

const edgeCount = DATA.rf_topology.edges.length;
el('pill-rf').textContent = DATA.requirements.length;
el('pill-topo').textContent = DATA.rf_topology.nodes.length;
el('pill-ep').textContent = DATA.endpoints.length;
el('pill-gap').textContent =
  DATA.coverage.orphan_modules.length + DATA.coverage.orphan_endpoints.length;

// ---- Coverage meter helper ----
function covMeter(v) {
  if (v == null) return '<span class="chip muted">no implementation</span>';
  const pct = Math.round(v * 100);
  const tier = v >= 0.9 ? 'high' : (v >= 0.7 ? 'mid' : 'low');
  return `<span class="cov ${tier}" title="avg link confidence">` +
    `<span class="track"><span class="fill" style="width:${pct}%"></span></span>` +
    `<span class="v">${pct}% coverage</span></span>`;
}
const statusClass = s => 'st-' + String(s || 'draft').replace(/[^a-z_]/gi, '_').toLowerCase();

// ---- Requirements spine + detail ----
const nav = el('rfnav');
const rfmain = el('rfmain');
let activeRF = null;

function renderSpine(filter) {
  const q = (filter || '').trim().toLowerCase();
  nav.innerHTML = '';
  const matches = DATA.requirements.filter(rf =>
    !q || rf.id.toLowerCase().includes(q) || (rf.title || '').toLowerCase().includes(q) ||
    (rf.description || '').toLowerCase().includes(q));
  if (!DATA.requirements.length) {
    nav.innerHTML = '<div class="none">No requirements linked yet.<br>See the Endpoints &amp; Gaps tabs.</div>';
    return;
  }
  if (!matches.length) {
    nav.innerHTML = '<div class="none">No requirements match “' + esc(filter) + '”.</div>';
    return;
  }
  matches.forEach(rf => {
    const b = document.createElement('button');
    b.className = 'rf';
    b.type = 'button';
    b.setAttribute('aria-current', rf.id === activeRF ? 'true' : 'false');
    b.innerHTML =
      `<div class="top"><span class="rid">${esc(rf.id)}</span>` +
      `<span class="dot ${statusClass(rf.status)}" title="${esc(rf.status)}"></span></div>` +
      `<div class="ti">${esc(rf.title)}</div>`;
    b.addEventListener('click', () => selectRF(rf.id));
    nav.appendChild(b);
  });
}

function selectRF(id) {
  activeRF = id;
  nav.querySelectorAll('.rf').forEach(n => {
    const on = n.querySelector('.rid') && n.querySelector('.rid').textContent === id;
    n.setAttribute('aria-current', on ? 'true' : 'false');
  });
  const rf = DATA.requirements.find(r => r.id === id);
  if (!rf) return;

  let h = '<div class="detail-head">' +
    `<div class="eyebrow">${esc(rf.id)}</div>` +
    `<h2 class="title">${esc(rf.title)}</h2></div>`;

  h += '<div class="meta-row">' +
    `<span class="chip status ${rf.coverage != null ? 'accent' : 'muted'}"><span class="dot ${statusClass(rf.status)}"></span>${esc(rf.status)}</span>` +
    covMeter(rf.coverage) +
    `<span class="chip muted">${rf.symbols.length} symbol${rf.symbols.length === 1 ? '' : 's'}</span>` +
    '</div>';

  h += rf.description
    ? `<p class="desc">${esc(rf.description)}</p>`
    : '<p class="desc"><span class="empty">No description.</span></p>';

  // Implementing symbols → table
  h += '<div class="sec"><h3 class="sec-h">Implementing symbols' +
    `<span class="ct">${rf.symbols.length}</span></h3>`;
  if (rf.symbols.length) {
    h += '<div class="card"><table><thead><tr>' +
      '<th>Symbol</th><th>Signature</th><th>Location</th></tr></thead><tbody>';
    rf.symbols.forEach(s => {
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

  // Owned endpoints
  h += '<div class="sec"><h3 class="sec-h">Owned endpoints' +
    `<span class="ct">${rf.endpoints.length}</span></h3>`;
  h += rf.endpoints.length
    ? '<div class="clusterbox">' +
      rf.endpoints.map(e => `<span class="chip mono accent">${esc(e)}</span>`).join('') + '</div>'
    : '<div class="empty">None.</div>';
  h += '</div>';

  // Depends on → clickable chips
  h += '<div class="sec"><h3 class="sec-h">Depends on' +
    `<span class="ct">${rf.depends_on.length}</span></h3>`;
  if (rf.depends_on.length) {
    h += '<div class="clusterbox">' +
      rf.depends_on.map(d =>
        `<button type="button" class="chip dep" data-goto="${esc(d)}">${esc(d)}</button>`
      ).join('') + '</div>';
  } else {
    h += '<div class="empty">No dependencies — this requirement is independent.</div>';
  }
  h += '</div>';

  rfmain.innerHTML = h;
  rfmain.scrollTop = 0;
  rfmain.querySelectorAll('[data-goto]').forEach(btn =>
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-goto');
      if (DATA.requirements.some(r => r.id === target)) {
        el('rf-filter').value = '';
        renderSpine('');
        selectRF(target);
      }
    }));
}

el('rf-filter').addEventListener('input', e => renderSpine(e.target.value));
renderSpine('');
if (DATA.requirements.length) selectRF(DATA.requirements[0].id);

// ---- Topology (Mermaid, themed) ----
const safeId = s => s.replace(/[^A-Za-z0-9_]/g, '_');
function buildMermaid() {
  const t = DATA.rf_topology;
  // Which nodes touch an edge (linked) vs sit alone (independent).
  const linked = new Set();
  t.edges.forEach(e => { linked.add(e.from); linked.add(e.to); });
  let src = 'graph TD\\n';
  if (!t.nodes.length) { return src + '  empty["No requirements indexed"]\\n'; }
  t.nodes.forEach(n => {
    const nid = safeId(n.id);
    const label = (n.id + ': ' + (n.title || '')).replace(/"/g, "'");
    src += `  ${nid}["${label}"]\\n`;
    src += linked.has(n.id) ? `  class ${nid} linked\\n` : `  class ${nid} indep\\n`;
  });
  t.edges.forEach(e => {
    src += `  ${safeId(e.from)} -->|${(e.kind || 'requires')}| ${safeId(e.to)}\\n`;
  });
  // classDef: linked = accent-filled; independent = dashed/secondary.
  src += '  classDef linked fill:var(--mm-linked-fill),stroke:var(--mm-accent),stroke-width:1.5px,color:var(--mm-ink);\\n';
  src += '  classDef indep fill:var(--mm-indep-fill),stroke:var(--mm-faint),stroke-width:1.2px,stroke-dasharray:4 3,color:var(--mm-ink-soft);\\n';
  return src;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
let mermaidRendered = false;
async function renderTopology() {
  const t = DATA.rf_topology;
  const linkedCount = new Set();
  t.edges.forEach(e => { linkedCount.add(e.from); linkedCount.add(e.to); });
  const indep = t.nodes.length - linkedCount.size;
  el('topo-counts').textContent =
    `${t.nodes.length} requirements · ${edgeCount} edges · ${indep} independent`;
  if (mermaidRendered) return;
  mermaidRendered = true;

  // Bridge our CSS custom properties into vars Mermaid's classDef can read,
  // so the graph matches the active (light/dark) palette.
  document.documentElement.style.setProperty('--mm-linked-fill', cssVar('--accent-weak'));
  document.documentElement.style.setProperty('--mm-indep-fill', cssVar('--surface-2'));
  document.documentElement.style.setProperty('--mm-accent', cssVar('--accent'));
  document.documentElement.style.setProperty('--mm-faint', cssVar('--faint'));
  document.documentElement.style.setProperty('--mm-ink', cssVar('--fg'));
  document.documentElement.style.setProperty('--mm-ink-soft', cssVar('--muted'));

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
    const { svg } = await mermaid.render('rfgraph', buildMermaid());
    el('mermaid-graph').innerHTML = svg;
  } catch (err) {
    // Offline / CDN unavailable: show readable graph source, never blank.
    el('mermaid-graph').innerHTML =
      '<pre>' + esc(buildMermaid()) + '</pre>';
  }
}

// ---- Endpoints (grouped, Swagger-like) ----
function frameworkLabel(fw) {
  return fw ? fw : 'Decorator / MCP tools';
}
function buildEndpoints() {
  const groups = {};
  DATA.endpoints.forEach(ep => {
    const k = frameworkLabel(ep.framework);
    (groups[k] = groups[k] || []).push(ep);
  });
  const keys = Object.keys(groups).sort((a, b) => {
    // Real frameworks first, the catch-all decorator group last.
    const da = a === 'Decorator / MCP tools', db = b === 'Decorator / MCP tools';
    return da - db || a.localeCompare(b);
  });
  let h = '<p class="lead">Every discovered entry point, grouped by framework. ' +
    'Badges show which requirement(s) own each handler.</p>';
  if (!keys.length) { el('epmain').innerHTML = h + '<div class="empty">No endpoints found.</div>'; return; }
  keys.forEach(k => {
    const rows = groups[k];
    h += '<section class="epgroup"><div class="epgroup-h">' +
      `<span class="fw">${esc(k)}</span>` +
      `<span class="fwtag">${rows.length} handler${rows.length === 1 ? '' : 's'}</span></div>`;
    h += '<div class="card"><table><thead><tr>' +
      '<th>Handler</th><th>Signature</th><th>Route</th><th>RFs</th></tr></thead><tbody>';
    rows.forEach(ep => {
      const method = ep.method
        ? `<span class="method ${String(ep.method).toLowerCase()}">${esc(ep.method)}</span> ` : '';
      const route = (ep.method || ep.path)
        ? `${method}<span class="path">${esc(ep.path || '')}</span>`
        : '<span class="empty">—</span>';
      const rfs = ep.rf_ids.length
        ? ep.rf_ids.map(r => `<span class="chip accent" style="font-size:11px">${esc(r)}</span>`).join(' ')
        : '<span class="chip muted">unlinked</span>';
      h += '<tr>' +
        `<td><span class="qname">${esc(ep.handler)}</span></td>` +
        `<td>${ep.signature ? `<span class="sig">${esc(ep.signature)}</span>` : '<span class="empty">—</span>'}</td>` +
        `<td>${route}</td><td>${rfs}</td></tr>`;
    });
    h += '</tbody></table></div></section>';
  });
  el('epmain').innerHTML = h;
}

// ---- Gaps ----
const TOTAL_LABELS = {
  modules_without_rf: 'Modules without RF',
  modules_implicitly_covered: 'Implicitly covered',
  modules_truly_orphan: 'Truly orphan',
  modules_unsupported_language: 'Unsupported language',
  rfs_without_implementation: 'RFs w/o impl',
  rfs_low_confidence: 'RFs low confidence',
  rfs_with_test_coverage: 'RFs w/ tests',
};
function buildGaps() {
  const g = DATA.coverage;
  let h = '<p class="lead">Coverage gaps — code the requirement graph does not yet reach. ' +
    'Orphan modules and endpoints are candidates for new RF links.</p>';

  // KPI row from totals
  h += '<div class="kpis">';
  Object.entries(g.totals).forEach(([k, v]) => {
    const label = TOTAL_LABELS[k] || k.replace(/_/g, ' ');
    let cls = '';
    if (k === 'modules_truly_orphan' || k === 'modules_without_rf') cls = ' flag';
    if (k === 'rfs_with_test_coverage' && v > 0) cls = ' good';
    if (k === 'rfs_without_implementation' && v > 0) cls = ' flag';
    h += `<div class="kpi${cls}"><div class="n">${esc(v)}</div><div class="k">${esc(label)}</div></div>`;
  });
  h += '</div>';

  h += '<div class="sec"><h3 class="sec-h">Orphan modules' +
    `<span class="ct">${g.orphan_modules.length}</span></h3>`;
  h += g.orphan_modules.length
    ? '<ul class="orphan-list">' +
      g.orphan_modules.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>'
    : '<div class="empty">None — every module is reachable from a requirement.</div>';
  h += '</div>';

  h += '<div class="sec"><h3 class="sec-h">Orphan endpoints' +
    `<span class="ct">${g.orphan_endpoints.length}</span></h3>`;
  h += g.orphan_endpoints.length
    ? '<ul class="orphan-list">' +
      g.orphan_endpoints.map(m => `<li>${esc(m)}</li>`).join('') + '</ul>'
    : '<div class="empty">None — every endpoint is linked to a requirement.</div>';
  h += '</div>';

  el('gapmain').innerHTML = h;
}

// ---- Tab switching ----
document.querySelectorAll('nav.tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll('nav.tabs button').forEach(b =>
      b.setAttribute('aria-current', b === btn ? 'page' : 'false'));
    document.querySelectorAll('.panel').forEach(p => {
      const on = p.dataset.panel === tab;
      p.classList.toggle('active', on);
      p.hidden = !on;
    });
    if (tab === 'topology') renderTopology();
  });
});

buildEndpoints();
buildGaps();
</script>
</body>
</html>
"""
