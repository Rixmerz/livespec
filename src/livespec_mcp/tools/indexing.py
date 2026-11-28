"""Indexing tool: index_project.

Every tool accepts ``workspace`` (absolute project root). Pass it on each call
when one MCP server handles multiple repos — no ``LIVESPEC_WORKSPACE`` in
mcp.json and no restart (LRU cache in ``get_state``).

v0.9 P6: `get_index_status` removed (deprecated in v0.8 P3.2). Read the
`project://index/status` resource for the same payload.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from livespec_mcp.domain.indexer import index_project as run_index
from livespec_mcp.domain.languages import EXTRACTOR_SUPPORTED, grammar_fix_hint
from livespec_mcp.domain.rag import rebuild_chunks
from livespec_mcp.state import AppState, get_state
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

if TYPE_CHECKING:
    from fastmcp import FastMCP

_log = logging.getLogger("livespec.indexing")


def compute_index_status(st: AppState) -> dict[str, Any]:
    """Module-level so resources.py keeps a stable shape.

    The tool wrapper around this helper was removed in v0.9 P6 — the
    `project://index/status` resource is the canonical surface now.
    """
    pid = st.project_id
    last = st.conn.execute(
        "SELECT * FROM index_run WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    files = st.conn.execute("SELECT COUNT(*) c FROM file WHERE project_id=?", (pid,)).fetchone()[
        "c"
    ]
    syms = st.conn.execute(
        "SELECT COUNT(*) c FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?",
        (pid,),
    ).fetchone()["c"]
    edges = st.conn.execute(
        """SELECT COUNT(*) c FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id
           JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (pid,),
    ).fetchone()["c"]
    specs = st.conn.execute("SELECT COUNT(*) c FROM spec WHERE project_id=?", (pid,)).fetchone()[
        "c"
    ]
    status: dict[str, Any] = {
        "workspace": str(st.settings.workspace),
        "project_id": pid,
        "files": int(files),
        "symbols": int(syms),
        "edges": int(edges),
        "specs": int(specs),
        "last_run": dict(last) if last else None,
    }
    # Only when traces were ingested, so the shape is unchanged otherwise.
    from livespec_mcp.domain.trace_ingest import public_runtime_state

    runtime = public_runtime_state(st.conn, pid)
    if runtime is not None:
        status["runtime_evidence"] = runtime
    return status


def run_index_pipeline(st: AppState, *, force: bool = False) -> dict[str, Any]:
    """Index + idempotent chunk rebuild. Shared by the `index_project` MCP
    tool and the `livespec index` CLI subcommand — both surfaces must report
    the same payload shape."""
    with st.lock():
        stats = run_index(st.settings, st.conn, force=force)
        existing = st.conn.execute(
            "SELECT COUNT(*) c FROM chunk WHERE project_id=?", (st.project_id,)
        ).fetchone()["c"]
        # A deletion-only change increments nothing in files_changed, but its
        # chunks (FTS) must be pruned — otherwise search keeps returning hits
        # for deleted files. stats.files_deleted covers that.
        if force or stats.files_changed or stats.files_deleted or existing == 0:
            chunk_stats: dict[str, Any] = dict(rebuild_chunks(st.conn, st.project_id))
        else:
            chunk_stats = {"skipped": "no file changes"}
    payload: dict[str, Any] = {
        "files_total": stats.files_total,
        "files_changed": stats.files_changed,
        "files_skipped": stats.files_skipped,
        "files_deleted": stats.files_deleted,
        "symbols_total": stats.symbols_total,
        "edges_total": stats.edges_total,
        "spec_links_created": stats.spec_links_created,
        "manual_links_restored": stats.manual_links_restored,
        "languages": stats.languages,
        "languages_unsupported": stats.languages_unsupported,
        "repo_config": stats.repo_config,
        "workspace": str(st.settings.workspace),
        "watcher_started": False,
        "chunks": chunk_stats,
    }
    _attach_grammar_failures(payload, stats)
    _attach_zero_symbol_warning(payload, stats, st)
    _attach_tracks(st, payload)
    _record_in_registry(st)
    return payload


def _attach_tracks(st: AppState, payload: dict[str, Any]) -> None:
    """Rebuild the track tables from ``CHANGESPEC.md`` (the source of truth).

    Read-only on the workspace: indexing never writes CHANGESPEC.md. A
    malformed entry is reported under ``tracks.problems``; any other failure
    is logged and reported, never costs the index run."""
    try:
        from livespec_mcp.domain.changespec import ingest_changespec
        from livespec_mcp.storage.db import transaction

        with st.lock(), transaction(st.conn):
            payload["tracks"] = ingest_changespec(st.conn, st.project_id, st.settings.workspace)
    except Exception as e:
        _log.exception("CHANGESPEC.md ingest failed; skipping")
        payload["tracks"] = {"error": f"CHANGESPEC.md ingest failed: {e}"}


def _record_in_registry(st: AppState) -> None:
    """Note this workspace in the user-level registry `livespec ui` reads.

    The registry lives outside every repository (see `livespec_mcp.registry`).
    Best-effort by design: a read-only home directory or a corrupt registry
    must never cost the user their index run.
    """
    try:
        from livespec_mcp.registry import record_state

        record_state(st)
    except Exception:
        _log.exception("could not record workspace in the livespec registry; skipping")


def _attach_grammar_failures(payload: dict[str, Any], stats: Any) -> None:
    """Say out loud when a language was skipped because its grammar is missing.

    `tree-sitter-language-pack` 1.x downloads grammars on first use, so an
    offline or proxied machine indexes Python (stdlib `ast`) and nothing else.
    Reported rather than logged because the number an agent reads next —
    `find_dead_code`, `audit_coverage`, any count at all — is wrong by the
    whole of those languages, and nothing else in the payload hints at it.
    """
    if not stats.languages_failed:
        return
    total = sum(stats.languages_failed.values())
    payload["languages_failed"] = stats.languages_failed
    payload["languages_failed_hint"] = (
        f"{total} file(s) in {len(stats.languages_failed)} language(s) were "
        "SKIPPED because their tree-sitter grammar could not be loaded "
        "(tree-sitter-language-pack downloads grammars on first use). Symbol, "
        "edge and dead-code counts exclude them entirely. Run "
        "`livespec grammars` once with network access, then re-index — the "
        "files were left unindexed on purpose so they are retried."
    )
    # One entry per language with the actual error and a fix for THAT error
    # (a proxy's CA, no network, no zstd decoder) — the counts above say how
    # much is missing, this says why and what to do.
    payload["parser_errors"] = [
        {
            "language": lang,
            "files": stats.languages_failed.get(lang, 0),
            "error": err,
            "hint": grammar_fix_hint(err),
        }
        for lang, err in sorted((stats.parser_errors or {}).items())
    ]


def _attach_zero_symbol_warning(payload: dict[str, Any], stats: Any, st: AppState) -> None:
    """Top-level `warning` when a language had files this run but 0 symbols.

    The failure this guards against: `languages: {typescript: 136}` next to
    `symbols_total: 0` reads as a successful index of an empty repo, and every
    Spec feature downstream then silently does nothing. Counted from the DB
    (what the project holds for that language now), so a language whose
    grammar failed but whose symbols survive from an earlier run is NOT
    flagged — those symbols are stale but real, and `parser_errors` already
    says the files were skipped.

    Tree-sitter languages only: a Python package of empty `__init__.py`
    files legitimately has zero symbols; a TS/JS file always yields at least
    its module symbol.
    """
    seen = {
        lang: n
        for lang, n in (stats.languages or {}).items()
        if n and lang in EXTRACTOR_SUPPORTED and lang != "python"
    }
    if not seen:
        return
    rows = st.conn.execute(
        """SELECT f.language AS language, COUNT(s.id) AS n
           FROM file f LEFT JOIN symbol s ON s.file_id = f.id
           WHERE f.project_id = ? GROUP BY f.language""",
        (st.project_id,),
    ).fetchall()
    have = {r["language"]: int(r["n"]) for r in rows}
    empty = sorted(lang for lang in seen if have.get(lang, 0) == 0)
    if not empty:
        return
    failed = set(stats.languages_failed or {})
    parts = ", ".join(f"{lang} ({seen[lang]} file(s))" for lang in empty)
    why = (
        "its tree-sitter parser could not be loaded — see `parser_errors` for the error and the fix"
        if set(empty) <= failed
        else "the extractor produced nothing for them"
    )
    payload["warning"] = (
        f"0 symbols were extracted for {parts}: {why}. Until that is fixed, "
        "call graph, dead-code, endpoint and @spec: coverage results for these "
        "languages are empty, not clean."
    )


def _external_edge_counts(st: AppState) -> dict[str, int]:
    from livespec_mcp.domain.graph import external_edge_summary

    return external_edge_summary(st.conn, st.project_id) or {}


def _attach_external_edge_staleness(
    st: AppState, result: dict[str, Any], before: dict[str, int]
) -> None:
    """Report what this index run did to edges ingested from an external graph.

    Silent on an index nobody has ingested into, and on a run that changed
    nothing. Otherwise it says how many ingested edges the run destroyed and
    how many survived — the survivors being the more dangerous half, since they
    are still answering `who_calls` from a graph built against code that has
    since moved."""
    if not before:
        return
    if not (result.get("files_changed") or result.get("files_deleted")):
        return
    after = _external_edge_counts(st)
    from livespec_mcp.domain.trace_ingest import RUNTIME_ORIGIN

    total_before = sum(before.values())
    total_after = sum(after.values())
    hint = (
        f"This run changed files. {total_before - total_after} of "
        f"{total_before} ingested edges went with the symbols that were "
        f"re-extracted; {total_after} survive and may no longer match the "
        "code."
    )
    if set(before) - {RUNTIME_ORIGIN}:
        hint += (
            " Regenerate the external graph and re-run "
            "ingest_external_graph, or ingest_external_graph(remove=True)."
        )
    if RUNTIME_ORIGIN in before:
        hint += (
            f" `{RUNTIME_ORIGIN}` edges and hits are dropped with a "
            "re-extracted symbol on purpose (the body that ran is gone): run "
            "the traced program again and re-run ingest_traces."
        )
    result["external_edges_stale"] = {
        "by_origin": before,
        "surviving_by_origin": after,
        "dropped_by_reextract": total_before - total_after,
        "hint": hint,
    }


def _maybe_auto_ingest(st: AppState, result: dict[str, Any]) -> None:
    """Re-apply a recorded ingest after a run that changed files, if asked.

    Off unless `[graph] auto_ingest = true`: an ingest writes rows into
    `symbol_edge`, and a write that happens because someone saved a file is not
    something to opt anyone into silently.

    On, it closes the window the staleness report can only describe. A
    re-extract cascades ingested edges away with the symbols they pointed at,
    and the rows that survive are derived from a graph built against code that
    has since moved. Re-running the ingest from the same graph rebuilds both
    halves against the current index.

    The graph comes from the recorded provenance first (whatever was actually
    ingested last, with the same relations) and falls back to `[graph]
    external`. Never fatal: a missing or unreadable graph leaves the staleness
    report to say so, exactly as it would have without this.
    """
    from livespec_mcp.domain.external_ingest import EXTERNAL_ORIGIN, read_ingest

    if not (result.get("files_changed") or result.get("files_deleted")):
        return
    from livespec_mcp.config import load_repo_config

    cfg = load_repo_config(st.settings.workspace)
    if not cfg.graph_auto_ingest:
        return
    prior = read_ingest(st.conn, st.project_id, EXTERNAL_ORIGIN)
    graph_path = prior[0]["graph_path"] if prior else cfg.external_graph
    if not graph_path:
        return
    relations: list[str] | None = None
    if prior and prior[0].get("relations"):
        try:
            relations = json.loads(prior[0]["relations"])
        except (TypeError, ValueError):
            relations = None
    try:
        outcome = _run_external_ingest(
            st, resolved_path=graph_path, relations=relations, dry_run=False
        )
    except Exception:
        _log.exception("auto ingest failed; leaving the staleness report to speak")
        return
    if outcome.get("isError"):
        # Not fatal: the staleness report below already says the edges may no
        # longer match, which is exactly the situation an unreadable graph
        # leaves us in. Failing the index over it would be worse.
        result["external_ingest_refresh_failed"] = outcome.get("error")
        return
    result["external_ingest_refreshed"] = {
        "source": outcome.get("source"),
        "edges_added": outcome.get("edges_added"),
        "edges_replaced": outcome.get("edges_replaced"),
    }


def _delete_external_edges(st: AppState, project_ids: list[int] | int, origin: str) -> int:
    """Delete exactly the edges one external origin wrote into these projects.

    The `_resolve_refs` contract forbids DELETEing from `symbol_edge` — refs
    from unchanged files must survive when the files they target change. That
    rule is about the *resolver*, which cannot know whether an absent ref means
    "gone" or "not re-parsed this run". Here the answer is not in doubt: these
    rows exist only because a previous ingest of this origin put them there,
    and the predicate cannot reach a livespec-derived row.

    Scoped by the SOURCE symbol's project. Over a group DB that covers every
    edge exactly once — including a cross-repo edge, whose source lives in one
    of these projects even though its target lives in another.
    """
    ids = [project_ids] if isinstance(project_ids, int) else list(project_ids)
    placeholders = ",".join("?" for _ in ids)
    cur = st.conn.execute(
        f"""DELETE FROM symbol_edge
           WHERE origin = ?
             AND src_symbol_id IN (
               SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id
               WHERE f.project_id IN ({placeholders})
             )""",
        (origin, *ids),
    )
    return int(cur.rowcount or 0)


def _run_external_ingest(
    st: AppState,
    *,
    resolved_path: str,
    relations: list[str] | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Plan (and optionally apply) an ingest. Shared by the tool and auto-ingest.

    Module level rather than a closure inside `register()` so `index_project`
    can re-apply a recorded ingest after a re-extract without going through the
    MCP tool — and so the write path has exactly one implementation.
    """
    from livespec_mcp.domain.external_ingest import (
        DEFAULT_RELATIONS,
        EXTERNAL_ORIGIN,
        IMPORT_RELATIONS,
        plan_ingest,
        record_ingest,
        sample_edges,
    )
    from livespec_mcp.domain.external_source import (
        load_gated_external_graph,
        unknown_relations_report,
    )
    from livespec_mcp.domain.graph import invalidate_graph_cache
    from livespec_mcp.tools._errors import mcp_error

    pid = st.project_id
    # Every project in the group, not just the home one. `graphify merge-graphs`
    # produces a single graph.json spanning several repos, and a group DB is the
    # one place livespec can hold both ends of an edge between them — the exact
    # cross-repo dependency `route_ref` only covers when it happens to be HTTP.
    # Identical to the previous behaviour on an ungrouped workspace, where
    # `group_project_ids()` is `[project_id]`.
    pids = st.group_project_ids()
    placeholders = ",".join("?" for _ in pids)
    indexed_files = {
        r["path"]
        for r in st.conn.execute(
            f"SELECT path FROM file WHERE project_id IN ({placeholders})", tuple(pids)
        )
    }
    graph, overlap, problem = load_gated_external_graph(
        st.settings.workspace,
        resolved_path,
        indexed_files,
        keep_link_meta=True,
    )
    if problem is not None:
        return mcp_error(problem.message, hint=problem.hint)

    symbols = [
        dict(r)
        for r in st.conn.execute(
            "SELECT s.id, s.name, s.qualified_name, s.start_line, "
            "f.path AS file_path, f.project_id AS project_id "
            "FROM symbol s JOIN file f ON f.id = s.file_id "
            f"WHERE f.project_id IN ({placeholders})",
            tuple(pids),
        )
    ]
    # Rows this ingest owns are excluded from "already known" because the apply
    # path deletes them first. Without that, a second dry run would report every
    # edge as agreed and predict a no-op for a run that actually rewrites them.
    existing = {
        (int(r["src_symbol_id"]), int(r["dst_symbol_id"]), r["edge_type"])
        for r in st.conn.execute(
            f"""SELECT e.src_symbol_id, e.dst_symbol_id, e.edge_type
               FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id IN ({placeholders}) AND e.origin <> ?""",
            (*pids, EXTERNAL_ORIGIN),
        )
    }
    plan = plan_ingest(
        graph,
        symbols,
        existing,
        relations=frozenset(relations) if relations else None,
    )

    sym_meta = {int(r["id"]): r for r in symbols}
    payload: dict[str, Any] = {
        "source": graph.path,
        "origin": EXTERNAL_ORIGIN,
        "dry_run": dry_run,
        "relations": sorted(relations or DEFAULT_RELATIONS),
        "external_nodes": graph.node_count,
        "external_edges": graph.edge_count,
        "file_overlap": round(overlap, 3),
        "mapped_nodes": plan.mapped_nodes,
        "ambiguous_nodes": plan.ambiguous_nodes,
        "projects": len(pids),
        "edges_to_add": plan.edge_count,
        "edges_by_relation": dict(sorted(plan.by_relation.items())),
        "already_known": plan.agreed,
        "already_known_by_relation": dict(sorted(plan.agreed_by_relation.items())),
        "skipped": dict(sorted(plan.skipped.items())),
        "sample": sample_edges(plan, sym_meta),
    }
    if plan.ambiguous_cross_project:
        payload["ambiguous_cross_project"] = plan.ambiguous_cross_project
        payload["ambiguous_cross_project_hint"] = (
            f"{plan.ambiguous_cross_project} external node(s) were claimed by "
            "symbols in different repos of this group and dropped. Each repo "
            "stores paths relative to its own root, so two repos sharing a path "
            "(src/index.ts) produce one key both match. Dropping avoids writing "
            "an edge into the wrong repo; re-running will not change it."
        )
    payload.update(unknown_relations_report(graph))
    if graph.has_non_ast_origin:
        payload["warning"] = (
            "Some external edges are not marked `_origin: ast` — this graph "
            "may include LLM-derived (semantic) edges, unlike a code-only "
            "Graphify run. Those would land in your call graph."
        )
    if relations and set(relations) & IMPORT_RELATIONS:
        payload["import_relations_warning"] = (
            "Import relations are being ingested. `who_calls` does not "
            "distinguish edge types, so importers will be reported as "
            "callers. Run with remove=True to undo."
        )

    if dry_run:
        payload["next"] = (
            "ingest_external_graph(dry_run=False) to apply, or "
            "find_dead_code(corroborate_with=…) if you only want dead-code "
            "filtering without writing to the index."
        )
        return payload

    # Under the lock `index_project` takes: a re-extract landing between the
    # DELETE and the INSERT cascades the symbols away and the INSERT fails on a
    # foreign key, with no shaped error to show for it.
    with st.lock():
        replaced = _delete_external_edges(st, pids, EXTERNAL_ORIGIN)
        st.conn.executemany(
            """INSERT OR IGNORE INTO
               symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight, origin)
               VALUES(?,?,?,?,?)""",
            [
                (
                    e.src_symbol_id,
                    e.dst_symbol_id,
                    e.edge_type,
                    e.weight,
                    EXTERNAL_ORIGIN,
                )
                for e in plan.new_edges
            ],
        )
        record_ingest(
            st.conn,
            pid,
            origin=EXTERNAL_ORIGIN,
            graph_path=graph.path,
            relations=frozenset(relations) if relations else DEFAULT_RELATIONS,
            edges_written=plan.edge_count,
        )
        # The DELETE above removed every row this origin owned, including a
        # pair a trace also observed and the new graph no longer reports.
        from livespec_mcp.domain.trace_ingest import restore_runtime_edges

        restore_runtime_edges(st.conn, pids)
        st.conn.commit()
    for project in pids:
        invalidate_graph_cache(project)
    payload["edges_added"] = plan.edge_count
    payload["edges_replaced"] = replaced
    payload["hint"] = (
        "These edges are now part of the call graph and will show up in "
        "who_calls / analyze_impact / find_dead_code, labelled "
        f"`{EXTERNAL_ORIGIN}`. Re-run after any index_project that changed "
        "files; remove=True takes them back out."
    )
    return payload


def _run_trace_ingest(st: AppState, *, paths: list[Any], dry_run: bool) -> dict[str, Any]:
    """Plan (and optionally apply) a FlowTrace ingest. One write path."""
    from livespec_mcp.domain.graph import invalidate_graph_cache
    from livespec_mcp.domain.trace_ingest import (
        RUNTIME_ORIGIN,
        SymbolResolver,
        apply_plan,
        classify_edges,
        read_traces,
        sample_new_edges,
    )
    from livespec_mcp.tools._errors import mcp_error

    pid = st.project_id
    pids = st.group_project_ids()
    placeholders = ",".join("?" for _ in pids)
    plan = read_traces(paths, SymbolResolver(st.conn, pids, st.settings.workspace))

    if plan.enter_events == 0:
        return mcp_error(
            "No FlowTrace `enter` events in the trace.",
            hint=(
                f"Read {plan.events_read} event(s) and {plan.malformed_lines} "
                "malformed line(s). Expected FlowTrace schema v2 JSONL: one "
                'object per line with "event": "enter", trace_id and span_id.'
            ),
        )
    if not plan.hits:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(plan.unresolved.items()))
        return mcp_error(
            f"None of the {plan.enter_events} enter events resolved to an indexed symbol.",
            hint=(
                f"Unresolved by reason: {reasons}. {plan.with_location} event(s) "
                "carried a `file`. `file` / `line` are emitted by FlowTrace "
                "releases whose schema v2 includes them — with an older capture "
                "layer only the name fallback runs, and it needs a method + "
                "class that is unique in its module (overloads and same-named "
                "functions are skipped, never guessed). Also check that the "
                "trace is of THIS workspace and that index_project has run."
            ),
        )

    # Rows this origin owns are excluded from "already known", exactly as the
    # external ingest does: a second dry run of the same file must report the
    # same new edges as the first, not a no-op.
    known = {
        (int(r["src_symbol_id"]), int(r["dst_symbol_id"]))
        for r in st.conn.execute(
            f"""SELECT e.src_symbol_id, e.dst_symbol_id
               FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id IN ({placeholders})
                 AND e.edge_type = 'calls' AND e.origin <> ?""",
            (*pids, RUNTIME_ORIGIN),
        )
    }
    classify_edges(plan, known)

    # Names only for the edges the sample will show (the most-travelled ones).
    top = sorted(plan.new_edges, key=lambda pair: (-plan.edges[pair], pair))[:20]
    sample_ids = {i for pair in top for i in pair}
    sym_meta: dict[int, dict[str, Any]] = {}
    if sample_ids:
        ids = sorted(sample_ids)
        sym_meta = {
            int(r["id"]): dict(r)
            for r in st.conn.execute(
                f"SELECT id, qualified_name FROM symbol WHERE id IN ({','.join('?' * len(ids))})",
                tuple(ids),
            )
        }

    sources = [
        {
            "path": src.path,
            "enter_events": src.enter_events,
            "resolved_events": src.resolved_events,
            "malformed_lines": src.malformed_lines,
        }
        for src in plan.sources[:20]
    ]
    payload: dict[str, Any] = {
        "origin": RUNTIME_ORIGIN,
        "dry_run": dry_run,
        "trace_files": len(plan.sources),
        "sources": sources,
        "events_read": plan.events_read,
        "enter_events": plan.enter_events,
        "malformed_lines": plan.malformed_lines,
        "traces": plan.traces,
        "langs": dict(sorted(plan.langs.items())),
        "events_with_location": plan.with_location,
        "resolved_events": plan.resolved_events,
        "resolved_by": dict(sorted(plan.resolved_by.items())),
        "unresolved_events": sum(plan.unresolved.values()),
        "unresolved": dict(sorted(plan.unresolved.items())),
        "unresolved_samples": {k: v for k, v in sorted(plan.unresolved_samples.items())},
        "symbols_hit": len(plan.hits),
        "edges_observed": len(plan.edges),
        "edges_to_add": len(plan.new_edges),
        "already_known": plan.confirmed_edges,
        "skipped": dict(sorted(plan.edges_skipped.items())),
        "discarded_spans": plan.discarded_spans,
        "sample": sample_new_edges(plan, sym_meta),
    }
    if plan.discarded_spans:
        payload["discarded_note"] = (
            f"{plan.discarded_spans} span(s) were not credited as evidence: they "
            "entered before their parent, or after it had exited on the same "
            "thread, which no program does (an older FlowTrace wrote them for a "
            "__repr__ it ran itself and for generator frames). They add no edge "
            "and no hit, so they cannot rescue a symbol from find_dead_code. "
            "Re-record the trace with a current FlowTrace to be sure of the rest."
        )
    if len(plan.sources) > len(sources):
        payload["sources_truncated"] = len(plan.sources) - len(sources)

    if dry_run:
        payload["next"] = (
            "ingest_traces(trace_path=…, dry_run=False) to apply. "
            "`edges_to_add` are calls the static graph lacks; `already_known` "
            "are calls it had and the trace confirms."
        )
        return payload

    # Under the lock `index_project` takes, for the reason the external ingest
    # gives: a re-extract between the DELETE and the INSERT cascades symbols
    # away and the INSERT fails on a foreign key.
    # One transaction: the connection is in autocommit mode, and an ingest that
    # stopped halfway would leave hits without the edges they came with.
    import sqlite3

    from livespec_mcp.storage.db import transaction

    try:
        with st.lock(), transaction(st.conn):
            written = apply_plan(st.conn, pid, pids, plan)
    except sqlite3.IntegrityError:
        return mcp_error(
            "The index changed while the trace was being read; nothing was written.",
            hint="A re-index replaced symbols this ingest resolved to. Run ingest_traces again.",
        )
    for project in pids:
        invalidate_graph_cache(project)
    payload.update(written)
    payload["hint"] = (
        "Observed calls are now part of the call graph, labelled "
        f"`{RUNTIME_ORIGIN}` (`via_external_edge` in who_calls), and "
        "find_dead_code reports a `runtime` tier per candidate. Evidence for a "
        "symbol is dropped when its file is re-extracted; remove=True takes "
        "everything back out."
    )
    return payload


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False})
    def index_project(
        force: bool = False,
        watch: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Walk the workspace, parse code, persist symbols + call edges.

        File-incremental via xxh3 content hash; pass force=True to re-extract.
        Respects .gitignore and an optional .livespec.toml at the workspace
        root ([index] table: ignore, languages, max_file_bytes — config
        patterns outrank .gitignore). Pass watch=True to also start a
        filesystem watcher after indexing so subsequent edits trigger
        automatic re-index (debounce 2s). Rebuilds FTS5 search chunks
        idempotently. Records the workspace in the user-level registry
        (outside the repo) that `livespec ui` reads; nothing UI-related is
        written into the workspace.
        When ``[specs].sync_from`` is set in ``.livespec.toml``, markdown
        specs are re-imported after each index (idempotent). Optional
        ``[specs].links_seed`` replays ``bulk_link_spec_symbols`` from JSON.
        Use after pulling new commits or when documentation feels stale.
        """
        st = get_state(workspace, create=True)
        # Sampled BEFORE the run, and that is the whole point. A re-extract
        # deletes and re-inserts the symbols of every changed file, and the FK
        # cascade takes their ingested edges with them — so by the time the run
        # is over, the rows most likely to have gone stale are the ones that no
        # longer exist to be counted. Comparing the two counts is the only way
        # to report what a run actually cost an ingest.
        external_before = _external_edge_counts(st)
        result = run_index_pipeline(st, force=force)
        _maybe_auto_ingest(st, result)
        _attach_external_edge_staleness(st, result, external_before)
        try:
            from livespec_mcp.domain.specs_sync import sync_specs_from_config

            specs_sync = sync_specs_from_config(st)
            if specs_sync is not None:
                result["specs_sync"] = specs_sync
        except Exception:
            _log.exception("specs sync failed; skipping")
        if watch:
            from livespec_mcp.domain.watcher import Watcher, register_watcher

            settings = st.settings
            db_path = settings.db_path

            def _do_reindex() -> None:
                # Run on a DEDICATED connection, not st.conn. The watcher fires
                # from a background thread; sharing the tool threads' connection
                # let its BEGIN/COMMIT interleave with concurrent tool calls —
                # dirty reads mid-index and, worse, unlocked writes joining the
                # indexer transaction and being silently rolled back. A private
                # WAL connection isolates the reindex; other connections see the
                # result once it commits. Invalidate the graph cache after so the
                # freshly-indexed graph is picked up on the next analysis call.
                from livespec_mcp.domain.graph import invalidate_graph_cache
                from livespec_mcp.storage.db import connect

                conn = connect(db_path)
                try:
                    stats = run_index(settings, conn)
                finally:
                    conn.close()
                if stats.files_changed or stats.files_deleted:
                    invalidate_graph_cache()

            ws_path = settings.workspace
            w = Watcher(workspace=ws_path, on_reindex=_do_reindex, debounce_seconds=2.0)
            register_watcher(ws_path, w)
            w.start()
            result["watcher_started"] = True
        return result

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "idempotentHint": True,
            "destructiveHint": False,
        }
    )
    def ingest_external_graph(
        graph_path: str | None = None,
        dry_run: bool = True,
        remove: bool = False,
        relations: list[str] | None = None,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Add a second extractor's edges to this index's call graph.

        Reads a [Graphify](https://github.com/Graphify-Labs/graphify)
        `graph.json` and writes the dependency edges livespec's own resolver
        missed into `symbol_edge`, tagged `origin='external:graphify'`. Unlike
        `find_dead_code(corroborate_with=…)`, which can only *remove* dead-code
        candidates and touches no table, ingested edges are real edges: after
        this, `who_calls`, `who_does_this_call`, `analyze_impact` and
        `find_dead_code` all see them.

        The three things worth knowing before running it:

        - **No symbol is ever created.** An edge is ingested only when both
          endpoints already resolve to symbols livespec extracted. Everything
          else is counted under `skipped.endpoint_not_indexed` and dropped.
        - **It is reversible and idempotent.** Every run first deletes the rows
          it wrote last time, so the result depends on the current graph and
          the current index, never on history. `remove=True` deletes them and
          writes nothing.
        - **`dry_run=True` is the default.** The first call reports what it
          would add — counts by relation, a sample of concrete edges, and how
          often the two extractors already agree. Call again with
          `dry_run=False` to apply.

        `relations` overrides the ingested set (default: `calls`,
        `indirect_call`, `inherits`, `mixes_in`, `uses`, `references`). The
        import relations — `imports`, `imports_from`, `re_exports` — are
        available but off by default: they describe module wiring, and putting
        "imported by" rows into the graph would make `who_calls` report
        importers as callers. Use `find_dead_code(corroborate_with=…)` when you
        want import evidence; it asks the broader question without writing
        anything.

        Re-run after any `index_project` that changed files: a re-extract can
        delete or move the symbols these edges point at.
        """
        from livespec_mcp.domain.external_ingest import (
            EXTERNAL_ORIGIN,
            RELATION_EDGE_TYPE,
            clear_ingest,
        )
        from livespec_mcp.domain.graph import invalidate_graph_cache
        from livespec_mcp.tools._errors import mcp_error

        st = get_state(workspace)
        pid = st.project_id

        if remove:
            # Under the same lock `index_project` takes. A reindex landing
            # between the DELETE and the COMMIT cascades symbols away
            # underneath it; the ingest path below is worse, because a
            # re-extract between its DELETE and its INSERT makes the INSERT
            # fail on a foreign key with no shaped error to show for it.
            group = st.group_project_ids()
            with st.lock():
                removed = _delete_external_edges(st, group, EXTERNAL_ORIGIN)
                clear_ingest(st.conn, pid, EXTERNAL_ORIGIN)
                # A pair both a graph and a trace reported is one row, owned by
                # whichever arrived first. If that was the graph, the call that
                # was observed at runtime must not leave with it.
                from livespec_mcp.domain.trace_ingest import restore_runtime_edges

                restore_runtime_edges(st.conn, group)
                st.conn.commit()
            for project in group:
                invalidate_graph_cache(project)
            return {
                "removed": removed,
                "origin": EXTERNAL_ORIGIN,
                "hint": (
                    "The call graph is livespec-only again. livespec's own "
                    "edges were never touched — the delete is scoped to rows "
                    "carrying this origin."
                ),
            }

        if relations is not None:
            unknown = sorted(set(relations) - set(RELATION_EDGE_TYPE))
            if unknown:
                return mcp_error(
                    f"Unknown external relation(s): {', '.join(unknown)}",
                    did_you_mean=sorted(RELATION_EDGE_TYPE),
                    hint=(
                        "These are Graphify's `relation` values. Omit "
                        "`relations` for the default dependency set."
                    ),
                )
            if not relations:
                return mcp_error(
                    "relations is empty — nothing to ingest.",
                    hint="Omit it for the default set, or name at least one relation.",
                )

        from livespec_mcp.domain.external_source import (
            resolve_external_graph_source,
        )

        resolved_path, availability_hint = resolve_external_graph_source(
            st.settings.workspace, graph_path
        )
        if not resolved_path:
            return mcp_error(
                "No external graph to ingest.",
                hint=(
                    availability_hint
                    or "Pass graph_path=<graphify-out/graph.json>, or set "
                    '`[graph] external = "graphify-out/graph.json"` in '
                    ".livespec.toml."
                ),
            )
        return _run_external_ingest(
            st, resolved_path=resolved_path, relations=relations, dry_run=dry_run
        )

    ingest_external_graph.__doc__ = (ingest_external_graph.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "idempotentHint": True,
            "destructiveHint": False,
        }
    )
    def ingest_traces(
        trace_path: str | list[str] | None = None,
        dry_run: bool = True,
        remove: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Add what actually ran (a FlowTrace trace) to this index's call graph.

        Reads FlowTrace schema v2 JSONL — one file, a list of files (one per
        process of a distributed trace), or a directory of `*.jsonl` — and
        resolves each `enter` event to a symbol livespec already indexed. Two
        things come out of it that no static pass can produce:

        - **Calls through dynamic dispatch.** `getattr(obj, name)()`, a
          registry of callables, DI, reflection: the caller -> callee pairs the
          trace observed are written to `symbol_edge` as `calls` edges tagged
          `origin='runtime:flowtrace'`, so `who_calls` / `who_does_this_call` /
          `analyze_impact` see them (marked `via_external_edge`).
          `edges_to_add` counts the ones the static graph lacks;
          `already_known` the ones it had and the trace confirms.
        - **Per-symbol hit counts**, which give `find_dead_code` its `runtime`
          tiers: a candidate that ran is dropped (`rescued_by_runtime`), one
          whose file was reached while it never executed is `not_seen` (the
          strongest removal evidence), one in a file no trace touched is
          `no_evidence`.

        What to know before running it:

        - **No symbol is ever created, and nothing is guessed.** An event
          resolves by `file` + `line` when the trace carries them (the method
          name must still agree with the symbol there), otherwise by method +
          class + module, and only when that names exactly one symbol.
          Everything else is counted under `unresolved` by reason, with
          samples.
        - **`dry_run=True` is the default.** It reports the plan and a sample
          of the new edges and writes nothing.
        - **Idempotent per trace file.** Ingesting a file again (same path, or
          the same content elsewhere) replaces what it contributed: hits are
          not double-counted, and an edge only that file supported is
          retracted. Different files accumulate. `remove=True` deletes exactly
          this origin's edges, hits and provenance; an edge livespec's own
          resolver derives is never touched.
        - **Evidence follows the code.** Re-indexing a changed file drops the
          evidence for its symbols — the body that ran is gone. Trace again
          and re-ingest. A trace is evidence of what one run did: `not_seen`
          means that run, not production.
        """
        from livespec_mcp.domain.graph import invalidate_graph_cache
        from livespec_mcp.domain.trace_ingest import (
            RUNTIME_ORIGIN,
            expand_trace_paths,
            remove_runtime,
        )
        from livespec_mcp.tools._errors import mcp_error

        st = get_state(workspace)

        if remove:
            group = st.group_project_ids()
            from livespec_mcp.storage.db import transaction

            with st.lock(), transaction(st.conn):
                gone = remove_runtime(st.conn, group)
            for project in group:
                invalidate_graph_cache(project)
            return {
                "removed": gone["edges"],
                "hits_removed": gone["symbols_with_hits"],
                "trace_files_removed": gone["sources"],
                "origin": RUNTIME_ORIGIN,
                "hint": (
                    "Runtime evidence is gone: the call graph and "
                    "find_dead_code are static-only again. Edges from any "
                    "other origin were never touched — the delete is scoped "
                    "to rows carrying this origin."
                ),
            }

        paths, problem = expand_trace_paths(st.settings.workspace, trace_path)
        if problem is not None:
            return mcp_error(problem.message, hint=problem.hint)
        return _run_trace_ingest(st, paths=paths, dry_run=dry_run)

    ingest_traces.__doc__ = (ingest_traces.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE

    # Append the shared workspace note as a real docstring. A bare f-string as
    # the first statement is an expression, not a docstring, so __doc__ would be
    # None and the MCP client would see no description for this flagship tool.
    index_project.__doc__ = (index_project.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE
