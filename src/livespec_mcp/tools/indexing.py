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
from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.indexer import index_project as run_index
from livespec_mcp.domain.rag import rebuild_chunks
from livespec_mcp.state import AppState, get_state
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

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
    files = st.conn.execute(
        "SELECT COUNT(*) c FROM file WHERE project_id=?", (pid,)
    ).fetchone()["c"]
    syms = st.conn.execute(
        "SELECT COUNT(*) c FROM symbol s JOIN file f ON f.id=s.file_id WHERE f.project_id=?",
        (pid,),
    ).fetchone()["c"]
    edges = st.conn.execute(
        """SELECT COUNT(*) c FROM symbol_edge e JOIN symbol s ON s.id=e.src_symbol_id
           JOIN file f ON f.id=s.file_id WHERE f.project_id=?""",
        (pid,),
    ).fetchone()["c"]
    specs = st.conn.execute(
        "SELECT COUNT(*) c FROM spec WHERE project_id=?", (pid,)
    ).fetchone()["c"]
    return {
        "workspace": str(st.settings.workspace),
        "project_id": pid,
        "files": int(files),
        "symbols": int(syms),
        "edges": int(edges),
        "specs": int(specs),
        "last_run": dict(last) if last else None,
    }


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
    return payload


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


def _should_build_explorer(st: AppState, explorer: bool) -> bool:
    """Return whether ``index_project`` should (re)generate the Spec Explorer bundle.

    True when any of:

    * ``explorer=True`` (explicit opt-in),
    * the bundle already exists (freshness — keep it from going stale),
    * the workspace looks like a FastAPI app (``main.py`` / ``app.py`` with
      ``app = FastAPI(...)``) and no bundle exists yet (first-index autodetect).
    """
    if explorer:
        return True
    explorer_dir = st.settings.state_dir / "explorer"
    if explorer_dir.exists():
        return True
    from livespec_mcp.explorer.autowire import find_fastapi_entrypoints

    return bool(find_fastapi_entrypoints(st.settings.workspace))


def _maybe_regenerate_explorer(st: AppState, explorer: bool) -> bool:
    """Refresh the static Spec Explorer bundle to keep it from going stale.

    Regenerates when :func:`_should_build_explorer` is true (explicit flag,
    existing bundle, or FastAPI entry autodetect on first index). Any failure
    is logged and swallowed — a bad explorer build must never break the index
    pipeline. Returns whether the bundle was (re)written.

    Refreshes on EVERY index (not gated on code changes) because specs/links
    can change without any file changing; the cost of the refresh itself was
    cut by the v0.20 coverage-BFS inversion (H3) and the single
    compute_endpoints pass, so an always-fresh bundle stays affordable.
    """
    if not _should_build_explorer(st, explorer):
        return False
    try:
        # Imported here (not at module top) so explorer.py — owned by another
        # surface — is only loaded when a bundle refresh is actually needed.
        from livespec_mcp.tools.explorer import write_explorer_bundle

        write_explorer_bundle(st)
        return True
    except Exception:
        _log.exception("Spec Explorer bundle regeneration failed; skipping")
        return False


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
    total_before = sum(before.values())
    total_after = sum(after.values())
    result["external_edges_stale"] = {
        "by_origin": before,
        "surviving_by_origin": after,
        "dropped_by_reextract": total_before - total_after,
        "hint": (
            f"This run changed files. {total_before - total_after} of "
            f"{total_before} ingested edges went with the symbols that were "
            f"re-extracted; {total_after} survive and may no longer match the "
            "code. Regenerate the external graph and re-run "
            "ingest_external_graph, or ingest_external_graph(remove=True)."
        ),
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


def _delete_external_edges(
    st: AppState, project_ids: list[int] | int, origin: str
) -> int:
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


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False})
    def index_project(
        force: bool = False,
        watch: bool = False,
        explorer: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Walk the workspace, parse code, persist symbols + call edges.

        File-incremental via xxh3 content hash; pass force=True to re-extract.
        Respects .gitignore and an optional .livespec.toml at the workspace
        root ([index] table: ignore, languages, max_file_bytes — config
        patterns outrank .gitignore). Pass watch=True to also start a
        filesystem watcher after indexing so subsequent edits trigger
        automatic re-index (debounce 2s). Rebuilds FTS5 search chunks
        idempotently. Pass explorer=True to (re)generate the static Spec
        Explorer bundle (.mcp-docs/explorer/) after indexing; it is also
        auto-refreshed whenever that bundle already exists, so the viewer
        never goes stale.
        When ``[specs].sync_from`` is set in ``.livespec.toml``, markdown
        specs are re-imported after each index (idempotent). Optional
        ``[specs].links_seed`` replays ``bulk_link_spec_symbols`` from JSON.
        On a first index, a FastAPI workspace (``app = FastAPI(...)`` in
        ``main.py`` / ``app.py``) auto-builds the bundle and autowires
        ``mount_explorer(app)`` when ``[explorer] auto_mount = true``.
        A bundle-regeneration failure never breaks indexing — it is logged
        and skipped. The payload reports `explorer_regenerated`.
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
        result["explorer_regenerated"] = _maybe_regenerate_explorer(st, explorer)
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

    ingest_external_graph.__doc__ = (
        ingest_external_graph.__doc__ or ""
    ) + WORKSPACE_DOCSTRING_NOTE

    # Append the shared workspace note as a real docstring. A bare f-string as
    # the first statement is an expression, not a docstring, so __doc__ would be
    # None and the MCP client would see no description for this flagship tool.
    index_project.__doc__ = (index_project.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE
