// livespec ui — one page over every indexed project. No build step.
// Every node is built with textContent (h()); index data is never parsed as HTML.
"use strict";

const main = document.getElementById("main");
const crumbs = document.getElementById("crumbs");
const statusEl = document.getElementById("status");

// ---------------------------------------------------------------- helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function link(href, text, attrs) {
  return h("a", { href, "data-nav": true, ...(attrs || {}) }, text);
}

function announce(msg) {
  statusEl.textContent = "";
  window.setTimeout(() => { statusEl.textContent = msg; }, 30);
}

async function api(path, opts) {
  const res = await fetch(path, { headers: { Accept: "application/json" }, ...(opts || {}) });
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

function fmtInt(n) {
  return n === null || n === undefined ? "—" : Number(n).toLocaleString();
}

function fmtPct(n) {
  return n === null || n === undefined ? "—" : `${Number(n).toFixed(1).replace(/\.0$/, "")}%`;
}

function parseTs(ts) {
  if (!ts) return null;
  let s = String(ts);
  if (!s.includes("T")) s = s.replace(" ", "T");
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += "Z";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtWhen(ts) {
  const d = parseTs(ts);
  if (!d) return "never";
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const steps = [[60, "second"], [3600, "minute"], [86400, "hour"], [2592000, "day"], [31536000, "month"]];
  let unit = "year";
  let div = 31536000;
  for (let i = 0; i < steps.length; i++) {
    if (Math.abs(secs) < steps[i][0]) {
      unit = steps[i][1];
      div = i === 0 ? 1 : steps[i - 1][0];
      break;
    }
  }
  return rtf.format(-Math.round(secs / div), unit);
}

function timeEl(ts) {
  const d = parseTs(ts);
  return h("time", { datetime: d ? d.toISOString() : null, title: d ? d.toLocaleString() : null }, fmtWhen(ts));
}

function meter(pct, label) {
  const fill = h("span", { class: "fill" });
  fill.style.width = `${Math.max(0, Math.min(100, pct || 0))}%`;
  return h("div", { class: "meter" },
    h("span", { class: "bar", role: "img", "aria-label": `${label}: ${fmtPct(pct)}` }, fill),
    h("span", { "aria-hidden": "true" }, fmtPct(pct)));
}

function badge(kind, text) {
  return h("span", { class: `badge ${kind}` }, text || kind.replace(/_/g, " "));
}

const STATUS_TEXT = {
  ok: "indexed",
  missing_db: "index missing",
  missing_workspace: "folder missing",
};

function languagesChips(langs) {
  const entries = Object.entries(langs || {});
  if (!entries.length) return h("span", { class: "muted" }, "no files");
  return h("ul", { class: "chips", "aria-label": "Languages" },
    entries.map(([l, n]) => h("li", { class: "chip", title: `${n} files` }, `${l} ${n}`)));
}

function table(caption, columns, rows) {
  return h("div", { class: "table-wrap" },
    h("table", {},
      caption ? h("caption", {}, caption) : null,
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { scope: "col", class: c.num ? "num" : null }, c.label)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, columns.map((c) => h("td", { class: c.num ? "num" : null }, c.cell(r))))))));
}

function setCrumbs(items) {
  crumbs.replaceChildren(...items.map((it, i) => {
    const last = i === items.length - 1;
    return h("li", {}, last ? h("span", { "aria-current": "page" }, it.label) : link(it.href, it.label));
  }));
}

function clean(nodes) {
  return nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false);
}

function render(title, ...nodes) {
  document.title = title ? `${title} · livespec` : "livespec";
  main.replaceChildren(...clean(nodes));
}

function loading(what) {
  render(what, h("p", { class: "muted" }, `Loading ${what}…`));
}

function showError(err) {
  render("Error", h("h1", {}, "Something went wrong"), h("p", { class: "notice error", role: "alert" }, String(err.message || err)));
}

function projectHref(id, sub) { return `/p/${encodeURIComponent(id)}${sub || ""}`; }
function specHref(id, specId) { return projectHref(id, `/spec/${encodeURIComponent(specId)}`); }
function symbolHref(id, qname) { return projectHref(id, `/symbol?qname=${encodeURIComponent(qname)}`); }

function loc(file, line) { return h("span", { class: "mono muted" }, `${file}:${line}`); }

// ------------------------------------------------------------------- home

let firstRoute = true;
let homeView = "groups";
try { homeView = localStorage.getItem("livespec-ui-view") || "groups"; } catch (_) { /* private mode */ }

function projectCard(p, covCells) {
  const cov = h("dd", {}, h("span", { class: "muted" }, p.status === "ok" ? "computing…" : "—"));
  covCells.push([p, cov]);
  const actions = [];
  if (p.status !== "ok" && p.registered) {
    actions.push(h("button", { type: "button", class: "ghost", onclick: () => forget(p) }, `Forget ${p.name}`));
  }
  const title = p.status === "missing_db" ? h("span", {}, p.name) : link(projectHref(p.id), p.name);
  return h("article", { class: "card", "aria-labelledby": `t-${p.id}` },
    h("div", { class: "row" },
      h("h3", { id: `t-${p.id}` }, title),
      h("span", { class: "spacer" }),
      badge(p.status, STATUS_TEXT[p.status] || p.status),
      p.registered ? null : badge("unregistered", "via group DB")),
    h("div", { class: "path" }, p.path),
    languagesChips(p.languages),
    h("dl", {},
      h("dt", {}, "Symbols"), h("dd", {}, fmtInt(p.counts && p.counts.symbols)),
      h("dt", {}, "Specs"), h("dd", {}, fmtInt(p.counts && p.counts.specs)),
      h("dt", {}, "Coverage"), cov,
      h("dt", {}, "Last indexed"), h("dd", {}, timeEl(p.last_indexed))),
    p.error ? h("p", { class: "notice error" }, p.error) : null,
    actions.length ? h("div", { class: "row end" }, actions) : null);
}

async function fillCoverage(covCells) {
  for (const [p, cell] of covCells) {
    if (p.status !== "ok" || p.error) continue;
    try {
      const c = await api(`/api/p/${encodeURIComponent(p.id)}/coverage`);
      if (!c.specs) {
        cell.replaceChildren(h("span", { class: "muted" }, "no Specs"));
      } else {
        cell.replaceChildren(
          h("div", {}, meter(c.implemented_pct, "Specs implemented"), h("span", { class: "muted" }, `${c.specs_implemented}/${c.specs} Specs implemented · ${fmtPct(c.avg_test_coverage_pct)} tested`)));
      }
    } catch (e) {
      cell.replaceChildren(h("span", { class: "muted" }, `unavailable (${e.message})`));
    }
  }
}

async function forget(p) {
  const dlg = document.getElementById("confirm");
  document.getElementById("confirm-body").textContent =
    `Remove ${p.path} from the livespec registry? Nothing on disk is deleted; indexing it again brings it back.`;
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", async function onClose() {
    dlg.removeEventListener("close", onClose);
    if (dlg.returnValue !== "ok") return;
    try {
      await api("/api/forget", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Livespec-UI": "1", Accept: "application/json" },
        body: JSON.stringify({ path: p.path }),
      });
      announce(`${p.name} forgotten`);
      route();
    } catch (e) {
      announce(`Could not forget ${p.name}: ${e.message}`);
    }
  });
}

async function viewHome() {
  setCrumbs([{ label: "All projects" }]);
  loading("projects");
  const data = await api("/api/projects");
  const byId = Object.fromEntries(data.projects.map((p) => [p.id, p]));
  const filter = h("input", { type: "search", id: "filter", placeholder: "Filter by name or path", autocomplete: "off" });
  const body = h("div", {});
  const toggle = h("div", { class: "segmented", role: "group", "aria-label": "Group projects" },
    ["groups", "flat"].map((v) => h("button", {
      type: "button",
      "aria-pressed": String(homeView === v),
      onclick: () => {
        homeView = v;
        try { localStorage.setItem("livespec-ui-view", v); } catch (_) { /* ignore */ }
        draw();
      },
    }, v === "groups" ? "By group" : "All projects")));

  function matches(p) {
    const q = filter.value.trim().toLowerCase();
    return !q || p.name.toLowerCase().includes(q) || p.path.toLowerCase().includes(q);
  }

  function draw() {
    for (const b of toggle.querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String((b.textContent === "By group") === (homeView === "groups")));
    }
    const covCells = [];
    const sections = [];
    if (!data.projects.length) {
      sections.push(h("div", { class: "notice" },
        h("p", {}, "No indexed projects yet."),
        h("p", {}, "Index a repository (", h("code", {}, "livespec index /path/to/repo"),
          " or the MCP tool ", h("code", {}, "index_project"), ") and reload. An index made before the registry existed can be added with ",
          h("code", {}, "livespec ui --add /path/to/repo"), ".")));
    } else if (homeView === "groups") {
      for (const g of data.groups) {
        const members = g.projects.map((id) => byId[id]).filter(matches);
        if (!members.length) continue;
        sections.push(h("section", { class: "group", "aria-labelledby": `g-${g.id}` },
          h("header", {},
            h("h2", { id: `g-${g.id}` }, link(`/g/${encodeURIComponent(g.id)}`, `Group: ${g.name}`)),
            h("span", { class: "mono muted" }, g.db_path),
            h("span", { class: "muted" }, `${g.projects.length} repos`)),
          h("div", { class: "cards" }, members.map((p) => projectCard(p, covCells)))));
      }
      const solo = data.ungrouped.map((id) => byId[id]).filter(matches);
      if (solo.length) {
        sections.push(h("section", { "aria-labelledby": "g-solo" },
          h("h2", { id: "g-solo" }, data.groups.length ? "Ungrouped projects" : "Projects"),
          h("div", { class: "cards" }, solo.map((p) => projectCard(p, covCells)))));
      }
    } else {
      const all = data.projects.filter(matches);
      sections.push(h("div", { class: "cards" }, all.map((p) => projectCard(p, covCells))));
    }
    if (data.projects.length && !sections.length) sections.push(h("p", { class: "muted" }, "No project matches the filter."));
    body.replaceChildren(...sections);
    fillCoverage(covCells);
  }

  filter.addEventListener("input", draw);
  const total = data.projects.length;
  const missing = data.projects.filter((p) => p.status !== "ok").length;
  render("Projects",
    h("p", { class: "eyebrow" }, "livespec ui"),
    h("h1", {}, "Indexed projects"),
    h("p", { class: "muted" }, `${total} project${total === 1 ? "" : "s"} in ${data.groups.length} group${data.groups.length === 1 ? "" : "s"}`,
      missing ? ` · ${missing} unavailable` : "", " · registry ", h("code", {}, data.registry_path)),
    h("div", { class: "row" }, h("label", { for: "filter" }, "Filter"), filter, h("span", { class: "spacer" }), toggle),
    body);
  draw();
  announce(`${total} projects loaded`);
}

// ---------------------------------------------------------------- project

const TABS = [
  ["specs", "Specs"],
  ["symbols", "Symbols"],
  ["endpoints", "Endpoints"],
];

function tabs(id, active) {
  const list = h("div", { role: "tablist", "aria-label": "Project sections" },
    TABS.map(([key, label]) => h("button", {
      type: "button",
      role: "tab",
      id: `tab-${key}`,
      "aria-selected": String(key === active),
      "aria-controls": "panel",
      tabindex: key === active ? "0" : "-1",
      onclick: () => navigate(projectHref(id, key === "specs" ? "" : `/${key}`)),
    }, label)));
  list.addEventListener("keydown", (ev) => {
    const btns = [...list.querySelectorAll("[role=tab]")];
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (ev.key === "ArrowRight") j = (i + 1) % btns.length;
    if (ev.key === "ArrowLeft") j = (i - 1 + btns.length) % btns.length;
    if (ev.key === "Home") j = 0;
    if (ev.key === "End") j = btns.length - 1;
    if (j !== null) { ev.preventDefault(); btns[j].focus(); btns[j].click(); }
  });
  return list;
}

const overviewCache = new Map();
async function overview(id) {
  if (!overviewCache.has(id)) overviewCache.set(id, api(`/api/p/${encodeURIComponent(id)}`));
  try { return await overviewCache.get(id); } catch (e) { overviewCache.delete(id); throw e; }
}

function projectCrumbs(ov, extra) {
  const items = [{ label: "All projects", href: "/" }];
  if (ov.group_id) items.push({ label: `Group: ${ov.group_name}`, href: `/g/${encodeURIComponent(ov.group_id)}` });
  items.push({ label: ov.name, href: projectHref(ov.id) });
  setCrumbs(extra ? items.concat(extra) : items.map((it, i, a) => (i === a.length - 1 ? { label: it.label } : it)));
}

function projectHeader(ov, cov) {
  const c = ov.counts || {};
  return [
    h("p", { class: "eyebrow" }, ov.group_name ? `project · group ${ov.group_name}` : "project"),
    h("h1", {}, ov.name),
    h("p", { class: "mono muted" }, ov.path),
    languagesChips(ov.languages),
    h("div", { class: "stats" },
      [["Files", fmtInt(c.files)], ["Symbols", fmtInt(c.symbols)], ["Edges", fmtInt(c.edges)], ["Specs", fmtInt(c.specs)],
        ["Specs implemented", cov ? fmtPct(cov.implemented_pct) : "…"], ["Avg test coverage", cov ? fmtPct(cov.avg_test_coverage_pct) : "…"],
        ["Orphan modules", cov ? fmtInt(cov.modules_truly_orphan) : "…"]]
        .map(([k, v]) => h("div", { class: "stat" }, h("div", { class: "v" }, v), h("div", { class: "k" }, k))),
      h("div", { class: "stat" }, h("div", { class: "v" }, timeEl(ov.last_indexed)), h("div", { class: "k" }, "Last indexed"))),
  ];
}

async function projectPage(id, tab, panelBuilder) {
  loading("project");
  const ov = await overview(id);
  projectCrumbs(ov);
  const panel = h("section", { id: "panel", role: "tabpanel", "aria-labelledby": `tab-${tab}`, tabindex: "0" }, h("p", { class: "muted" }, "Loading…"));
  const head = h("div", {}, projectHeader(ov, null));
  render(ov.name, head, tabs(id, tab), panel);
  api(`/api/p/${encodeURIComponent(id)}/coverage`).then((cov) => head.replaceChildren(...projectHeader(ov, cov))).catch(() => {});
  try {
    panel.replaceChildren(...clean([await panelBuilder(ov)]));
  } catch (e) {
    panel.replaceChildren(h("p", { class: "notice error", role: "alert" }, e.message));
  }
}

async function specsPanel(id) {
  const data = await api(`/api/p/${encodeURIComponent(id)}/specs`);
  if (!data.total) {
    return h("div", { class: "notice" }, "This project has no Specs yet. Import them with ",
      h("code", {}, "import_specs_from_markdown"), " or ", h("code", {}, "sync_openspec"),
      ", or bootstrap with ", h("code", {}, "propose_specs_from_codebase"), ".");
  }
  announce(`${data.total} Specs`);
  return data.capabilities.map((cap) => h("section", { "aria-label": cap.name || "No capability" },
    h("h2", {}, cap.name ? `Capability: ${cap.name}` : "No capability", h("span", { class: "muted" }, ` (${cap.specs.length})`)),
    table(null, [
      { label: "Spec", cell: (s) => h("div", {}, link(specHref(id, s.spec_id), s.spec_id), h("div", { class: "muted" }, s.title)) },
      { label: "Kind", cell: (s) => s.kind.replace(/_/g, " ") },
      { label: "State", cell: (s) => badge(s.dev_state) },
      { label: "Symbols", num: true, cell: (s) => fmtInt(s.links) },
      { label: "Scenarios", num: true, cell: (s) => fmtInt(s.scenarios) },
      { label: "Test coverage", cell: (s) => meter(s.test_coverage_pct, `Test coverage of ${s.spec_id}`) },
    ], cap.specs)));
}

async function symbolsPanel(id, ov) {
  const params = new URLSearchParams(location.search);
  const input = h("input", { type: "search", id: "symq", value: params.get("q") || "", placeholder: "Name or qualified name", autocomplete: "off", "aria-describedby": "symhint" });
  const results = h("div", { "aria-live": "polite" });
  let timer = null;
  let seq = 0;
  async function run() {
    const q = input.value.trim();
    const url = new URL(location.href);
    if (q) url.searchParams.set("q", q); else url.searchParams.delete("q");
    history.replaceState(null, "", url);
    if (!q) { results.replaceChildren(h("p", { class: "muted" }, `Type to search ${fmtInt(ov.counts.symbols)} symbols.`)); return; }
    const mine = ++seq;
    const data = await api(`/api/p/${encodeURIComponent(id)}/symbols?q=${encodeURIComponent(q)}`);
    if (mine !== seq) return;
    if (!data.results.length) { results.replaceChildren(h("p", { class: "muted" }, `No symbol matches “${q}”.`)); return; }
    results.replaceChildren(table(`${data.results.length} of ${fmtInt(data.total)} matches`, [
      { label: "Symbol", cell: (s) => link(symbolHref(id, s.qname), s.qname, { class: "mono" }) },
      { label: "Kind", cell: (s) => s.kind },
      { label: "Location", cell: (s) => loc(s.file, s.line) },
      { label: "Specs", num: true, cell: (s) => fmtInt(s.specs) },
    ], data.results));
  }
  input.addEventListener("input", () => { window.clearTimeout(timer); timer = window.setTimeout(() => run().catch((e) => results.replaceChildren(h("p", { class: "notice error" }, e.message))), 200); });
  window.setTimeout(() => { input.focus(); run().catch(() => {}); }, 0);
  return [
    h("div", { class: "row" }, h("label", { for: "symq" }, "Search symbols"), input),
    h("p", { id: "symhint", class: "muted" }, "Matches anywhere in the name; exact names rank first."),
    results,
  ];
}

async function endpointsPanel(id) {
  const data = await api(`/api/p/${encodeURIComponent(id)}/endpoints`);
  if (!data.total) return h("p", { class: "muted" }, "No endpoints detected in this project.");
  announce(`${data.total} endpoints`);
  return table(`${data.total} endpoints`, [
    { label: "Route", cell: (e) => (e.path ? h("span", { class: "mono" }, `${e.method || ""} ${e.path}`.trim()) : h("span", { class: "muted" }, "—")) },
    { label: "Handler", cell: (e) => (e.handler && !e.handler.includes(":") ? link(symbolHref(id, e.handler), e.handler, { class: "mono" }) : h("span", { class: "mono" }, e.handler)) },
    { label: "Framework", cell: (e) => e.framework || e.kind || "—" },
    { label: "Location", cell: (e) => loc(e.file, e.line) },
    { label: "Specs", cell: (e) => (e.spec_ids.length ? h("span", {}, e.spec_ids.map((s, i) => [i ? ", " : "", link(specHref(id, s), s)])) : h("span", { class: "muted" }, "none")) },
  ], data.endpoints);
}

function symbolRows(id, items, total, what) {
  if (!items.length) return h("p", { class: "muted" }, `No ${what}.`);
  return h("div", {},
    h("ul", { class: "list" }, items.map((s) => h("li", {},
      s.local ? link(symbolHref(id, s.qname), s.qname, { class: "mono" }) : h("span", { class: "mono" }, s.qname),
      s.local ? null : badge("implemented", s.project || "other repo"),
      h("span", { class: "chip" }, s.edge_type),
      s.origin && s.origin !== "livespec" ? h("span", { class: "chip", title: "edge from an ingested external graph" }, s.origin) : null,
      loc(s.file, s.line)))),
    total > items.length ? h("p", { class: "muted" }, `Showing ${items.length} of ${fmtInt(total)}.`) : null);
}

async function viewSymbol(id) {
  const qname = new URLSearchParams(location.search).get("qname") || "";
  loading("symbol");
  const ov = await overview(id);
  projectCrumbs(ov, [{ label: "Symbols", href: projectHref(id, "/symbols") }, { label: qname }]);
  const s = await api(`/api/p/${encodeURIComponent(id)}/symbol?qname=${encodeURIComponent(qname)}`);
  render(s.qname,
    h("p", { class: "eyebrow" }, `${s.kind} · ${s.language || ""}`),
    h("h1", { class: "mono" }, s.qname),
    h("p", {}, loc(s.file, `${s.start_line}-${s.end_line}`), s.visibility ? [" · ", s.visibility] : null),
    s.signature ? h("pre", {}, s.signature) : null,
    s.docstring ? h("pre", {}, s.docstring) : null,
    s.other_definitions.length ? h("p", { class: "muted" }, `Also defined at: ${s.other_definitions.map((d) => `${d.file}:${d.line}`).join(", ")}`) : null,
    h("h2", {}, "Specs"),
    s.specs.length
      ? h("ul", { class: "list" }, s.specs.map((x) => h("li", {}, link(specHref(id, x.spec_id), x.spec_id), h("span", { class: "muted" }, x.title), h("span", { class: "chip" }, x.relation))))
      : h("p", { class: "muted" }, "Not linked to any Spec."),
    h("div", { class: "columns" },
      h("section", { "aria-labelledby": "callers" }, h("h2", { id: "callers" }, `Who calls it (${fmtInt(s.callers_total)})`), symbolRows(id, s.callers, s.callers_total, "callers")),
      h("section", { "aria-labelledby": "callees" }, h("h2", { id: "callees" }, `What it calls (${fmtInt(s.callees_total)})`), symbolRows(id, s.callees, s.callees_total, "callees"))));
}

async function viewSpec(id, specId) {
  loading("Spec");
  const ov = await overview(id);
  projectCrumbs(ov, [{ label: "Specs", href: projectHref(id) }, { label: specId }]);
  const s = await api(`/api/p/${encodeURIComponent(id)}/specs/${encodeURIComponent(specId)}`);
  const c = s.coverage;
  const depList = (items, empty) => (items.length
    ? h("ul", { class: "list" }, items.map((d) => h("li", {}, link(specHref(id, d.spec_id), d.spec_id), h("span", { class: "muted" }, d.title), h("span", { class: "chip" }, d.kind))))
    : h("p", { class: "muted" }, empty));
  render(s.spec_id,
    h("p", { class: "eyebrow" }, [s.kind.replace(/_/g, " "), s.module ? `capability ${s.module}` : null, s.source].filter(Boolean).join(" · ")),
    h("h1", {}, s.title),
    h("p", { class: "row" }, h("code", {}, s.spec_id), badge(c.dev_state), h("span", { class: "chip" }, `status ${s.status}`), h("span", { class: "chip" }, `priority ${s.priority}`)),
    s.description ? h("pre", {}, s.description) : null,
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("div", { class: "v" }, fmtInt(c.implementing_symbols)), h("div", { class: "k" }, "Implementing symbols")),
      h("div", { class: "stat" }, h("div", { class: "v" }, fmtPct(c.test_coverage_pct)), h("div", { class: "k" }, `Test coverage (${c.tested_symbols}/${c.total_symbols}, ${c.coverage_source})`)),
      h("div", { class: "stat" }, h("div", { class: "v" }, c.confidence === null ? "—" : c.confidence.toFixed(2)), h("div", { class: "k" }, "Avg link confidence"))),
    s.mirrors.length ? h("p", {}, "Also in: ", s.mirrors.map((m, i) => [i ? ", " : "", link(specHref(m.id, s.spec_id), m.project)])) : null,
    h("h2", {}, `Scenarios (${s.scenarios.length})`),
    s.scenarios.length
      ? s.scenarios.map((sc) => h("details", { class: "scenario" }, h("summary", {}, sc.name), sc.body ? h("pre", {}, sc.body) : null,
        sc.symbols.length ? h("ul", { class: "list" }, sc.symbols.map((x) => h("li", {}, link(symbolHref(id, x.qname), x.qname, { class: "mono" }), h("span", { class: "chip" }, x.relation), loc(x.file, x.line)))) : null))
      : h("p", { class: "muted" }, "No scenarios."),
    h("h2", {}, `Symbols (${s.symbols.length})`),
    s.symbols.length
      ? table(null, [
        { label: "Symbol", cell: (x) => (x.local ? link(symbolHref(id, x.qname), x.qname, { class: "mono" }) : h("span", { class: "mono" }, `${x.project}: ${x.qname}`)) },
        { label: "Relation", cell: (x) => x.relation },
        { label: "Location", cell: (x) => loc(x.file, x.line) },
        { label: "Confidence", num: true, cell: (x) => x.confidence.toFixed(2) },
        { label: "Source", cell: (x) => x.source },
      ], s.symbols)
      : h("p", { class: "muted" }, "No code is linked to this Spec yet."),
    c.uncovered_symbols.length ? [h("h3", {}, `Not reached by any test (${c.uncovered_symbols_count})`),
      h("ul", { class: "chips" }, c.uncovered_symbols.map((q) => h("li", { class: "chip" }, link(symbolHref(id, q), q))))] : null,
    h("div", { class: "columns" },
      h("section", {}, h("h2", {}, "Depends on"), depList(s.depends_on, "No dependencies.")),
      h("section", {}, h("h2", {}, "Depended on by"), depList(s.depended_by, "Nothing depends on it."))),
    h("h2", {}, `History (${(s.tracks || []).length})`),
    (s.tracks || []).length
      ? h("ol", { class: "list" }, s.tracks.map((t) => h("li", {},
        h("p", { class: "row" }, h("strong", {}, t.title), badge(t.tier, t.tier === "touched" ? "touched only" : "changed"), h("span", { class: "muted" }, t.date || ""), h("code", {}, t.range)),
        t.why ? h("p", {}, t.why) : null,
        t.spec_why ? h("p", { class: "muted" }, `Why here: ${t.spec_why}`) : null,
        t.deltas.length ? h("ul", { class: "chips" }, t.deltas.map((d) => h("li", { class: "chip" }, `${d.operation} ${d.requirement}`))) : null,
        t.code.length ? h("ul", { class: "chips" }, t.code.map((x) => h("li", { class: "chip mono" }, `${{ added: "+", modified: "~", removed: "-" }[x.change] || "?"} ${x.name}`, h("span", { class: "muted" }, ` ${x.path}`)))) : null)))
      : h("p", { class: "muted" }, "No recorded track touched this Spec (CHANGESPEC.md)."));
}

// ------------------------------------------------------------------ group

async function viewGroup(gid) {
  loading("group");
  const g = await api(`/api/g/${encodeURIComponent(gid)}`);
  setCrumbs([{ label: "All projects", href: "/" }, { label: `Group: ${g.name}` }]);
  render(`Group ${g.name}`,
    h("p", { class: "eyebrow" }, "polyrepo group"),
    h("h1", {}, g.name),
    h("p", { class: "mono muted" }, g.db_path),
    h("div", { class: "stats" },
      [["Repos", g.projects.length], ["Cross-repo Specs", g.shared_specs.length], ["Route hops", g.route_hops.length], ["Cross-repo edges", g.cross_repo_edges]]
        .map(([k, v]) => h("div", { class: "stat" }, h("div", { class: "v" }, fmtInt(v)), h("div", { class: "k" }, k)))),
    h("h2", {}, "Repos"),
    table(null, [
      { label: "Repo", cell: (p) => h("div", {}, link(projectHref(p.id), p.name), h("div", { class: "mono muted" }, p.path)) },
      { label: "Languages", cell: (p) => languagesChips(p.languages) },
      { label: "Symbols", num: true, cell: (p) => fmtInt(p.counts.symbols) },
      { label: "Specs", num: true, cell: (p) => fmtInt(p.counts.specs) },
      { label: "Spec links", num: true, cell: (p) => fmtInt(p.links) },
    ], g.projects),
    h("h2", {}, "Cross-repo Specs"),
    g.shared_specs.length
      ? table("Spec ids mirrored in more than one repo (or xrepo-*)", [
        { label: "Spec", cell: (s) => h("div", {}, h("code", {}, s.spec_id), h("div", { class: "muted" }, s.title)) },
        { label: "Implemented in", cell: (s) => h("ul", { class: "chips" }, s.projects.map((p) => h("li", { class: "chip" }, link(specHref(p.id, s.spec_id), `${p.project} · ${p.links}`)))) },
        { label: "Depends on", cell: (s) => (s.depends_on.length ? s.depends_on.join(", ") : "—") },
      ], g.shared_specs)
      : h("p", { class: "muted" }, "No Spec id is shared between repos. Mirror a Spec as xrepo-<name> in each repo to see the flow here."),
    h("h2", {}, "HTTP route hops"),
    g.route_hops.length
      ? table(null, [
        { label: "From", cell: (r) => h("span", { class: "mono" }, `${r.from_project}: ${r.from_symbol}`) },
        { label: "Route", cell: (r) => h("code", {}, `${r.method || "*"} ${r.path}`) },
        { label: "To", cell: (r) => h("span", { class: "mono" }, `${r.to_project}: ${r.to_symbol}`) },
        { label: "Cross-repo", cell: (r) => (r.cross_repo ? "yes" : "no") },
      ], g.route_hops)
      : h("p", { class: "muted" }, "No client call resolved to a server route in this group yet."));
}

// ----------------------------------------------------------------- router

function navigate(href) {
  history.pushState(null, "", href);
  route();
}

async function route() {
  const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  try {
    if (!parts.length) await viewHome();
    else if (parts[0] === "g" && parts[1]) await viewGroup(parts[1]);
    else if (parts[0] === "p" && parts[1]) {
      const id = parts[1];
      const sub = parts[2];
      if (!sub) await projectPage(id, "specs", () => specsPanel(id));
      else if (sub === "symbols") await projectPage(id, "symbols", (ov) => symbolsPanel(id, ov));
      else if (sub === "endpoints") await projectPage(id, "endpoints", () => endpointsPanel(id));
      else if (sub === "symbol") await viewSymbol(id);
      else if (sub === "spec" && parts[3]) await viewSpec(id, parts.slice(3).join("/"));
      else throw new Error(`No page at ${location.pathname}`);
    } else throw new Error(`No page at ${location.pathname}`);
  } catch (e) {
    showError(e);
  }
  if (firstRoute) {
    firstRoute = false;
  } else if (!document.activeElement || document.activeElement === document.body || !main.contains(document.activeElement)) {
    const h1 = main.querySelector("h1");
    if (h1) { h1.setAttribute("tabindex", "-1"); h1.focus({ preventScroll: true }); }
  }
}

document.addEventListener("click", (ev) => {
  const a = ev.target.closest("a[data-nav]");
  if (!a || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin) return;
  ev.preventDefault();
  navigate(url.pathname + url.search);
  window.scrollTo(0, 0);
});
window.addEventListener("popstate", route);

// ------------------------------------------------------------------ theme

const themeBtn = document.getElementById("theme");
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  const dark = t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  themeBtn.setAttribute("aria-pressed", String(dark));
}
let savedTheme = null;
try { savedTheme = localStorage.getItem("livespec-ui-theme"); } catch (_) { /* ignore */ }
applyTheme(savedTheme);
themeBtn.addEventListener("click", () => {
  const dark = themeBtn.getAttribute("aria-pressed") === "true";
  const next = dark ? "light" : "dark";
  try { localStorage.setItem("livespec-ui-theme", next); } catch (_) { /* ignore */ }
  applyTheme(next);
});

route();
