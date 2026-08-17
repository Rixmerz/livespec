"""Spec tools: CRUD + linking + implementation lookup + Spec dependency graph.

P1.2 consolidation: `suggest_rf_links` removed. To get implementation
candidates for a Spec, call `search(query=<spec.title + spec.description>,
scope='code')` directly — the agent can then post-filter and call
`link_spec_symbol` for each accepted candidate.

Spec-link naming (v0.20 renamed RF -> Spec; taxonomy expanded via `kind`):
  Spec -> code symbol            link_spec_symbol
  Spec -> another Spec           link_spec_dependency
"""

from __future__ import annotations

import re
import sqlite3
from collections import deque
from collections.abc import Container
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastmcp import FastMCP

from livespec_mcp.domain import matcher
from livespec_mcp.domain.graph import graph_pagerank, load_graph
from livespec_mcp.domain.matcher import scan_annotations
from livespec_mcp.state import get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import _is_test_file_path, symbol_not_found_error
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

SpecKind = Literal[
    "functional_requirement",
    "non_functional_requirement",
    "adr",
    "design",
    "constraint",
    "epic",
    "other",
]


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


# `@word:` / `@word=` annotation-shaped comment, ANY verb (unlike
# `matcher._PREFIX_HEAD_RE`, which only matches the recognized vocabulary).
# Used by `scan_annotation_verbs` to find annotations the real matcher
# silently drops. Colon/equals required — matches the shape described in
# the bug report (`@rf:BE-RF-080`), not bare `@decorator` syntax.
#
# Deliberately NOT anchored to line-start (unlike `_PREFIX_HEAD_RE`): real
# annotations show up as `/** @rf:X */` (opener + annotation on one line)
# and `* @rf:X / @rf:Y` (two mentions on one line) — both would be missed,
# or under-counted, by a `^`-anchored single-leader regex. This tool is a
# diagnostic sweep, not a linker, so the wider match (any `@word:` anywhere)
# trades a little false-positive risk for not silently under-counting the
# exact class of bug it exists to catch.
_ANY_ANNOTATION_RE = re.compile(r"@(?P<verb>\w+)\s*[:=]\s*(?P<rest>[^\n\r@]*)")

# Payload "looks like a spec identifier" even when it isn't SPEC-NNN shaped
# (`matcher.SPEC_TOKEN_RE`) — e.g. `BE-RF-080`, `FE-RF-119`, `DEVMCP-RF-021`.
# When this matches, `@spec` is the domain-correct suggestion regardless of
# edit distance from the bare verb: pure Levenshtein on `"rf"` picks `"see"`
# (distance 3) over `"spec"` (distance 4), which is technically the nearest
# recognized verb but the wrong answer for every one of these — they're all
# spec references, not "see also" pointers.
_LIKELY_SPEC_ID_RE = re.compile(r"\b[A-Za-z]{2,}(?:-[A-Za-z]{2,})*-\d+\b")


def _levenshtein(a: str, b: str) -> int:
    """True edit distance (stdlib-only DP). `difflib.SequenceMatcher.ratio()`
    over-weights a shared PREFIX (e.g. would rank 'reference' above 'spec'
    for input 'rf' purely because both start with 'r') — not what
    `scan_annotation_verbs`'s docstring promises ("nearest recognized verb
    by edit distance")."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


_DOC_FIRST_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_GENERIC_MODULE_NAMES = {
    "src", "lib", "core", "common", "utils", "util", "helpers", "helper",
    "tests", "test", "internal", "main", "mod", "index", "init", "__init__",
    "app", "crates", "pkg",
}


# Reject minting/accepting PREFIX-NNN ids (SPEC-001, BE-RF-102, …). OpenSpec
# slugs are the only dialect; a colliding slug gets a numeric suffix instead.
_NUMERIC_ID_RE = re.compile(r"^[A-Za-z]+-\d+$")


def is_legacy_numeric_spec_id(spec_id: str) -> bool:
    """True for ``SPEC-001`` / ``AUTH-12`` (single PREFIX-NNN). OpenSpec slugs
    and multi-segment store ids like ``BE-RF-102`` are not this shape."""
    return bool(_NUMERIC_ID_RE.match((spec_id or "").strip()))


def disambiguated_slug_id(candidate: str, taken: Container[str]) -> str:
    """``auth-login`` → ``auth-login-2`` → ``auth-login-3`` … on collision.

    A colliding slug used to fall back to ``SPEC-NNN``, which answered a
    uniqueness problem by switching dialects: one project ended up with slug
    ids everywhere except the two specs whose titles happened to clash.
    """
    if candidate not in taken:
        return candidate
    for n in range(2, 1000):
        alt = f"{candidate}-{n}"
        if alt not in taken:
            return alt
    return f"{candidate}-{uuid4().hex[:8]}"


class _SpecIdsInProject:
    """`in` against the project's spec ids without loading them all."""

    def __init__(self, conn, project_id: int) -> None:
        self._conn, self._pid = conn, project_id

    def __contains__(self, spec_id: object) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM spec WHERE project_id=? AND spec_id=?",
                (self._pid, str(spec_id)),
            ).fetchone()
            is not None
        )


def _default_spec_id(conn, project_id: int, title: str, module: str | None) -> str:
    """OpenSpec-shaped slug, numbered rather than renamed when it collides."""
    from livespec_mcp.domain.md_specs import _ospec_spec_id

    return disambiguated_slug_id(
        _ospec_spec_id(title, module), _SpecIdsInProject(conn, project_id)
    )


def _noop_decorator(**_kwargs: Any):
    """Identity decorator: returns the wrapped function unchanged.

    Used to suppress @mcp.tool registration on a per-tool basis when
    splitting `register` between the default surface (agentic tools) and
    the optional `livespec-spec` plugin surface (mutation tools).
    """

    def _wrap(fn):
        return fn

    return _wrap



def _workspace_note(fn):
    """Append the shared workspace note to a tool's docstring.

    A triple-quoted literal followed by ``+ WORKSPACE_DOCSTRING_NOTE`` is not
    a docstring. Python only treats a bare string *literal* as one, so the
    concatenation evaluated to a discarded expression and left ``__doc__`` at
    ``None`` — the tool shipped with no description at all in ``tools/list``.
    22 of 48 tools were in that state, ``find_symbol``, ``search`` and
    ``quick_orient`` among them: almost half the surface invisible to an agent
    choosing what to call.

    The pattern reads correctly, which is why it survived a long time. This
    decorator keeps the intent and makes it actually happen.
    """
    fn.__doc__ = (fn.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE
    return fn

def register(
    mcp: FastMCP,
    agentic: bool = True,
    mutation: bool = False,
) -> None:
    """Register Spec tools.

    v0.8 P3.4 split (v0.20 renamed rf -> spec):
      - ``agentic=True, mutation=False`` (default, called by ``server.py``):
        registers the Spec tools an agent ASKS — ``list_specs``,
        ``get_spec_implementation``, ``propose_specs_from_codebase``
        — plus the brownfield-discovery helpers.
      - ``agentic=False, mutation=True`` (called by ``tools.plugins.spec``):
        registers the mutation/linking tools a HUMAN runs to mutate Spec
        state. Auto-loads when the workspace DB has spec rows or
        ``LIVESPEC_PLUGINS`` includes ``spec``.

    The dual-decorator pattern below keeps every tool definition in a
    single file while letting registration flip on/off per surface.
    """
    agentic_tool = mcp.tool if agentic else _noop_decorator
    mutation_tool = mcp.tool if mutation else _noop_decorator

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    @_workspace_note
    def create_spec(
        title: str,
        description: str | None = None,
        spec_id: str | None = None,
        kind: SpecKind = "functional_requirement",
        module: str | None = None,
        priority: Literal["low", "medium", "high", "critical"] = "medium",
        status: Literal["draft", "active", "deprecated"] = "draft",
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Create a Spec (functional requirement, NFR, ADR, design note, ...).

        Auto-assigns ``spec_id`` from the title (+ module) as an OpenSpec
        slug (e.g. ``auth-user-login``); a title that collides gets
        ``auth-user-login-2``. Pass ``spec_id`` explicitly to override with
        another slug — ``SPEC-001``-shaped ids are rejected.
        `kind` classifies the spec — see SpecKind for the documented values
        (free-text column, not DB-enforced, so custom kinds also work). Not
        idempotent — use `update_spec` for changes."""
        st = get_state(workspace)
        pid = st.project_id
        if spec_id is not None and is_legacy_numeric_spec_id(spec_id):
            return mcp_error(
                f"spec_id {spec_id!r} uses the removed PREFIX-NNN dialect",
                hint="use an OpenSpec slug (e.g. auth-user-login), or omit "
                "spec_id to derive one from the title",
            )
        sid = spec_id or _default_spec_id(st.conn, pid, title, module)
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
        try:
            cur = st.conn.execute(
                """INSERT INTO spec(project_id, spec_id, kind, title, description, module_id, status, priority)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (pid, sid, kind, title, description, module_id, status, priority),
            )
        except sqlite3.IntegrityError:
            # UNIQUE(project_id, spec_id) — surface a shaped error instead of a
            # raw sqlite internals string leaking to the client.
            return mcp_error(
                f"Spec '{sid}' already exists in this project.",
                hint="use update_spec to modify it, or omit spec_id to auto-number",
            )
        return {"id": int(cur.lastrowid), "spec_id": sid, "kind": kind, "title": title}

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def update_spec(
        spec_id: str,
        title: str | None = None,
        description: str | None = None,
        kind: SpecKind | None = None,
        status: Literal["draft", "active", "deprecated"] | None = None,
        priority: Literal["low", "medium", "high", "critical"] | None = None,
        module: str | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Patch fields on an existing Spec. Idempotent."""
        st = get_state(workspace)
        pid = st.project_id
        row = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (pid, spec_id)
        ).fetchone()
        if not row:
            return mcp_error(
                f"Spec '{spec_id}' not found",
                hint="check `list_specs()` for known spec ids",
            )
        spec_pk = int(row["id"])
        sets: list[str] = []
        args: list[Any] = []
        for col, val in [
            ("title", title), ("description", description), ("kind", kind),
            ("status", status), ("priority", priority),
        ]:
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
        args.append(spec_pk)
        st.conn.execute(f"UPDATE spec SET {', '.join(sets)} WHERE id=?", args)
        return {"spec_id": spec_id, "updated": True}

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def list_specs(
        status: str | None = None,
        module: str | None = None,
        priority: str | None = None,
        kind: str | None = None,
        has_implementation: bool | None = None,
        capability: str | None = None,
        limit: int = 100,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """List Specs with filters. Returns spec_id, kind, title, status, priority,
        module, link_count, scenario_count.

        ``capability`` is an OpenSpec-interop alias for ``module`` (an OpenSpec
        capability maps to a livespec module); pass either.

        Paginated (mirrors ``audit_coverage`` / ``scan_docstrings_for_spec_hints``):
        ``limit`` (default 100, hard-capped at 200 to bound response size on
        large spec sets — a flat un-paginated dump exceeded MCP's token limit
        on a 185-spec repo) caps the page; ``cursor`` resumes; ``next_cursor``
        is null when exhausted. ``count`` is the exact total regardless of the
        page (``total`` is its long-standing alias, kept for existing callers).
        ``summary_only=True`` returns just the count
        and the list of ``spec_id``s (no title/description bodies) — use this
        first to size a project before paging bodies."""
        st = get_state(workspace)
        pid = st.project_id
        # OpenSpec capability == livespec module. Accept either arg name.
        if capability and not module:
            module = capability
        where = ["sp.project_id=?"]
        args: list[Any] = [pid]
        if status:
            where.append("sp.status=?")
            args.append(status)
        if priority:
            where.append("sp.priority=?")
            args.append(priority)
        if kind:
            where.append("sp.kind=?")
            args.append(kind)
        if module:
            where.append("m.name=?")
            args.append(module)
        # Apply has_implementation in SQL BEFORE the LIMIT — filtering in Python
        # after the slice let a page silently shrink to 0 while matching specs
        # existed beyond the limit.
        if has_implementation is not None:
            op = ">" if has_implementation else "="
            where.append(
                f"(SELECT COUNT(*) FROM spec_symbol ss WHERE ss.spec_id=sp.id) {op} 0"
            )
        where_sql = " AND ".join(where)

        if summary_only:
            ids = [
                r["spec_id"]
                for r in st.conn.execute(
                    f"""SELECT sp.spec_id FROM spec sp LEFT JOIN module m ON m.id=sp.module_id
                        WHERE {where_sql} ORDER BY sp.spec_id""",
                    args,
                ).fetchall()
            ]
            # `count` is what every other paginated tool calls the exact total;
            # `total` predates it and stays so existing callers keep working.
            return {"count": len(ids), "total": len(ids), "spec_ids": ids}

        total = int(
            st.conn.execute(
                f"""SELECT COUNT(*) AS n FROM spec sp LEFT JOIN module m ON m.id=sp.module_id
                    WHERE {where_sql}""",
                args,
            ).fetchone()["n"]
        )
        page_limit = max(1, min(int(limit), 200))
        cursor = max(0, int(cursor))
        rows = [
            dict(r)
            for r in st.conn.execute(
                f"""SELECT sp.id, sp.spec_id, sp.kind, sp.title, sp.description, sp.status, sp.priority,
                           m.name AS module,
                           (SELECT COUNT(*) FROM spec_symbol ss WHERE ss.spec_id=sp.id) AS link_count,
                           (SELECT COUNT(*) FROM spec_scenario sc WHERE sc.spec_id=sp.id) AS scenario_count
                    FROM spec sp LEFT JOIN module m ON m.id=sp.module_id
                    WHERE {where_sql}
                    ORDER BY sp.spec_id LIMIT ? OFFSET ?""",
                [*args, page_limit, cursor],
            ).fetchall()
        ]
        next_cursor = cursor + page_limit if cursor + page_limit < total else None
        return {
            "specs": rows,
            "count": total,
            "total": total,
            "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
        }

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def get_cross_repo_guide(workspace: Workspace | None = None) -> dict[str, Any]:
        """How to use polyrepo ``group_db`` + mirrored ``xrepo-*`` Specs.

        Prefer this tool when MCP resource lists look incomplete (some hosts
        cache ``resources/list`` and miss ``project://group`` /
        ``project://cross-repo``). Returns the guide markdown plus a live
        ``group`` snapshot for the given workspace."""
        from livespec_mcp.prompts import _load_cross_repo_guide
        from livespec_mcp.resources import compute_group_view

        st = get_state(workspace)
        return {
            "guide": _load_cross_repo_guide(),
            "group": compute_group_view(st),
            "prompt": "cross_repo_workflow",
            "resources": [
                "project://cross-repo",
                "project://group",
                "guide://cross-repo",
            ],
        }

    def _do_link_spec_symbol(
        spec_id: str,
        symbol_qname: str,
        relation: str,
        confidence: float,
        source: str,
        unlink: bool,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        spec = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (pid, spec_id)
        ).fetchone()
        if not spec:
            return mcp_error(
                f"Spec '{spec_id}' not found",
                hint="check `list_specs()` for known spec ids",
            )
        # Group-aware: home project first, then the rest of a shared group DB
        # (ungrouped → home only, identical to the previous behaviour).
        sym = st.resolve_symbol(symbol_qname)
        if not sym:
            return symbol_not_found_error(st.conn, pid, symbol_qname)
        if unlink:
            st.conn.execute(
                "DELETE FROM spec_symbol WHERE spec_id=? AND symbol_id=? AND relation=?",
                (spec["id"], sym["id"], relation),
            )
            return {"unlinked": True, "spec_id": spec_id, "symbol": symbol_qname}
        st.conn.execute(
            """INSERT OR REPLACE INTO spec_symbol(spec_id, symbol_id, relation, confidence, source)
               VALUES(?,?,?,?,?)""",
            (spec["id"], sym["id"], relation, confidence, source),
        )
        return {"linked": True, "spec_id": spec_id, "symbol": symbol_qname, "relation": relation}

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def link_spec_symbol(
        spec_id: str,
        symbol_qname: str,
        relation: Literal["implements", "tests", "references"] = "implements",
        confidence: float = 1.0,
        source: Literal["manual", "annotation", "embedding", "llm"] = "manual",
        unlink: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Link (or unlink) a Spec to a code symbol. unlink=True removes the link."""
        return _do_link_spec_symbol(
            spec_id, symbol_qname, relation, confidence, source, unlink, workspace
        )

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def link_scenario_symbol(
        spec_id: str,
        scenario_name: str,
        symbol_qname: str,
        relation: Literal["implements", "tests", "references"] = "implements",
        confidence: float = 1.0,
        source: Literal["manual", "annotation", "embedding", "llm"] = "manual",
        unlink: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Link (or unlink) a single OpenSpec `#### Scenario:` to a code symbol.

        Scenario-level traceability: answers "which code/test verifies *this*
        WHEN/THEN scenario?" — finer than `link_spec_symbol` (whole requirement).
        The scenario is resolved by `(spec_id, scenario_name)`; see the scenario
        names in `get_spec_implementation`. `unlink=True` removes the link.
        """
        st = get_state(workspace)
        pid = st.project_id
        scen = st.conn.execute(
            """SELECT sc.id FROM spec_scenario sc JOIN spec sp ON sp.id=sc.spec_id
               WHERE sp.project_id=? AND sp.spec_id=? AND sc.name=?""",
            (pid, spec_id, scenario_name),
        ).fetchone()
        if not scen:
            return mcp_error(
                f"scenario {scenario_name!r} not found on spec {spec_id!r}",
                hint="check scenario names via `get_spec_implementation(spec_id)`",
            )
        sym = st.resolve_symbol(symbol_qname)
        if not sym:
            return symbol_not_found_error(st.conn, pid, symbol_qname)
        if unlink:
            st.conn.execute(
                "DELETE FROM scenario_symbol WHERE scenario_id=? AND symbol_id=? AND relation=?",
                (int(scen["id"]), int(sym["id"]), relation),
            )
            return {"unlinked": True, "spec_id": spec_id, "scenario": scenario_name}
        st.conn.execute(
            """INSERT OR REPLACE INTO scenario_symbol(scenario_id, symbol_id, relation, confidence, source)
               VALUES(?,?,?,?,?)""",
            (int(scen["id"]), int(sym["id"]), relation, confidence, source),
        )
        return {
            "linked": True,
            "spec_id": spec_id,
            "scenario": scenario_name,
            "symbol": symbol_qname,
            "relation": relation,
        }

    @agentic_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def bulk_link_spec_symbols(
        mappings: list[dict[str, Any]],
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Batch-link N (spec_id, symbol_qname) pairs in a single transaction.

        Each `mappings` entry accepts:
            {
              "spec_id": "auth-user-login",                 # required
              "symbol_qname": "pkg.auth.login",            # required
              "relation": "implements" | "tests" | "references",  # default implements
              "confidence": 0.0..1.0,                      # default 1.0
              "source": "manual" | "annotation" | "embedding" | "llm",  # default manual
            }

        Returns per-mapping result so the caller knows which entries
        landed vs. which failed (Spec/symbol not found, validation, etc.):
            {
              "linked": int, "skipped": int, "failed": int,
              "results": [
                {"spec_id": "auth-user-login", "symbol_qname": "...",
                 "ok": bool, "linked": bool, "error": str | None},
                ...
              ]
            }

        Idempotent: re-linking an existing (spec, symbol, relation) is a
        no-op (`linked: false` but `ok: true`). v0.7 B1 — closes the
        brownfield migration friction where 50+ specs needed individual
        round-trips.

        **Test symbols:** ``symbol_qname`` must name an indexed *function* or
        *method* (e.g. ``tests.pkg.test_auth.test_login_ok``). Test *modules*
        (``tests.pkg.test_auth``) are not symbols and will fail lookup.
        """
        st = get_state(workspace)
        from livespec_mcp.domain.specs_sync import bulk_link_spec_symbols_impl

        return bulk_link_spec_symbols_impl(st, mappings)

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_spec_implementation(
        spec_id: str,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """What code implements a Spec: list of symbols + files + coverage signals."""
        st = get_state(workspace)
        pid = st.project_id
        spec = st.conn.execute(
            """SELECT sp.*, m.name AS module FROM spec sp
               LEFT JOIN module m ON m.id=sp.module_id
               WHERE sp.project_id=? AND sp.spec_id=?""",
            (pid, spec_id),
        ).fetchone()
        if not spec:
            return mcp_error(
                f"Spec '{spec_id}' not found",
                hint="check `list_specs()` for known spec ids",
            )
        rows = st.conn.execute(
            """SELECT s.qualified_name, s.kind, s.signature, s.start_line, s.end_line,
                      f.path, ss.relation, ss.confidence, ss.source
               FROM spec_symbol ss JOIN symbol s ON s.id=ss.symbol_id
               JOIN file f ON f.id=s.file_id
               WHERE ss.spec_id=?
               ORDER BY ss.confidence DESC, s.qualified_name""",
            (spec["id"],),
        ).fetchall()
        files = sorted({r["path"] for r in rows})
        scenarios = []
        for s in st.conn.execute(
            "SELECT id, name, body FROM spec_scenario WHERE spec_id=? ORDER BY ordinal, id",
            (spec["id"],),
        ):
            # Scenario-level traceability: symbols linked to this specific
            # WHEN/THEN scenario (not just the parent requirement).
            scen_syms = [
                {
                    "qualified_name": r["qualified_name"],
                    "relation": r["relation"],
                    "confidence": r["confidence"],
                    "source": r["source"],
                }
                for r in st.conn.execute(
                    """SELECT sym.qualified_name, ssy.relation, ssy.confidence, ssy.source
                       FROM scenario_symbol ssy JOIN symbol sym ON sym.id=ssy.symbol_id
                       WHERE ssy.scenario_id=?
                       ORDER BY ssy.confidence DESC, sym.qualified_name""",
                    (int(s["id"]),),
                )
            ]
            scenarios.append(
                {
                    "name": s["name"],
                    "body": s["body"],
                    "symbols": scen_syms,
                    "verified": len(scen_syms) > 0,
                }
            )
        return {
            "spec": {
                "spec_id": spec["spec_id"],
                "kind": spec["kind"],
                "title": spec["title"],
                "description": spec["description"],
                "status": spec["status"],
                "priority": spec["priority"],
                "module": spec["module"],
                # OpenSpec interop: capability is the OpenSpec name for module.
                "capability": spec["module"],
            },
            "symbols": [dict(r) for r in rows],
            "files": files,
            # OpenSpec scenarios (WHEN/THEN) attached to this requirement.
            "scenarios": scenarios,
            "coverage": {
                "symbol_count": len(rows),
                "file_count": len(files),
                "scenario_count": len(scenarios),
                "scenarios_verified": sum(1 for s in scenarios if s["verified"]),
            },
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def import_specs_from_markdown(
        path: str,
        fmt: str = "openspec",
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Bulk-create / update Specs from OpenSpec Markdown.

        OpenSpec only (`### Requirement: <name>` + SHALL prose +
        `#### Scenario:` WHEN/THEN). The requirement name becomes the title;
        a slug becomes the spec_id; `## REMOVED Requirements` → `deprecated`,
        else `active`. Point `path` at a single file, or at an `openspec/`
        directory to walk its `specs/`/`changes/` tree (capability = folder).

        The former native `## SPEC-NNN:` catalog is removed — migrate those
        files under `openspec/specs/<capability>/spec.md` and use
        `sync_openspec`. `fmt` accepts `"openspec"` or `"auto"` only.

        Idempotent: re-import updates in place. Specs default to
        `kind=functional_requirement` — reclassify with `update_spec`.
        Path is resolved relative to the workspace root if not absolute.
        """
        if fmt not in ("auto", "openspec"):
            return mcp_error(
                f"invalid fmt: {fmt!r}",
                did_you_mean=["openspec", "auto"],
                hint="OpenSpec only — native ## SPEC-NNN catalogs are removed; "
                "migrate to openspec/ and call sync_openspec",
            )
        st = get_state(workspace)
        try:
            from livespec_mcp.domain.md_specs import UnsupportedSpecCatalogError
            from livespec_mcp.domain.specs_sync import (
                import_specs_from_markdown_file,
            )

            out = import_specs_from_markdown_file(st, path, fmt=fmt)
            # Internal bookkeeping for the full-tree sweep, not payload.
            out.pop("spec_ids", None)
            return out
        except UnsupportedSpecCatalogError as e:
            return mcp_error(
                str(e),
                hint="write ### Requirement: under openspec/specs/<capability>/spec.md",
            )
        except FileNotFoundError:
            return mcp_error(
                f"file not found: {path}",
                hint="path is resolved relative to the workspace root if not absolute",
            )

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": True})
    def delete_spec(spec_id: str, workspace: Workspace | None = None) -> dict[str, Any]:
        """Permanently delete a Spec and its spec_symbol links (cascade).

        Idempotent: deleting an unknown spec_id returns deleted=False without error.
        """
        st = get_state(workspace)
        pid = st.project_id
        cur = st.conn.execute(
            "DELETE FROM spec WHERE project_id=? AND spec_id=?", (pid, spec_id)
        )
        return {"spec_id": spec_id, "deleted": cur.rowcount > 0}

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def propose_specs_from_codebase(
        module_depth: int = 3,
        min_symbols_per_group: int = 3,
        max_proposals: int = 30,
        skip_already_covered: bool = True,
        community_graph: str | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Heuristic Spec discovery for brownfield projects (v0.7 B2).

        The killer feature for adopting livespec on an existing codebase.
        Groups symbols by their qname prefix at `module_depth` (default 3;
        e.g. depth=3 means `src.pkg.auth.*` -> group "src.pkg.auth"). A
        deeper default avoids collapsing a whole `src.*` subtree into one
        useless spec — pass a lower `module_depth` for shallow layouts. Ranks
        groups by total
        PageRank score, and proposes one Spec candidate per actionable group
        (kind defaults to "functional_requirement"):

          {
            "proposed_spec_id": "auth-auth",       # OpenSpec slug; -2 on collision
            "title": "Auth",                       # humanized module name
            "description": "...",                  # first sentence of top symbol's docstring
            "module_key": "pkg.auth",
            "symbol_count": 12,
            "score": 0.0341,                       # sum of pagerank
            "suggested_symbols": [{qualified_name, kind, file_path, pagerank}, ...]
          }

        Filters:
        - Generic module names (src, lib, core, common, utils, ...) are not
          used as Spec titles — fall back to the previous segment.
        - Already-Spec-covered groups: skipped by default when **any** symbol
          in the group is already linked (brownfield repos with Specs were
          proposing duplicate modules for sparsely-linked groups under the
          old ≥50% rule). Pass `skip_already_covered=False` to also propose
          for partially covered modules (sub-feature Specs alongside an
          existing feature Spec).
        - Infrastructure / dunders / decorated handlers: excluded from
          symbol counts (same heuristic as find_dead_code).

        Unreleased: ``community_graph=<path to a Graphify graph.json>`` groups
        by the external graph's detected communities instead of by qname
        prefix. Module prefix is a proxy for capability and a poor one — it
        follows the folder layout, so a feature split across ``services/`` and
        ``routes/`` becomes two proposals and a grab-bag ``utils/`` becomes
        one. Communities are derived from how the code actually connects.
        Symbols the external graph doesn't cover fall back to module grouping,
        so this only ever adds signal. Only community *membership* is used
        (Leiden, deterministic) — never the LLM-written community labels;
        titles stay derived from the symbols themselves.

        Output is sorted by group score descending. Pair with
        bulk_link_spec_symbols + create_spec to land accepted
        proposals in two calls per spec: create the spec, then bulk-link its
        symbols. Payload includes ``skipped_covered_count`` when the skip
        filter drops groups.
        """
        st = get_state(workspace)
        pid = st.project_id
        view = load_graph(st.conn, pid)
        ranks = graph_pagerank(view)

        # Optional external communities. Loaded up front so a bad path fails
        # before any work, and so the error is about the file rather than
        # surfacing later as mysteriously module-shaped groups.
        from livespec_mcp.tools.analysis import _resolve_corroboration_source

        ext_graph = None
        community_graph, community_graph_hint = _resolve_corroboration_source(
            st, community_graph
        )
        if community_graph:
            from livespec_mcp.domain.external_graph import (
                load_external_graph,
                overlap_ratio,
            )

            ext_path = Path(community_graph)
            if not ext_path.is_absolute():
                ext_path = st.settings.workspace / ext_path
            try:
                ext_graph = load_external_graph(ext_path)
            except FileNotFoundError:
                return mcp_error(
                    f"External graph not found: {ext_path}",
                    hint=(
                        "Generate one with `/graphify <repo>` (writes "
                        "graphify-out/graph.json), or pass an absolute path."
                    ),
                )
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                return mcp_error(
                    f"Could not read external graph {ext_path}: {exc}",
                    hint="Expected Graphify's NetworkX node-link graph.json.",
                )
            indexed_files = {
                r["path"]
                for r in st.conn.execute(
                    "SELECT path FROM file WHERE project_id=?", (pid,)
                )
            }
            if overlap_ratio(ext_graph, indexed_files) < 0.1:
                return mcp_error(
                    f"External graph {ext_path} shares almost no files with "
                    "this index.",
                    hint=(
                        "It probably describes a different repo, or was built "
                        "from a different root so its paths do not line up."
                    ),
                )

        # Already-linked symbol IDs (for skip_already_covered)
        linked_sids = {
            int(r["symbol_id"])
            for r in st.conn.execute(
                """SELECT DISTINCT ss.symbol_id FROM spec_symbol ss
                   JOIN symbol s ON s.id=ss.symbol_id
                   JOIN file f ON f.id=s.file_id
                   WHERE f.project_id=?""",
                (pid,),
            )
        }

        # v0.8 P2 fix #10: skip test modules — they exercise features but
        # aren't features themselves. Mirrors find_dead_code's entry-point
        # path filter.
        # Delegates test detection rather than keeping a third copy of the
        # heuristic: this one only knew directory conventions, so a
        # `*.test.ts` / `*.spec.ts` file sitting beside its source was
        # proposed as a feature. `bin/` and `scripts/` are not tests — they
        # are excluded for the same "not a feature" reason, so they stay.
        def _is_test_path(p: str) -> bool:
            return (
                _is_test_file_path(p)
                or p.startswith(("bin/", "scripts/"))
                or "/__fixtures__/" in p
                or "/fixtures/" in p
            )

        # Group symbols by qname prefix at module_depth — or, when an external
        # graph is supplied, by its detected community.
        groups: dict[str, list[tuple[int, float, dict]]] = {}
        community_grouped = 0
        module_grouped = 0
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

            group_key: str | None = None
            if ext_graph is not None:
                node = ext_graph.lookup(
                    meta.get("file_path") or "",
                    int(meta.get("start_line") or 0),
                    meta.get("name") or (parts[-1] if parts else ""),
                )
                if node is not None and node.community is not None:
                    group_key = f"community:{node.community}"
                    community_grouped += 1
            if group_key is None:
                # Fallback: a symbol the external graph does not cover still
                # deserves a proposal, so external grouping only ever adds.
                if len(parts) <= module_depth:
                    continue
                group_key = ".".join(parts[:module_depth])
                module_grouped += 1
            groups.setdefault(group_key, []).append((sid, score, meta))

        # Build proposals
        proposals: list[dict[str, Any]] = []
        skipped_covered_count = 0
        from livespec_mcp.domain.md_specs import _ospec_spec_id

        existing_ids = {
            r["spec_id"]
            for r in st.conn.execute(
                "SELECT spec_id FROM spec WHERE project_id=?", (pid,)
            )
        }
        used_ids: set[str] = set()

        for group_key, syms in groups.items():
            if len(syms) < min_symbols_per_group:
                continue

            # Skip groups that already have any Spec↔symbol link
            if skip_already_covered:
                covered = sum(1 for sid, _, _ in syms if sid in linked_sids)
                if covered > 0:
                    skipped_covered_count += 1
                    continue

            # Sort by pagerank desc and pick top
            syms.sort(key=lambda x: x[1], reverse=True)
            top = syms[:10]

            # Title: humanize the deepest non-generic segment of group_key.
            # A community key carries no name of its own — and Graphify's own
            # community labels are LLM-written, so importing them would quietly
            # put a model in a deterministic path. Derive the title from the
            # members instead: the most common non-generic module segment among
            # the highest-ranked symbols.
            if group_key.startswith("community:"):
                seg_counts: dict[str, int] = {}
                for _, _, m in syms:
                    for seg in (m.get("qualified_name") or "").split(".")[:-1]:
                        if seg and seg.lower() not in _GENERIC_MODULE_NAMES:
                            seg_counts[seg] = seg_counts.get(seg, 0) + 1
                if seg_counts:
                    title_seg = max(seg_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
                else:
                    title_seg = (
                        (syms[0][2].get("qualified_name") or "group").split(".")[-1]
                    )
            else:
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

            proposals.append({
                "proposed_spec_id": "",  # assigned after sort by score
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

        # OpenSpec-shaped ids in score order, numbered when two titles collide.
        for p in proposals:
            key = p["module_key"] or ""
            # Never seed a Spec id from a community number. Leiden ids depend
            # on the clustering run, so `community-4-checkout` would become
            # `community-7-checkout` on the next graph build — a Spec id that
            # changes under you is worse than a slightly less specific one.
            module_hint = (
                None if key.startswith("community:") else (key.split(".")[-1] or None)
            )
            sid = disambiguated_slug_id(
                _ospec_spec_id(p["title"], module_hint), existing_ids | used_ids
            )
            p["proposed_spec_id"] = sid
            used_ids.add(sid)

        payload: dict[str, Any] = {
            "proposals": proposals,
            "total_modules_examined": len(groups),
            "module_depth": module_depth,
            "skipped_covered_count": skipped_covered_count,
            "skip_already_covered": skip_already_covered,
        }
        if ext_graph is None and community_graph_hint:
            payload["grouping_available"] = community_graph_hint
        if ext_graph is not None:
            payload["grouping"] = {
                "source": ext_graph.path,
                "external_nodes": ext_graph.node_count,
                "symbols_grouped_by_community": community_grouped,
                "symbols_grouped_by_module": module_grouped,
                "hint": (
                    "Groups keyed `community:N` come from the external graph's "
                    "detected communities; the rest fell back to module prefix. "
                    "Only community membership is used — titles are derived "
                    "from the symbols, never from LLM-written community labels."
                ),
            }
        return payload

    @mutation_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def scan_docstrings_for_spec_hints(
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Surface Spec candidates from existing docstrings / JSDoc — brownfield helper.

        Walks every symbol that has a docstring AND is not already linked
        to any Spec. For each one, extracts:
          - the first sentence (up to ~140 chars)
          - the leading action verb if present ("Validates...", "Handles...",
            "Manages...", etc.)
          - the symbol metadata

        Works on Python docstrings and JS/TS JSDoc already stored on
        ``symbol.docstring`` by the extractors. Pure ``@spec:`` /
        ``@param`` annotation lines are skipped. Prefer
        ``propose_specs_from_codebase`` when there are no prose docstrings.

        Returns also a `verb_histogram` so the agent can see which actions
        dominate the codebase ("47 'Validates...', 31 'Handles...'") —
        that's the input signal for v0.7 B2 (propose_specs_from_codebase).

        v0.7 B6 — heuristic only, no LLM. The agent decides which hints
        become specs.
        """
        st = get_state(workspace)
        pid = st.project_id

        rows = st.conn.execute(
            """SELECT s.id, s.qualified_name, s.kind, s.docstring,
                      s.start_line, s.end_line, f.path AS file_path
               FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.docstring IS NOT NULL AND s.docstring != ''
                 AND NOT EXISTS (
                   SELECT 1 FROM spec_symbol ss WHERE ss.symbol_id=s.id
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
                # Pure annotation lines like '@spec:SPEC-001' — already scanned
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

        out: dict[str, Any] = {
            "count": len(hints),
            "verb_histogram_top": [
                {"word": w, "n": n} for w, n in top_verbs
            ],
        }
        if not hints and rows:
            out["hint"] = (
                "Indexed symbols have docstrings/JSDoc but none yielded "
                "actionable prose (often pure @spec:/@param lines, or already "
                "Spec-linked). Prefer propose_specs_from_codebase / OpenSpec."
            )
        if summary_only:
            return out

        page = hints[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(hints) else None
        out["hints"] = page
        out["next_cursor"] = next_cursor
        return out

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def scan_spec_annotations(workspace: Workspace | None = None) -> dict[str, Any]:
        """Re-scan all symbol docstrings for Spec annotations and (re)link them.

        Two-level matcher, OpenSpec slugs only:
        - Explicit prefix `@spec:auth-user-login` / `@implements:auth-user-login`
          -> confidence 1.0
        - Verb-anchored `implements auth-user-login` (negation guard) -> 0.7
        A slug is recognized when it is a spec id in the store. Idempotent:
        skips existing links.

        Ids annotated in code that match no spec come back as
        `unknown_annotation_ids` (complete) plus a capped
        `unknown_annotation_sample` — most often an OpenSpec requirement that
        was renamed, which changes the slug its id derives from and leaves
        every `@spec:` pointing at it silently dead.
        """
        st = get_state(workspace)
        pid = st.project_id
        res = scan_annotations(st.conn, pid)
        out: dict[str, Any] = {"links_created": res.created}
        if res.unknown_ids:
            out["unknown_annotation_ids"] = res.unknown_ids
            out["unknown_annotation_sample"] = res.unknown_sample
            out["hint"] = (
                "these ids are annotated in code but no spec answers to them — "
                "run list_specs to see the current ids, or export_openspec to "
                "write a <!-- livespec:id= --> marker so renames stop breaking links"
            )
        return out

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def scan_annotation_verbs(
        sample_per_group: int = 10,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Find `@word:` annotation-shaped comments the matcher will NOT link.

        `scan_spec_annotations` / `parse_annotations` only consume a fixed
        verb vocabulary (`@spec`, `@implements`, `@tests`, `@see`,
        `@references`, plus the `@not_spec` negation) AND require the payload
        to contain a `SPEC-NNN`-shaped token. A comment like `@rf:BE-RF-080`
        satisfies neither: the verb `rf` is unrecognized, so it is silently
        invisible to every other Spec tool — no error, no count, nothing.
        This tool is the check that surfaces that gap.

        Two independent failure modes are reported, since either one alone
        is enough to make an annotation dead weight:
        - `unrecognized_verb`: the `@word` isn't in the recognized set.
          `did_you_mean` suggests the nearest recognized verb by edit
          distance.
        - `token_shape`: the verb IS recognized but the payload after it
          doesn't contain a `SPEC-NNN` token (e.g. `@spec:BE-RF-080` — the
          verb is fine, `BE-RF-080` just isn't shaped like `SPEC-080`).

        Findings are grouped by the literal `@word` used, each with a count
        and a bounded sample of `file:line` sites (`sample_per_group`,
        default 10) — with potentially hundreds of raw hits, the caller
        needs the shape of the problem, not every occurrence.

        Scans indexed source FILES directly, not just extracted docstrings:
        an annotation sitting above a symbol tree-sitter doesn't extract as
        a linkable node (a bare `new Hono()` export, a Zod schema `const`)
        is invisible to `symbol.docstring` entirely, which would silently
        under-count this exact class of bug. Reading the file guarantees
        every occurrence is seen regardless of extractor coverage.
        """
        st = get_state(workspace)
        pid = st.project_id
        ws = st.settings.workspace
        file_rows = st.conn.execute(
            "SELECT path FROM file WHERE project_id=?", (pid,)
        ).fetchall()

        recognized = matcher.RECOGNIZED_PREFIX_VERBS_DISPLAY
        recognized_sorted = sorted(recognized)

        # Judge token shape against the SAME prefix set the real linker
        # derives from the store (`scan_annotations` / `derive_spec_prefixes`)
        # — otherwise this diagnostic and the linker disagree about what
        # counts as "linkable".
        spec_ids = (
            r["spec_id"]
            for r in st.conn.execute("SELECT spec_id FROM spec WHERE project_id=?", (pid,))
        )
        token_re = matcher.make_spec_token_re(matcher.derive_spec_prefixes(spec_ids))

        groups: dict[str, dict[str, Any]] = {}
        total_findings = 0
        for fr in file_rows:
            path = fr["path"]
            try:
                src = (ws / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            symbols: list[dict[str, Any]] | None = None
            for m in _ANY_ANNOTATION_RE.finditer(src):
                verb = m.group("verb")
                verb_lower = verb.lower()
                rest = m.group("rest")
                token_ok = bool(token_re.search(rest))
                verb_ok = verb_lower in recognized
                if verb_ok and token_ok:
                    continue  # the real matcher DOES consume this one
                total_findings += 1
                key = f"@{verb}"
                g = groups.setdefault(key, {
                    "verb": key,
                    "count": 0,
                    "reason": "unrecognized_verb" if not verb_ok else "token_shape",
                    "did_you_mean": None,
                    "did_you_mean_reason": None,
                    "sample": [],
                })
                g["count"] += 1
                if not verb_ok and g["did_you_mean"] is None:
                    if _LIKELY_SPEC_ID_RE.search(rest):
                        # Payload signal beats edit distance: this is a spec
                        # reference (BE-RF-080-shaped), not a typo of @see.
                        g["did_you_mean"] = "@spec"
                        g["did_you_mean_reason"] = (
                            "payload looks like a spec identifier "
                            f"({_LIKELY_SPEC_ID_RE.search(rest).group(0)!r})"
                        )
                    else:
                        nearest = min(
                            recognized_sorted,
                            key=lambda v: (_levenshtein(verb_lower, v), v),
                        )
                        g["did_you_mean"] = f"@{nearest}"
                        g["did_you_mean_reason"] = "nearest recognized verb by edit distance"
                if len(g["sample"]) < sample_per_group:
                    line = src[: m.start()].count("\n") + 1
                    if symbols is None:
                        symbols = [
                            dict(sr) for sr in st.conn.execute(
                                """SELECT qualified_name, start_line, end_line FROM symbol s
                                   JOIN file f ON f.id=s.file_id
                                   WHERE f.project_id=? AND f.path=?""",
                                (pid, path),
                            ).fetchall()
                        ]
                    qname = next(
                        (
                            s["qualified_name"] for s in symbols
                            if s["start_line"] <= line <= s["end_line"]
                        ),
                        None,
                    )
                    g["sample"].append({
                        "qualified_name": qname,
                        "file_path": path,
                        "line": line,
                        "token_candidate": rest.strip()[:40],
                    })

        groups_out = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
        return {
            "total_findings": total_findings,
            "verb_groups": groups_out,
            "recognized_verbs": recognized_sorted,
        }

    # ---------- v0.5 P2 / v0.6 P1 / v0.20: Spec dependency graph ----------

    def _do_link_spec_dependency(
        parent_spec_id: str,
        child_spec_id: str,
        kind: str,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        if parent_spec_id == child_spec_id:
            return mcp_error("A Spec cannot depend on itself")
        parent = st.conn.execute(
            "SELECT id, spec_id FROM spec WHERE project_id=? AND spec_id=?",
            (pid, parent_spec_id),
        ).fetchone()
        child = st.conn.execute(
            "SELECT id, spec_id FROM spec WHERE project_id=? AND spec_id=?",
            (pid, child_spec_id),
        ).fetchone()
        if not parent:
            return mcp_error(
                f"Spec '{parent_spec_id}' not found",
                hint="check `list_specs()` for known spec ids",
            )
        if not child:
            return mcp_error(
                f"Spec '{child_spec_id}' not found",
                hint="check `list_specs()` for known spec ids",
            )
        descendants: set[int] = set()
        frontier = [int(child["id"])]
        while frontier:
            current = frontier.pop()
            for r in st.conn.execute(
                "SELECT child_spec_id FROM spec_dependency WHERE parent_spec_id=?",
                (current,),
            ):
                cid = int(r["child_spec_id"])
                if cid in descendants:
                    continue
                descendants.add(cid)
                if cid == int(parent["id"]):
                    return mcp_error(
                        f"would create a cycle: {child_spec_id} already "
                        f"transitively depends on {parent_spec_id}",
                        hint="walk the existing graph with `get_spec_dependency_graph` to see the conflicting path",
                    )
                frontier.append(cid)
        cur = st.conn.execute(
            """INSERT OR IGNORE INTO spec_dependency(parent_spec_id, child_spec_id, kind)
               VALUES(?,?,?)""",
            (int(parent["id"]), int(child["id"]), kind),
        )
        return {
            "linked": cur.rowcount > 0,
            "parent": parent_spec_id,
            "child": child_spec_id,
            "kind": kind,
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def link_spec_dependency(
        parent_spec_id: str,
        child_spec_id: str,
        kind: Literal["requires", "extends", "conflicts"] = "requires",
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Declare that one Spec depends on another (Spec-Spec edge).

        Semantics:
        - `requires`  : parent needs child to be implemented first (the
                        common case — SPEC-API needs SPEC-AUTH).
        - `extends`   : parent specializes child's behavior (SPEC-EXPORT-PDF
                        extends SPEC-EXPORT).
        - `conflicts` : the two cannot both be active (mutually exclusive
                        rollouts).

        Idempotent on (parent, child, kind). Cycles are rejected at insert
        time: if adding the edge would create parent → … → parent in the
        forward closure, the call returns isError=True without writing.

        Self-loops are rejected by the schema CHECK constraint.
        """
        return _do_link_spec_dependency(parent_spec_id, child_spec_id, kind, workspace)

    def _do_unlink_spec_dependency(
        parent_spec_id: str,
        child_spec_id: str,
        kind: str | None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        parent = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?",
            (pid, parent_spec_id),
        ).fetchone()
        child = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?",
            (pid, child_spec_id),
        ).fetchone()
        if not parent or not child:
            return {"unlinked": 0, "parent": parent_spec_id, "child": child_spec_id}
        if kind is None:
            cur = st.conn.execute(
                "DELETE FROM spec_dependency WHERE parent_spec_id=? AND child_spec_id=?",
                (int(parent["id"]), int(child["id"])),
            )
        else:
            cur = st.conn.execute(
                """DELETE FROM spec_dependency WHERE parent_spec_id=? AND child_spec_id=?
                   AND kind=?""",
                (int(parent["id"]), int(child["id"]), kind),
            )
        return {
            "unlinked": cur.rowcount,
            "parent": parent_spec_id,
            "child": child_spec_id,
            "kind": kind,
        }

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": True})
    def unlink_spec_dependency(
        parent_spec_id: str,
        child_spec_id: str,
        kind: Literal["requires", "extends", "conflicts"] | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Remove a Spec dependency edge. If `kind` is None, drops every edge
        between the pair regardless of kind. Idempotent.
        """
        return _do_unlink_spec_dependency(parent_spec_id, child_spec_id, kind, workspace)

    def _do_get_spec_dependency_graph(
        spec_id: str,
        direction: str,
        max_depth: int,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        st = get_state(workspace)
        pid = st.project_id
        root = st.conn.execute(
            "SELECT id, spec_id FROM spec WHERE project_id=? AND spec_id=?",
            (pid, spec_id),
        ).fetchone()
        if not root:
            return mcp_error(
                f"Spec '{spec_id}' not found",
                hint="check `list_specs()` for known spec ids",
            )
        root_id = int(root["id"])

        visited: set[int] = {root_id}
        edges: list[tuple[int, int, str]] = []

        def walk(start: int, forward: bool) -> None:
            # BFS (popleft): reach each spec at its shortest dependency depth
            # so a node found first via a longer chain isn't pinned past
            # max_depth and its nearer dependencies dropped.
            frontier: deque[tuple[int, int]] = deque([(start, 0)])
            while frontier:
                node, depth = frontier.popleft()
                if depth >= max_depth:
                    continue
                if forward:
                    rows = st.conn.execute(
                        """SELECT parent_spec_id, child_spec_id, kind FROM spec_dependency
                           WHERE parent_spec_id=?""",
                        (node,),
                    )
                else:
                    rows = st.conn.execute(
                        """SELECT parent_spec_id, child_spec_id, kind FROM spec_dependency
                           WHERE child_spec_id=?""",
                        (node,),
                    )
                for r in rows:
                    edges.append((int(r["parent_spec_id"]), int(r["child_spec_id"]), r["kind"]))
                    next_id = int(r["child_spec_id"]) if forward else int(r["parent_spec_id"])
                    if next_id not in visited:
                        visited.add(next_id)
                        frontier.append((next_id, depth + 1))

        if direction in ("forward", "both"):
            walk(root_id, forward=True)
        if direction in ("backward", "both"):
            walk(root_id, forward=False)

        # Resolve metadata for visited specs (chunked to stay under SQLite's
        # host-parameter cap on a very large dependency graph).
        spec_meta: dict[int, dict[str, Any]] = {}
        visited_ids = list(visited)
        for i in range(0, len(visited_ids), 900):
            batch = visited_ids[i : i + 900]
            ph = ",".join("?" * len(batch))
            for r in st.conn.execute(
                f"SELECT id, spec_id, title, status, priority FROM spec WHERE id IN ({ph})",
                batch,
            ):
                spec_meta[int(r["id"])] = {
                    "spec_id": r["spec_id"],
                    "title": r["title"],
                    "status": r["status"],
                    "priority": r["priority"],
                }

        # Dedupe edges
        edge_keys: set[tuple[int, int, str]] = set()
        edge_payload: list[dict[str, Any]] = []
        for p, c, k in edges:
            key = (p, c, k)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edge_payload.append({
                "parent": spec_meta.get(p, {}).get("spec_id"),
                "child": spec_meta.get(c, {}).get("spec_id"),
                "kind": k,
            })

        return {
            "root": spec_id,
            "direction": direction,
            "nodes": list(spec_meta.values()),
            "edges": edge_payload,
        }

    @mutation_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_spec_dependency_graph(
        spec_id: str,
        direction: Literal["forward", "backward", "both"] = "both",
        max_depth: int = 5,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Walk the Spec dependency graph from a given Spec.

        - forward:  what does this Spec depend on (children, transitively)?
        - backward: what depends on this Spec (parents, transitively)?
        - both:     union of both.

        Returns the visited Spec metadata + the edges traversed.
        """
        return _do_get_spec_dependency_graph(spec_id, direction, max_depth, workspace)

    # ---------- v0.22: OpenSpec (Fission-AI) round-trip + change lifecycle ----------

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def export_openspec(
        out_dir: str = "openspec",
        include_changes: bool = True,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Write the project's Specs to an on-disk OpenSpec (Fission-AI) tree.

        Closes the round-trip with `import_specs_from_markdown` / `sync_openspec`:
        emits `<out_dir>/specs/<capability>/spec.md` (canonical requirements +
        `#### Scenario:` WHEN/THEN blocks) and, when `include_changes`, the
        `changes/` and `archive/` change packages. Capability == the spec's
        `module`; module-less specs land under a `general` capability. Only
        non-deprecated specs are emitted as canonical requirements. `out_dir` is
        resolved inside the workspace root.
        """
        st = get_state(workspace)
        from livespec_mcp.domain.openspec_export import export_openspec as _export

        try:
            root = st.settings.safe_path(out_dir)
        except ValueError as e:
            return mcp_error(str(e), hint="out_dir must stay inside the workspace root")
        return _export(st.conn, st.project_id, root, include_changes=include_changes)

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def validate_openspec(
        strict: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Validate the Spec set against OpenSpec structural rules.

        Mirrors `openspec validate [--strict]`. The load-bearing check is
        OpenSpec's own invariant — every requirement MUST have >=1 scenario;
        also flags missing titles, empty bodies, and missing RFC-2119
        normative keywords (SHALL/MUST/SHOULD/MAY). Returns `{valid,
        error_count, warning_count, findings, specs_without_scenarios,
        hygiene, ...}` — `findings` groups issues by `(issue, severity)`
        with a count, `project_level` flag, and a bounded sample of
        `{spec_id, title}`; `hygiene` reports aggregate stats (e.g. average
        description length). Missing-scenario and missing-normative-keyword
        findings are `warning` severity normally, `error` under `strict`
        (which is what gates `valid`); missing-title is always an error.
        """
        st = get_state(workspace)
        from livespec_mcp.domain.openspec_validate import validate_openspec as _validate

        return _validate(st.conn, st.project_id, strict=strict)

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    @_workspace_note
    def sync_openspec(
        openspec_dir: str | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Import an entire OpenSpec tree — specs AND change proposals — in one call.

        Discovers the OpenSpec root (`openspec_dir` if given, else `<workspace>/
        openspec`), reads `openspec.json` if present, imports canonical
        requirements from `specs/` and ingests every change under `changes/`
        (proposed) and `archive/` (archived). Idempotent: re-run to re-sync.
        For a single spec file use `import_specs_from_markdown` instead.
        """
        st = get_state(workspace)
        from livespec_mcp.domain.openspec_discover import (
            discover_openspec_root,
            sync_openspec_tree,
        )

        root = discover_openspec_root(st.settings.workspace, openspec_dir)
        if root is None:
            return mcp_error(
                "no OpenSpec directory found",
                hint=(
                    "pass openspec_dir=<path>, or create <workspace>/openspec/ "
                    "(with specs/ and optional changes/)"
                ),
            )
        return sync_openspec_tree(st, root)

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def list_spec_changes(
        status: str | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """List OpenSpec change proposals. Filter by status (proposed|applied|archived).

        Returns each change's name, status, delta count, and timestamps.
        """
        st = get_state(workspace)
        pid = st.project_id
        sql = [
            """SELECT c.id, c.name, c.status, c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM spec_change_delta d WHERE d.change_id=c.id)
                          AS delta_count
               FROM spec_change c WHERE c.project_id=?"""
        ]
        args: list[Any] = [pid]
        if status:
            sql.append("AND c.status=?")
            args.append(status)
        sql.append("ORDER BY c.name")
        rows = [dict(r) for r in st.conn.execute(" ".join(sql), args)]
        return {"changes": rows}

    @agentic_tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def get_spec_change(
        name: str,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Get one OpenSpec change: proposal/design/tasks prose + its delta requirements."""
        st = get_state(workspace)
        pid = st.project_id
        ch = st.conn.execute(
            """SELECT id, name, status, proposal, design, tasks, created_at, updated_at
               FROM spec_change WHERE project_id=? AND name=?""",
            (pid, name),
        ).fetchone()
        if ch is None:
            return mcp_error(
                f"change {name!r} not found",
                hint="check `list_spec_changes()` for known change names",
            )
        deltas = [
            dict(d)
            for d in st.conn.execute(
                """SELECT operation, capability, spec_id, title, description
                   FROM spec_change_delta WHERE change_id=? ORDER BY ordinal, id""",
                (int(ch["id"]),),
            )
        ]
        out = dict(ch)
        out.pop("id", None)
        out["deltas"] = deltas
        return out

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    @_workspace_note
    def apply_spec_change(
        name: str,
        dry_run: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Apply an OpenSpec change: fold its deltas into the canonical Spec set.

        ADDED/MODIFIED requirements are upserted and activated; REMOVED
        requirements are deprecated; RENAMED moves the old requirement's
        traceability links onto the new name and drops the old spec. Marks the
        change `applied`. Idempotent.

        `dry_run=True` returns the `plan` (counts per operation) + applicability
        `warnings` (e.g. MODIFIED/REMOVED targeting a spec that doesn't exist, or
        ADDED that would overwrite an existing one) WITHOUT mutating — preview
        before committing. A normal apply returns the same `warnings`.
        """
        st = get_state(workspace)
        from livespec_mcp.domain.openspec_changes import apply_change

        result = apply_change(st, name, dry_run=dry_run)
        if result.get("isError"):
            return mcp_error(
                result["error"],
                hint="check `list_spec_changes()` for known change names",
            )
        return result

    @mutation_tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    def archive_spec_change(
        name: str,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Mark an OpenSpec change archived (completed). Idempotent."""
        st = get_state(workspace)
        from livespec_mcp.domain.openspec_changes import archive_change

        result = archive_change(st, name)
        if result.get("isError"):
            return mcp_error(
                result["error"],
                hint="check `list_spec_changes()` for known change names",
            )
        return result
