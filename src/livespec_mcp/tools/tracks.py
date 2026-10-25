"""Track tools: the evolutionary history of code ↔ spec.

A track is a macro-commit — a named ``base..head`` range plus why it exists —
recorded per Spec in ``CHANGESPEC.md`` at the workspace root. The flow an
agent follows:

1. ``propose_track`` — draft: Specs changed (an OpenSpec delta, or the
   primary ``@spec:`` of changed code) and Specs only touched through shared
   code, symbols added / modified / removed per Spec, OpenSpec deltas, code
   with no Spec. Writes nothing.
2. The agent drafts a why per Spec and the person confirms them.
3. ``record_track`` — appends (or updates) the entry in ``CHANGESPEC.md`` and
   re-ingests it into the index.
4. ``get_spec_history`` / ``get_symbol_history`` — read the timeline back.

The domain logic lives in ``domain/tracks.py`` and ``domain/changespec.py``;
this module only validates input and shapes errors.
"""

from __future__ import annotations

import difflib
from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain.changespec import (
    CHANGESPEC_FILE,
    entry_from_draft,
    ingest_changespec,
    recorded_reasons,
    spec_aliases,
    spec_history,
    symbol_history,
    why_has_touched_line,
    write_entry,
)
from livespec_mcp.domain.spec_alias import deprecation_note, resolve_spec_id
from livespec_mcp.domain.tracks import (
    SymbolChange,
    TrackDraft,
    draft_track,
    normalize_symbol_query,
    symbols_by_change,
)
from livespec_mcp.state import AppState, get_state
from livespec_mcp.storage.db import transaction
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.tools.analysis import did_you_mean_symbols
from livespec_mcp.workspace_param import WORKSPACE_DOCSTRING_NOTE, Workspace

_MAX_LIMIT = 500


def _workspace_note(fn):
    fn.__doc__ = (fn.__doc__ or "") + WORKSPACE_DOCSTRING_NOTE
    return fn


def _git_error(problem: Any) -> dict[str, Any]:
    return mcp_error(problem.error, did_you_mean=problem.did_you_mean, hint=problem.hint)


def _all_changes(draft: TrackDraft) -> list[SymbolChange]:
    seen: dict[int, SymbolChange] = {}
    for t in draft.specs.values():
        for c in t.symbols:
            seen[id(c)] = c
    for c in draft.unlinked:
        seen[id(c)] = c
    return list(seen.values())


def _recorded_ids(st: AppState) -> set[tuple[str, str]]:
    return {
        (r["base_sha"], r["head_sha"])
        for r in st.conn.execute(
            "SELECT base_sha, head_sha FROM track WHERE project_id=?", (st.project_id,)
        )
    }


def _page(items: list[Any], limit: int, cursor: int) -> tuple[list[Any], int | None]:
    limit = max(1, min(int(limit), _MAX_LIMIT))
    cursor = max(0, int(cursor))
    page = items[cursor : cursor + limit]
    return page, (cursor + limit if cursor + limit < len(items) else None)


def _spec_payload(t: Any) -> dict[str, Any]:
    by = symbols_by_change(t.symbols)
    return {
        "spec_id": t.spec_id,
        "title": t.title,
        "tier": t.tier,
        "known": t.known,
        "deltas": [d.as_payload() for d in t.deltas],
        "symbols": by,
        "counts": {k: len(v) for k, v in by.items()},
    }


def _draft_counts(draft: TrackDraft) -> dict[str, int]:
    every = _all_changes(draft)
    return {
        "files_changed": len(draft.files),
        "specs_touched": len(draft.specs),
        "specs_changed": len(draft.changed_specs),
        "specs_touched_only": len(draft.touched_specs),
        "symbols_added": sum(c.change == "added" for c in every),
        "symbols_modified": sum(c.change == "modified" for c in every),
        "symbols_removed": sum(c.change == "removed" for c in every),
        "unlinked_symbols": len(draft.unlinked),
        "spec_deltas": sum(len(t.deltas) for t in draft.specs.values()),
    }


def _close(value: str, universe: set[str]) -> list[str]:
    return difflib.get_close_matches(value, sorted(universe), n=5, cutoff=0.5)


def _paged_lists(
    lists: dict[str, list[Any]], limit: int, cursor: int
) -> tuple[dict[str, list[Any]], int | None]:
    """Page every list with the same ``limit`` / ``cursor``; ``next_cursor``
    is set while any of them has more."""
    out: dict[str, list[Any]] = {}
    nxt: int | None = None
    for k, items in lists.items():
        out[k], n = _page(items, limit, cursor)
        if n is not None:
            nxt = n if nxt is None else max(nxt, n)
    return out, nxt


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def propose_track(
        base_ref: str,
        head_ref: str = "HEAD",
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Draft a track for ``base_ref..head_ref`` (a branch, a PR, a run of
        commits): which Specs it reached and, per Spec, which symbols it
        added / modified / removed, plus OpenSpec requirement deltas and the
        code it changed with no Spec (``unlinked``). Writes nothing.

        Specs come in two tiers. ``changed_specs``: the range has an OpenSpec
        delta for the Spec, or the Spec is the primary explicit annotation
        (first id of the first ``@spec:`` / ``@implements:`` line) of a symbol
        it changed. ``touched_specs``: every other Spec, reached only through
        shared code (secondary ids, verb-anchored mentions, link-table rows).

        Symbol-level: ``modified`` means the normalised body hash differs, so
        reformat-only edits do not count; renamed files pair their symbols.
        Spec mapping uses ``@spec:`` annotations and the link table at head
        and base-side annotations for removed symbols — run ``index_project``
        first so links are current.

        Next step: draft one why per Spec from the diff, show them to the
        person, and only once they confirm call ``record_track`` with the same
        refs; a why is expected for changed Specs, touched ones need none.
        ``limit`` / ``cursor`` page ``changed_specs``, ``touched_specs`` and
        each ``unlinked`` list (counts stay exact); ``summary_only`` returns
        counts and spec ids."""
        st = get_state(workspace)
        draft, problem = draft_track(
            st.conn, st.project_id, str(st.settings.workspace), base_ref, head_ref
        )
        if problem is not None:
            return _git_error(problem)
        assert draft is not None
        out: dict[str, Any] = {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "base_sha": draft.base_sha,
            "head_sha": draft.head_sha,
            "track_id": draft.track_id,
            "date": draft.date,
            "openspec_changes": draft.changes,
            "already_recorded": (draft.base_sha, draft.head_sha) in _recorded_ids(st),
            "counts": _draft_counts(draft),
            "files_unparsed": [{"path": p, "reason": r} for p, r in sorted(draft.unparsed.items())],
            "next": (
                "Draft a one-line why per spec (and one overall), confirm them "
                "with the person, then call record_track(base_ref, head_ref, "
                "title, why, spec_reasons) — it writes CHANGESPEC.md."
            ),
        }
        if summary_only:
            out["changed_spec_ids"] = [t.spec_id for t in draft.changed_specs]
            out["touched_spec_ids"] = [t.spec_id for t in draft.touched_specs]
            return out
        specs, nxt = _paged_lists(
            {
                "changed_specs": [_spec_payload(t) for t in draft.changed_specs],
                "touched_specs": [_spec_payload(t) for t in draft.touched_specs],
            },
            limit,
            cursor,
        )
        unlinked, n = _paged_lists(symbols_by_change(draft.unlinked), limit, cursor)
        if n is not None:
            nxt = n if nxt is None else max(nxt, n)
        out.update(specs)
        out["unlinked"] = unlinked
        out["next_cursor"] = nxt
        return out

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True, "destructiveHint": False})
    @_workspace_note
    def record_track(
        base_ref: str,
        head_ref: str,
        title: str,
        why: str,
        spec_reasons: dict[str, str] | None = None,
        include_touched_sections: bool = False,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Record the track ``base_ref..head_ref`` in ``CHANGESPEC.md`` at the
        workspace root (created if absent) and in the index.

        Call it only after ``propose_track`` and after the person confirmed
        ``title``, ``why`` and the per-Spec reasons (``spec_reasons``: spec id →
        why; keys must be Specs the range touched, legacy ids accepted).
        Changed Specs get a ``### <spec-id>`` section; touched ones go on one
        ``Also touched:`` line unless ``include_touched_sections`` is true or
        they have a reason, which gives them a section too.
        Idempotent: the same base/head updates its entry instead of adding a
        second one, and keeps the per-Spec reasons this call does not restate.
        Returns ``changed_specs`` / ``touched_specs`` (spec ids, paged by
        ``limit`` / ``cursor``, exact ``counts``); ``summary_only`` drops the
        lists and ``entry_markdown``. ``CHANGESPEC.md`` is the source of truth — the index is
        rebuilt from it on every ``index_project``. It is one of the two writes
        livespec makes into a project; the other is
        ``plan_schema_change(apply=True)``, into the declaration files of
        ``apply_workspaces`` only, each checked against its indexed hash."""
        st = get_state(workspace)
        if not title or not title.strip():
            return mcp_error("title is required", hint="a short name for the track")
        if not why or not why.strip():
            return mcp_error(
                "why is required", hint="the overall reason this range exists, in one line"
            )
        if why_has_touched_line(why):
            return mcp_error(
                "why has a line CHANGESPEC.md would read as the touched Specs",
                hint="drop the '<!-- livespec:touched -->' marker from that line",
            )
        ws = st.settings.workspace
        draft, problem = draft_track(st.conn, st.project_id, str(ws), base_ref, head_ref)
        if problem is not None:
            return _git_error(problem)
        assert draft is not None
        reasons: dict[str, str] = {}
        deprecated: list[dict[str, str]] = []
        for key, reason in (spec_reasons or {}).items():
            sid, alias = resolve_spec_id(st.conn, st.project_id, key)
            if sid not in draft.specs:
                known = {
                    r["spec_id"]
                    for r in st.conn.execute(
                        "SELECT spec_id FROM spec WHERE project_id=?", (st.project_id,)
                    )
                }
                touched = set(draft.specs)
                if sid in known:
                    msg = f"spec '{key}' is not touched by {base_ref}..{head_ref}"
                else:
                    msg = f"unknown spec id '{key}'"
                return mcp_error(
                    msg,
                    did_you_mean=_close(sid, touched) or _close(sid, known),
                    hint="spec_reasons keys must be spec ids propose_track listed "
                    f"for this range: {sorted(touched)[:20]}",
                )
            if (note := deprecation_note(alias, sid)) is not None:
                deprecated.append(note)
            reasons[sid] = reason
        with st.lock():
            kept = recorded_reasons(ws, draft.base_sha, draft.head_sha)
            entry = entry_from_draft(
                draft,
                title=title,
                why=why,
                spec_reasons={
                    **{k: v for k, v in kept.items() if k in draft.specs},
                    **reasons,
                },
                include_touched_sections=include_touched_sections,
            )
            written = write_entry(ws, entry)
            with transaction(st.conn):
                ingest = ingest_changespec(st.conn, st.project_id, ws)
        out: dict[str, Any] = {
            "track_id": entry.track_id,
            "base_sha": entry.base_sha,
            "head_sha": entry.head_sha,
            "file": written["path"],
            "created": written["created"],
            "updated": not written["created"],
            "counts": _draft_counts(draft),
            "tracks_in_file": ingest.get("tracks"),
        }
        if not summary_only:
            lists, nxt = _paged_lists(
                {
                    "changed_specs": [t.spec_id for t in draft.changed_specs],
                    "touched_specs": [t.spec_id for t in draft.touched_specs],
                },
                limit,
                cursor,
            )
            out.update(lists)
            out["next_cursor"] = nxt
            out["entry_markdown"] = written["entry_markdown"]
        if ingest.get("problems"):
            out["changespec_problems"] = ingest["problems"]
        if deprecated:
            out["deprecated_spec_ids"] = deprecated
        return out

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def get_spec_history(
        spec_id: str,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Timeline of the tracks that touched one Spec, newest first: when,
        the track's title and why, ``tier`` (``changed``: the track changed
        this Spec; ``touched``: only reached it through shared code), the
        per-Spec why, the OpenSpec deltas and the symbols added / modified /
        removed for it (none for a Spec on a track's ``Also touched`` line). Read from the index,
        which ``index_project`` rebuilds from ``CHANGESPEC.md``.

        Legacy spec ids (aliases) are accepted. Paginated (``limit`` /
        ``cursor``, exact ``count``); ``summary_only`` returns only the track
        ids, dates and tiers."""
        st = get_state(workspace)
        pid = st.project_id
        sid, alias = resolve_spec_id(st.conn, pid, spec_id)
        ids = {spec_id} | spec_aliases(st.conn, pid, sid)
        rows = spec_history(st.conn, pid, ids)
        exists = st.conn.execute(
            "SELECT 1 FROM spec WHERE project_id=? AND spec_id=?", (pid, sid)
        ).fetchone()
        if not rows and exists is None:
            universe = {
                r["spec_id"]
                for r in st.conn.execute("SELECT spec_id FROM spec WHERE project_id=?", (pid,))
            } | {
                r["spec_id"]
                for r in st.conn.execute(
                    """SELECT DISTINCT ts.spec_id FROM track_spec ts
                       JOIN track t ON t.id=ts.track_id WHERE t.project_id=?""",
                    (pid,),
                )
            }
            return mcp_error(
                f"Spec '{spec_id}' not found",
                did_you_mean=_close(spec_id, universe),
                hint="check `list_specs()` for known spec ids",
            )
        out: dict[str, Any] = {"spec_id": sid, "count": len(rows)}
        if (note := deprecation_note(alias, sid)) is not None:
            out["deprecated_spec_ids"] = [note]
        if not rows:
            out["hint"] = (
                f"no recorded track touched {sid}; tracks come from {CHANGESPEC_FILE} "
                "(propose_track → record_track)"
            )
        if summary_only:
            out["tracks"] = [
                {"track_id": r["track_id"], "date": r["date"], "tier": r["tier"]} for r in rows
            ]
            return out
        items, nxt = _page(rows, limit, cursor)
        out["tracks"] = items
        out["next_cursor"] = nxt
        return out

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @_workspace_note
    def get_symbol_history(
        symbol: str,
        limit: int = 200,
        cursor: int = 0,
        summary_only: bool = False,
        workspace: Workspace | None = None,
    ) -> dict[str, Any]:
        """Tracks that changed a symbol, newest first, with the Specs each
        track touched through it and their whys. ``symbol`` is a qualified
        name or the name local to its module (``Class.method``, ``fn``);
        separators ``::``, ``#`` and ``.`` are interchangeable. Removed
        symbols keep their history.

        Paginated (``limit`` / ``cursor``, exact ``count``); ``summary_only``
        returns only track ids and dates."""
        st = get_state(workspace)
        pid = st.project_id
        q = normalize_symbol_query(symbol)
        tracks, matched = symbol_history(st.conn, pid, q)
        if not tracks:
            in_index = st.conn.execute(
                """SELECT 1 FROM symbol s JOIN file f ON f.id=s.file_id
                   WHERE f.project_id=? AND (s.qualified_name=? OR s.name=?) LIMIT 1""",
                (pid, q, q),
            ).fetchone()
            if in_index is None:
                track_names = {
                    r["qualified_name"]
                    for r in st.conn.execute(
                        """SELECT DISTINCT x.qualified_name FROM track_symbol x
                           JOIN track t ON t.id=x.track_id WHERE t.project_id=?""",
                        (pid,),
                    )
                }
                return mcp_error(
                    f"Symbol '{symbol}' not found",
                    did_you_mean=_close(q, track_names)
                    or [d.get("qualified_name") for d in did_you_mean_symbols(st.conn, pid, q)],
                    hint="run `find_symbol(query=<short_name>)` to discover qualified names",
                )
            return {
                "symbol": symbol,
                "count": 0,
                "tracks": [],
                "next_cursor": None,
                "hint": f"no recorded track changed this symbol ({CHANGESPEC_FILE})",
            }
        out: dict[str, Any] = {
            "symbol": symbol,
            "matched": matched,
            "count": len(tracks),
        }
        if summary_only:
            out["tracks"] = [{"track_id": g["track_id"], "date": g["date"]} for g in tracks]
            return out
        page, nxt = _page(tracks, limit, cursor)
        out["tracks"] = page
        out["next_cursor"] = nxt
        return out
