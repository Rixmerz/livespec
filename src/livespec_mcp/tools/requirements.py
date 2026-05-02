"""RF tools: CRUD + linking + implementation lookup + RF dependency graph.

P1.2 consolidation: `suggest_rf_links` removed. To get implementation
candidates for an RF, call `search(query=<rf.title + rf.description>,
scope='code')` directly — the agent can then post-filter and call
`link_rf_symbol` for each accepted candidate.

RF-link naming (current; v0.6 renamed for clarity, v0.8 removed the
deprecated aliases):
  RF -> code symbol            link_rf_symbol
  RF -> another RF             link_rf_dependency
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastmcp import FastMCP

from pathlib import Path

from livespec_mcp.domain.graph import load_graph, page_rank
from livespec_mcp.domain.matcher import scan_annotations
from livespec_mcp.domain.md_rfs import parse_rfs_markdown
from livespec_mcp.state import get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import symbol_not_found_error


def _humanize_module_segment(seg: str) -> str:
    """auth_service -> 'Auth Service'; SyncQueue -> 'Sync Queue'."""
    s = seg.replace("_", " ").replace("-", " ")
    # Insert space before each uppercase letter that follows a lowercase
    out: list[str] = []
    for i, ch in enumerate(s):
        if i > 0 and ch.isupper() and s[i - 1].islower():
            out.append(" ")
        out.append(ch)
    title = "".join(out).strip()
    # Title-case, but only if it was lowercase to begin with (avoid
    # mangling acronyms like API, HTTP)
    if title.islower():
        title = title.title()
    return title


_DOC_FIRST_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_GENERIC_MODULE_NAMES = {
    "src", "lib", "core", "common", "utils", "util", "helpers", "helper",
    "tests", "test", "internal", "main", "mod", "index", "init", "__init__",
    "app", "crates", "pkg",
}


def _next_rf_id(conn, project_id: int) -> str:
    row = conn.execute(
        "SELECT rf_id FROM rf WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    n = 1
    if row:
        digits = "".join(c for c in row["rf_id"] if c.isdigit())
        if digits:
            n = int(digits) + 1
    return f"RF-{n:03d}"


def _noop_decorator(**_kwargs: Any):
    """Identity decorator: returns the wrapped function unchanged.

    Used to suppress @mcp.tool registration on a per-tool basis when
    splitting `register` between the default surface (agentic tools) and
    the optional `livespec-rf` plugin surface (mutation tools).
    """

    def _wrap(fn):
        return fn

    return _wrap


def register(
    mcp: FastMCP,
    agentic: bool = True,
    mutation: bool = False,
) -> None:
    """Register RF tools.

    v0.8 P3.4 split:
      - ``agentic=True, mutation=False`` (default, called by ``server.py``):
        registers the 3 RF tools an agent ASKS — ``list_requirements``,
        ``get_requirement_implementation``, ``propose_requirements_from_codebase``
        — plus the brownfield-discovery helpers.
      - ``agentic=False, mutation=True`` (called by ``tools.plugins.rf``):
        registers the 11 mutation/linking tools a HUMAN runs to mutate RF
        state. Auto-loads when the workspace DB has rf rows or
        ``LIVESPEC_PLUGINS`` includes ``rf``.

    The dual-decorator pattern below keeps every tool definition in a
    single file while letting registration flip on/off per surface.
    """
    agentic_tool = mcp.tool if agentic else _noop_decorator
    mutation_tool = mcp.tool if mutation else _noop_decorator

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    def create_requirement(
        title: str,
        description: str | None = None,
        rf_id: str | None = None,
        module: str | None = None,
        priority: Literal["low", "medium", "high", "critical"] = "medium",
        status: Literal["draft", "active", "deprecated"] = "draft",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Create a Functional Requirement.

        Auto-assigns rf_id (RF-NNN) if not provided. Not idempotent — use
        `update_requirement` for changes.
        """
        st = get_state(workspace)
        pid = st.project_id
        rid = rf_id or _next_rf_id(st.conn, pid)
        module_id = None
        if module:
            r = st.conn.execute(
                "SELECT id FROM module WHERE project_id=? AND name=?", (pid, module)
            ).fetchone()
            if r:
                module_id = int(r["id"])
            else:
                cur = st.conn.execute(
                    "INSERT INTO module(project_id, name) VALUES(?,?)", (pid, module)
                )
                module_id = int(cur.lastrowid)
        cur = st.conn.execute(
            """INSERT INTO rf(project_id, rf_id, title, description, module_id, status, priority)
               VALUES(?,?,?,?,?,?,?)""",
            (pid, rid, title, description, module_id, status, priority),
        )
        return {"id": int(cur.lastrowid), "rf_id": rid, "title": title}

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def update_requirement(
        rf_id: str,
        title: str | None = None,
        description: str | None = None,
        status: Literal["draft", "active", "deprecated"] | None = None,
        priority: Literal["low", "medium", "high", "critical"] | None = None,
        module: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Patch fields on an existing RF. Idempotent."""
        st = get_state(workspace)
        pid = st.project_id
        row = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
        ).fetchone()
        if not row:
            return mcp_error(
                f"RF '{rf_id}' not found",
                hint="check `list_requirements()` for known RF ids",
            )
        rf_pk = int(row["id"])
        sets: list[str] = []
        args: list[Any] = []
        for col, val in [("title", title), ("description", description), ("status", status), ("priority", priority)]:
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if module is not None:
            r = st.conn.execute(
                "SELECT id FROM module WHERE project_id=? AND name=?", (pid, module)
            ).fetchone()
            if not r:
                cur = st.conn.execute("INSERT INTO module(project_id, name) VALUES(?,?)", (pid, module))
                module_id = int(cur.lastrowid)
            else:
                module_id = int(r["id"])
            sets.append("module_id=?")
            args.append(module_id)
        sets.append("updated_at=datetime('now')")
        args.append(rf_pk)
        st.conn.execute(f"UPDATE rf SET {', '.join(sets)} WHERE id=?", args)
        return {"rf_id": rf_id, "updated": True}

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def list_requirements(
        status: str | None = None,
        module: str | None = None,
        priority: str | None = None,
        has_implementation: bool | None = None,
        limit: int = 100,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """List RFs with filters. Returns rf_id, title, status, priority, module, link_count."""
        st = get_state(workspace)
        pid = st.project_id
        sql = [
            """SELECT r.id, r.rf_id, r.title, r.description, r.status, r.priority,
                      m.name AS module,
                      (SELECT COUNT(*) FROM rf_symbol rs WHERE rs.rf_id=r.id) AS link_count
               FROM rf r LEFT JOIN module m ON m.id=r.module_id
               WHERE r.project_id=?"""
        ]
        args: list[Any] = [pid]
        if status:
            sql.append("AND r.status=?"); args.append(status)
        if priority:
            sql.append("AND r.priority=?"); args.append(priority)
        if module:
            sql.append("AND m.name=?"); args.append(module)
        sql.append("ORDER BY r.rf_id LIMIT ?"); args.append(limit)
        rows = [dict(r) for r in st.conn.execute(" ".join(sql), args).fetchall()]
        if has_implementation is not None:
            rows = [r for r in rows if (r["link_count"] > 0) == has_implementation]
        return {"requirements": rows}

    def _do_link_rf_symbol(
        rf_id: str,
        symbol_qname: str,
        relation: str,
        confidence: float,
        source: str,
        unlink: bool,
        workspace: str | None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        rf = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
        ).fetchone()
        if not rf:
            return mcp_error(
                f"RF '{rf_id}' not found",
                hint="check `list_requirements()` for known RF ids",
            )
        sym = st.conn.execute(
            """SELECT s.id FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
            (pid, symbol_qname),
        ).fetchone()
        if not sym:
            return symbol_not_found_error(st.conn, pid, symbol_qname)
        if unlink:
            st.conn.execute(
                "DELETE FROM rf_symbol WHERE rf_id=? AND symbol_id=? AND relation=?",
                (rf["id"], sym["id"], relation),
            )
            return {"unlinked": True, "rf_id": rf_id, "symbol": symbol_qname}
        st.conn.execute(
            """INSERT OR REPLACE INTO rf_symbol(rf_id, symbol_id, relation, confidence, source)
               VALUES(?,?,?,?,?)""",
            (rf["id"], sym["id"], relation, confidence, source),
        )
        return {"linked": True, "rf_id": rf_id, "symbol": symbol_qname, "relation": relation}

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def link_rf_symbol(
        rf_id: str,
        symbol_qname: str,
        relation: Literal["implements", "tests", "references"] = "implements",
        confidence: float = 1.0,
        source: Literal["manual", "annotation", "embedding", "llm"] = "manual",
        unlink: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Link (or unlink) an RF to a code symbol. unlink=True removes the link.

        v0.6 rename of link_requirement_to_code (kept as deprecated alias).
        """
        return _do_link_rf_symbol(
            rf_id, symbol_qname, relation, confidence, source, unlink, workspace
        )

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def bulk_link_rf_symbols(
        mappings: list[dict[str, Any]],
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Batch-link N (rf_id, symbol_qname) pairs in a single transaction.

        Each `mappings` entry accepts:
            {
              "rf_id": "RF-001",                          # required
              "symbol_qname": "pkg.auth.login",            # required
              "relation": "implements" | "tests" | "references",  # default implements
              "confidence": 0.0..1.0,                      # default 1.0
              "source": "manual" | "annotation" | "embedding" | "llm",  # default manual
            }

        Returns per-mapping result so the caller knows which entries
        landed vs. which failed (RF/symbol not found, validation, etc.):
            {
              "linked": int, "skipped": int, "failed": int,
              "results": [
                {"rf_id": "RF-001", "symbol_qname": "...",
                 "ok": bool, "linked": bool, "error": str | None},
                ...
              ]
            }

        Idempotent: re-linking an existing (rf, symbol, relation) is a no-op
        (`linked: false` but `ok: true`). v0.7 B1 — closes the brownfield
        migration friction where 50+ RFs needed individual round-trips.
        """
        st = get_state(workspace)
        pid = st.project_id
        results: list[dict[str, Any]] = []
        n_linked = 0
        n_skipped = 0
        n_failed = 0
        for m in mappings:
            rf_id = m.get("rf_id")
            symbol_qname = m.get("symbol_qname")
            if not rf_id or not symbol_qname:
                results.append({
                    "rf_id": rf_id, "symbol_qname": symbol_qname,
                    "ok": False, "linked": False,
                    "error": "rf_id and symbol_qname are required",
                })
                n_failed += 1
                continue
            relation = m.get("relation", "implements")
            confidence = float(m.get("confidence", 1.0))
            source = m.get("source", "manual")
            rf = st.conn.execute(
                "SELECT id FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
            ).fetchone()
            if not rf:
                results.append({
                    "rf_id": rf_id, "symbol_qname": symbol_qname,
                    "ok": False, "linked": False,
                    "error": f"RF '{rf_id}' not found",
                })
                n_failed += 1
                continue
            sym = st.conn.execute(
                """SELECT s.id FROM symbol s JOIN file f ON f.id=s.file_id
                   WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
                (pid, symbol_qname),
            ).fetchone()
            if not sym:
                results.append({
                    "rf_id": rf_id, "symbol_qname": symbol_qname,
                    "ok": False, "linked": False,
                    "error": f"Symbol '{symbol_qname}' not found",
                })
                n_failed += 1
                continue
            cur = st.conn.execute(
                """INSERT OR IGNORE INTO rf_symbol(rf_id, symbol_id, relation, confidence, source)
                   VALUES(?,?,?,?,?)""",
                (int(rf["id"]), int(sym["id"]), relation, confidence, source),
            )
            linked = cur.rowcount > 0
            if linked:
                n_linked += 1
            else:
                n_skipped += 1
            results.append({
                "rf_id": rf_id, "symbol_qname": symbol_qname,
                "ok": True, "linked": linked, "error": None,
            })
        return {
            "linked": n_linked,
            "skipped": n_skipped,
            "failed": n_failed,
            "total": len(mappings),
            "results": results,
        }

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_requirement_implementation(
        rf_id: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """What code implements an RF: list of symbols + files + coverage signals."""
        st = get_state(workspace)
        pid = st.project_id
        rf = st.conn.execute(
            """SELECT r.*, m.name AS module FROM rf r
               LEFT JOIN module m ON m.id=r.module_id
               WHERE r.project_id=? AND r.rf_id=?""",
            (pid, rf_id),
        ).fetchone()
        if not rf:
            return mcp_error(
                f"RF '{rf_id}' not found",
                hint="check `list_requirements()` for known RF ids",
            )
        rows = st.conn.execute(
            """SELECT s.qualified_name, s.kind, s.signature, s.start_line, s.end_line,
                      f.path, rs.relation, rs.confidence, rs.source
               FROM rf_symbol rs JOIN symbol s ON s.id=rs.symbol_id
               JOIN file f ON f.id=s.file_id
               WHERE rs.rf_id=?
               ORDER BY rs.confidence DESC, s.qualified_name""",
            (rf["id"],),
        ).fetchall()
        files = sorted({r["path"] for r in rows})
        return {
            "rf": {
                "rf_id": rf["rf_id"],
                "title": rf["title"],
                "description": rf["description"],
                "status": rf["status"],
                "priority": rf["priority"],
                "module": rf["module"],
            },
            "symbols": [dict(r) for r in rows],
            "files": files,
            "coverage": {"symbol_count": len(rows), "file_count": len(files)},
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def import_requirements_from_markdown(
        path: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Bulk-create / update RFs from a Markdown spec file.

        Format expected: `## RF-NNN: Title` headers, with `**Prioridad:** alta`
        and `**Módulo:** auth` metadata lines (Spanish or English variants).
        Idempotent: re-import updates existing RFs in place rather than duplicating.

        Path is resolved relative to the workspace root if not absolute.
        """
        st = get_state(workspace)
        pid = st.project_id
        p = Path(path)
        if not p.is_absolute():
            p = st.settings.workspace / path
        if not p.exists():
            return mcp_error(
                f"file not found: {p}",
                hint="path is resolved relative to the workspace root if not absolute",
            )
        text = p.read_text(encoding="utf-8", errors="replace")
        parsed = parse_rfs_markdown(text)
        created = 0
        updated = 0
        for prf in parsed:
            module_id = None
            if prf.module:
                row = st.conn.execute(
                    "SELECT id FROM module WHERE project_id=? AND name=?", (pid, prf.module)
                ).fetchone()
                if row:
                    module_id = int(row["id"])
                else:
                    cur = st.conn.execute(
                        "INSERT INTO module(project_id, name) VALUES(?,?)", (pid, prf.module)
                    )
                    module_id = int(cur.lastrowid)
            existing = st.conn.execute(
                "SELECT id FROM rf WHERE project_id=? AND rf_id=?", (pid, prf.rf_id)
            ).fetchone()
            if existing:
                st.conn.execute(
                    """UPDATE rf SET title=?, description=?, status=?, priority=?,
                       module_id=?, updated_at=datetime('now') WHERE id=?""",
                    (prf.title, prf.description, prf.status, prf.priority, module_id, existing["id"]),
                )
                updated += 1
            else:
                st.conn.execute(
                    """INSERT INTO rf(project_id, rf_id, title, description, module_id, status, priority)
                       VALUES(?,?,?,?,?,?,?)""",
                    (pid, prf.rf_id, prf.title, prf.description, module_id, prf.status, prf.priority),
                )
                created += 1
        return {
            "source": str(p),
            "parsed": len(parsed),
            "created": created,
            "updated": updated,
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": True})
    def delete_requirement(rf_id: str, workspace: str | None = None) -> dict[str, Any]:
        """Permanently delete an RF and its rf_symbol links (cascade).

        Idempotent: deleting an unknown rf_id returns deleted=False without error.
        """
        st = get_state(workspace)
        pid = st.project_id
        cur = st.conn.execute(
            "DELETE FROM rf WHERE project_id=? AND rf_id=?", (pid, rf_id)
        )
        return {"rf_id": rf_id, "deleted": cur.rowcount > 0}

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def propose_requirements_from_codebase(
        module_depth: int = 2,
        min_symbols_per_group: int = 3,
        max_proposals: int = 30,
        skip_already_covered: bool = True,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Heuristic RF discovery for brownfield projects (v0.7 B2).

        The killer feature for adopting livespec on an existing codebase.
        Groups symbols by their qname prefix at `module_depth` (e.g. depth=2
        means `pkg.auth.*` -> group "pkg.auth"), ranks groups by total
        PageRank score, and proposes one RF candidate per actionable group:

          {
            "proposed_rf_id": "RF-007",
            "title": "Auth",                       # humanized module name
            "description": "...",                  # first sentence of top symbol's docstring
            "module_key": "pkg.auth",
            "symbol_count": 12,
            "score": 0.0341,                       # sum of pagerank
            "suggested_symbols": [{qualified_name, kind, file_path, pagerank}, ...]
          }

        Filters:
        - Generic module names (src, lib, core, common, utils, ...) are not
          used as RF titles — fall back to the previous segment.
        - Already-RF-covered groups: skipped by default. Pass
          `skip_already_covered=False` to also propose RFs for partially
          covered modules (useful when adding sub-feature RFs alongside an
          existing feature RF).
        - Infrastructure / dunders / decorated handlers: excluded from
          symbol counts (same heuristic as find_dead_code).

        Output is sorted by group score descending. Pair with
        bulk_link_rf_symbols + create_requirement to land accepted
        proposals in two calls per RF: create the RF, then bulk-link its
        symbols.
        """
        st = get_state(workspace)
        pid = st.project_id
        view = load_graph(st.conn, pid)
        ranks = page_rank(view.g)

        # Already-linked symbol IDs (for skip_already_covered)
        linked_sids = {
            int(r["symbol_id"])
            for r in st.conn.execute(
                """SELECT DISTINCT rs.symbol_id FROM rf_symbol rs
                   JOIN symbol s ON s.id=rs.symbol_id
                   JOIN file f ON f.id=s.file_id
                   WHERE f.project_id=?""",
                (pid,),
            )
        }

        # v0.8 P2 fix #10: skip test modules — they exercise features but
        # aren't features themselves. Mirrors find_dead_code's entry-point
        # path filter.
        def _is_test_path(p: str) -> bool:
            return (
                p.startswith(("tests/", "test/", "bin/", "scripts/"))
                or "/tests/" in p
                or "/test/" in p
                or "/__tests__/" in p
                or "/__fixtures__/" in p
                or "/fixtures/" in p
            )

        # Group symbols by qname prefix at module_depth
        groups: dict[str, list[tuple[int, float, dict]]] = {}
        for sid, score in ranks.items():
            meta = view.sym_meta.get(sid)
            if meta is None:
                continue
            # Skip non-actionable noise (dunders/registers/DI helpers)
            from livespec_mcp.tools.analysis import _is_implicit_entry_point
            if _is_implicit_entry_point(meta):
                continue
            if _is_test_path(meta.get("file_path") or ""):
                continue
            qn = meta.get("qualified_name") or ""
            parts = qn.split(".")
            if len(parts) <= module_depth:
                continue
            group_key = ".".join(parts[:module_depth])
            groups.setdefault(group_key, []).append((sid, score, meta))

        # Build proposals
        proposals: list[dict[str, Any]] = []
        next_rf_n = 0
        # Compute starting RF id offset based on existing RFs
        last_rf = st.conn.execute(
            "SELECT rf_id FROM rf WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        if last_rf:
            digits = "".join(c for c in last_rf["rf_id"] if c.isdigit())
            next_rf_n = int(digits) if digits else 0

        for group_key, syms in groups.items():
            if len(syms) < min_symbols_per_group:
                continue

            # Skip groups that are already mostly covered
            if skip_already_covered:
                covered = sum(1 for sid, _, _ in syms if sid in linked_sids)
                if covered >= len(syms) * 0.5:
                    continue

            # Sort by pagerank desc and pick top
            syms.sort(key=lambda x: x[1], reverse=True)
            top = syms[:10]

            # Title: humanize the deepest non-generic segment of group_key
            segments = group_key.split(".")
            title_seg = segments[-1]
            for seg in reversed(segments):
                if seg.lower() not in _GENERIC_MODULE_NAMES:
                    title_seg = seg
                    break
            title = _humanize_module_segment(title_seg)

            # Description: first sentence of top symbol's docstring
            top_sid = top[0][0]
            doc_row = st.conn.execute(
                "SELECT docstring FROM symbol WHERE id=?", (top_sid,)
            ).fetchone()
            description: str | None = None
            if doc_row and doc_row["docstring"]:
                first = _DOC_FIRST_SENT_RE.split(
                    doc_row["docstring"].strip(), maxsplit=1
                )[0].strip()
                if first and not first.startswith("@"):
                    description = first[:200]

            score = sum(s for _, s, _ in top)

            next_rf_n += 1
            proposed_rf_id = f"RF-{next_rf_n:03d}"

            proposals.append({
                "proposed_rf_id": proposed_rf_id,
                "title": title,
                "description": description,
                "module_key": group_key,
                "symbol_count": len(syms),
                "score": round(float(score), 6),
                "suggested_symbols": [
                    {
                        "qualified_name": m["qualified_name"],
                        "kind": m["kind"],
                        "file_path": m["file_path"],
                        "pagerank": round(float(s), 6),
                    }
                    for _, s, m in top
                ],
            })

        proposals.sort(key=lambda p: p["score"], reverse=True)
        proposals = proposals[:max_proposals]

        # Re-number RF ids in score order so the highest-value group gets
        # RF-{next}, second gets RF-{next+1}, etc. — keeps the suggestion
        # naturally ordered.
        if last_rf:
            digits = "".join(c for c in last_rf["rf_id"] if c.isdigit())
            base = int(digits) if digits else 0
        else:
            base = 0
        for i, p in enumerate(proposals, start=1):
            p["proposed_rf_id"] = f"RF-{base + i:03d}"

        return {
            "proposals": proposals,
            "total_modules_examined": len(groups),
            "module_depth": module_depth,
        }

    @mutation_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def scan_docstrings_for_rf_hints(
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Surface RF candidates from existing docstrings — brownfield helper.

        Walks every symbol that has a docstring AND is not already linked
        to any RF. For each one, extracts:
          - the first sentence (up to ~140 chars)
          - the leading action verb if present ("Validates...", "Handles...",
            "Manages...", etc.)
          - the symbol metadata

        Useful when adopting livespec on an existing project: instead of
        guessing at RFs from scratch, the agent reads a few hundred of
        these hints and proposes RFs grouped by leading verb / module.

        Returns also a `verb_histogram` so the agent can see which actions
        dominate the codebase ("47 'Validates...', 31 'Handles...'") —
        that's the input signal for v0.7 B2 (propose_requirements_from_codebase).

        v0.7 B6 — heuristic only, no LLM. The agent decides which hints
        become RFs.
        """
        st = get_state(workspace)
        pid = st.project_id

        rows = st.conn.execute(
            """SELECT s.id, s.qualified_name, s.kind, s.docstring,
                      s.start_line, s.end_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.docstring IS NOT NULL AND s.docstring != ''
                 AND NOT EXISTS (
                   SELECT 1 FROM rf_symbol rs WHERE rs.symbol_id=s.id
                 )
               ORDER BY f.path, s.start_line""",
            (pid,),
        ).fetchall()

        # Strip trivial non-actionable hints
        _STOP_FIRST_WORDS = {
            "this", "the", "a", "an", "returns", "true", "false", "none",
            "todo", "fixme", "deprecated", "internal",
        }
        _SENT_END = re.compile(r"(?<=[.!?])\s+")

        hints: list[dict[str, Any]] = []
        verb_histogram: dict[str, int] = {}

        for r in rows:
            doc = (r["docstring"] or "").strip()
            if not doc:
                continue
            # First sentence, capped
            first_sent = _SENT_END.split(doc, maxsplit=1)[0].strip()
            if not first_sent or first_sent.startswith("@"):
                # Pure annotation lines like '@rf:RF-001' — already scanned
                continue
            first_sent = first_sent[:140]
            # Leading word
            tokens = first_sent.split()
            if not tokens:
                continue
            first_word = tokens[0].lower().strip(",.;:")
            if first_word in _STOP_FIRST_WORDS or len(first_word) < 3:
                continue
            verb_histogram[first_word] = verb_histogram.get(first_word, 0) + 1
            hints.append({
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file_path": r["file_path"],
                "start_line": r["start_line"],
                "first_sentence": first_sent,
                "leading_word": first_word,
            })

        # Top verbs descending
        top_verbs = sorted(
            verb_histogram.items(), key=lambda kv: kv[1], reverse=True
        )[:25]

        if summary_only:
            return {
                "count": len(hints),
                "verb_histogram_top": [
                    {"word": w, "n": n} for w, n in top_verbs
                ],
            }

        page = hints[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(hints) else None
        return {
            "count": len(hints),
            "verb_histogram_top": [
                {"word": w, "n": n} for w, n in top_verbs
            ],
            "hints": page,
            "next_cursor": next_cursor,
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def scan_rf_annotations(workspace: str | None = None) -> dict[str, Any]:
        """Re-scan all symbol docstrings for RF annotations and (re)link them.

        Two-level matcher (P1.4):
        - Explicit prefix `@rf:RF-001` / `@implements:RF-001` -> confidence 1.0
        - Verb-anchored `implements RF-001` (with negation guard) -> 0.7
        Idempotent: skips existing links.
        """
        st = get_state(workspace)
        pid = st.project_id
        n = scan_annotations(st.conn, pid)
        return {"links_created": n}

    # ---------- v0.5 P2 / v0.6 P1: RF dependency graph ----------

    def _do_link_rf_dependency(
        parent_rf_id: str,
        child_rf_id: str,
        kind: str,
        workspace: str | None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        if parent_rf_id == child_rf_id:
            return mcp_error("An RF cannot depend on itself")
        parent = st.conn.execute(
            "SELECT id, rf_id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, parent_rf_id),
        ).fetchone()
        child = st.conn.execute(
            "SELECT id, rf_id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, child_rf_id),
        ).fetchone()
        if not parent:
            return mcp_error(
                f"RF '{parent_rf_id}' not found",
                hint="check `list_requirements()` for known RF ids",
            )
        if not child:
            return mcp_error(
                f"RF '{child_rf_id}' not found",
                hint="check `list_requirements()` for known RF ids",
            )
        descendants: set[int] = set()
        frontier = [int(child["id"])]
        while frontier:
            current = frontier.pop()
            for r in st.conn.execute(
                "SELECT child_rf_id FROM rf_dependency WHERE parent_rf_id=?",
                (current,),
            ):
                cid = int(r["child_rf_id"])
                if cid in descendants:
                    continue
                descendants.add(cid)
                if cid == int(parent["id"]):
                    return mcp_error(
                        f"would create a cycle: {child_rf_id} already "
                        f"transitively depends on {parent_rf_id}",
                        hint="walk the existing graph with `get_rf_dependency_graph` to see the conflicting path",
                    )
                frontier.append(cid)
        cur = st.conn.execute(
            """INSERT OR IGNORE INTO rf_dependency(parent_rf_id, child_rf_id, kind)
               VALUES(?,?,?)""",
            (int(parent["id"]), int(child["id"]), kind),
        )
        return {
            "linked": cur.rowcount > 0,
            "parent": parent_rf_id,
            "child": child_rf_id,
            "kind": kind,
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def link_rf_dependency(
        parent_rf_id: str,
        child_rf_id: str,
        kind: Literal["requires", "extends", "conflicts"] = "requires",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Declare that one RF depends on another (RF-RF edge).

        Semantics:
        - `requires`  : parent needs child to be implemented first (the
                        common case — RF-API needs RF-AUTH).
        - `extends`   : parent specializes child's behavior (RF-EXPORT-PDF
                        extends RF-EXPORT).
        - `conflicts` : the two cannot both be active (mutually exclusive
                        rollouts).

        Idempotent on (parent, child, kind). Cycles are rejected at insert
        time: if adding the edge would create parent → … → parent in the
        forward closure, the call returns isError=True without writing.

        Self-loops are rejected by the schema CHECK constraint.
        """
        return _do_link_rf_dependency(parent_rf_id, child_rf_id, kind, workspace)

    def _do_unlink_rf_dependency(
        parent_rf_id: str,
        child_rf_id: str,
        kind: str | None,
        workspace: str | None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        parent = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, parent_rf_id),
        ).fetchone()
        child = st.conn.execute(
            "SELECT id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, child_rf_id),
        ).fetchone()
        if not parent or not child:
            return {"unlinked": 0, "parent": parent_rf_id, "child": child_rf_id}
        if kind is None:
            cur = st.conn.execute(
                "DELETE FROM rf_dependency WHERE parent_rf_id=? AND child_rf_id=?",
                (int(parent["id"]), int(child["id"])),
            )
        else:
            cur = st.conn.execute(
                """DELETE FROM rf_dependency WHERE parent_rf_id=? AND child_rf_id=?
                   AND kind=?""",
                (int(parent["id"]), int(child["id"]), kind),
            )
        return {
            "unlinked": cur.rowcount,
            "parent": parent_rf_id,
            "child": child_rf_id,
            "kind": kind,
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": True})
    def unlink_rf_dependency(
        parent_rf_id: str,
        child_rf_id: str,
        kind: Literal["requires", "extends", "conflicts"] | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Remove an RF dependency edge. If `kind` is None, drops every edge
        between the pair regardless of kind. Idempotent.
        """
        return _do_unlink_rf_dependency(parent_rf_id, child_rf_id, kind, workspace)

    def _do_get_rf_dependency_graph(
        rf_id: str,
        direction: str,
        max_depth: int,
        workspace: str | None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        root = st.conn.execute(
            "SELECT id, rf_id FROM rf WHERE project_id=? AND rf_id=?",
            (pid, rf_id),
        ).fetchone()
        if not root:
            return mcp_error(
                f"RF '{rf_id}' not found",
                hint="check `list_requirements()` for known RF ids",
            )
        root_id = int(root["id"])

        visited: set[int] = {root_id}
        edges: list[tuple[int, int, str]] = []

        def walk(start: int, forward: bool) -> None:
            frontier = [(start, 0)]
            while frontier:
                node, depth = frontier.pop()
                if depth >= max_depth:
                    continue
                if forward:
                    rows = st.conn.execute(
                        """SELECT parent_rf_id, child_rf_id, kind FROM rf_dependency
                           WHERE parent_rf_id=?""",
                        (node,),
                    )
                else:
                    rows = st.conn.execute(
                        """SELECT parent_rf_id, child_rf_id, kind FROM rf_dependency
                           WHERE child_rf_id=?""",
                        (node,),
                    )
                for r in rows:
                    edges.append((int(r["parent_rf_id"]), int(r["child_rf_id"]), r["kind"]))
                    next_id = int(r["child_rf_id"]) if forward else int(r["parent_rf_id"])
                    if next_id not in visited:
                        visited.add(next_id)
                        frontier.append((next_id, depth + 1))

        if direction in ("forward", "both"):
            walk(root_id, forward=True)
        if direction in ("backward", "both"):
            walk(root_id, forward=False)

        # Resolve metadata for visited RFs
        if visited:
            placeholders = ",".join("?" * len(visited))
            rf_meta = {
                int(r["id"]): {
                    "rf_id": r["rf_id"],
                    "title": r["title"],
                    "status": r["status"],
                    "priority": r["priority"],
                }
                for r in st.conn.execute(
                    f"SELECT id, rf_id, title, status, priority FROM rf WHERE id IN ({placeholders})",
                    list(visited),
                )
            }
        else:
            rf_meta = {}

        # Dedupe edges
        edge_keys: set[tuple[int, int, str]] = set()
        edge_payload: list[dict[str, Any]] = []
        for p, c, k in edges:
            key = (p, c, k)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edge_payload.append({
                "parent": rf_meta.get(p, {}).get("rf_id"),
                "child": rf_meta.get(c, {}).get("rf_id"),
                "kind": k,
            })

        return {
            "root": rf_id,
            "direction": direction,
            "nodes": list(rf_meta.values()),
            "edges": edge_payload,
        }

    @mutation_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_rf_dependency_graph(
        rf_id: str,
        direction: Literal["forward", "backward", "both"] = "both",
        max_depth: int = 5,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Walk the RF dependency graph from a given RF.

        - forward:  what does this RF depend on (children, transitively)?
        - backward: what depends on this RF (parents, transitively)?
        - both:     union of both.

        Returns the visited RF metadata + the edges traversed.
        """
        return _do_get_rf_dependency_graph(rf_id, direction, max_depth, workspace)
