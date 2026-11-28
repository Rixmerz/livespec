"""Ingest runtime evidence from FlowTrace traces into livespec's graph.

`external_ingest.py` borrows a second *static* extractor's edges. This module
borrows the one thing no static extractor has: what actually ran. A FlowTrace
trace (schema v2, JSONL, one `enter` / `exit` event per line) says which
function was entered and which span was its caller, so it can supply

- **edges the resolver cannot derive** — `getattr(obj, name)()`, a registry
  dict of callables, DI, reflection. They land in `symbol_edge` as
  `edge_type='calls'`, `origin='runtime:flowtrace'`;
- **per-symbol hit counts** — the evidence `find_dead_code` needs to tell "no
  static caller, but it runs" from "no static caller, and the run that reached
  its file never executed it".

The boundaries are the ones the external ingest set, for the same reasons:

1. **The symbol table stays ours.** An event counts only when it resolves to a
   symbol livespec already extracted. Nothing is created; everything else is
   counted under `unresolved` with its reason.
2. **Never guess.** Location (`file` + `line`) is preferred and still has to
   agree with the event's method name — a trace recorded against older code
   points at lines that now belong to another function. The name fallback
   accepts a unique match only; two overloads are `ambiguous_name`, not a coin
   toss. A wrong edge in the call graph is worse than a missing one.
3. **Labelled and reversible.** Rows carry the origin; `remove_runtime` deletes
   exactly this origin's edges, hits and provenance. An edge livespec's own
   resolver derives is never re-tagged (INSERT OR IGNORE), and the resolver
   reclaims a runtime row to `livespec` the moment it derives the same edge.

**State is a function of the ingested trace files, not of history.** Evidence
is stored per source file (`runtime_source`, migration 29). Ingesting a file
again — same path, or the same content under another path — replaces what that
file contributed, so a re-ingest neither double-counts hits nor leaves behind
edges the file no longer supports. Different files accumulate: two test runs
are two sources.

Symbols are deleted and re-inserted when their file is re-extracted, and the
FK cascade takes their runtime rows with them. That is correct, not a leak: the
body changed, so the observation no longer describes it.

Pure domain: no import from `tools/`; every failure is returned as data.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import xxhash

from livespec_mcp.domain.location import CALLABLE_KINDS, symbols_spanning

#: Value written to `symbol_edge.origin` and to the evidence tables.
RUNTIME_ORIGIN = "runtime:flowtrace"

#: A call that was observed happening. Not a resolver guess, so it sits above
#: every `min_weight` filter — unlike an external extractor's claim, which is
#: capped below 1.0 because it is still an inference from source.
RUNTIME_EDGE_WEIGHT = 1.0

#: FlowTrace `lang` -> livespec `file.language` values it can land on.
_LANG_FAMILY: dict[str, frozenset[str]] = {
    "python": frozenset({"python"}),
    "java": frozenset({"java"}),
    "node": frozenset({"javascript", "typescript", "tsx"}),
    "ts": frozenset({"javascript", "typescript", "tsx"}),
    "go": frozenset({"go"}),
    "rust": frozenset({"rust"}),
}

_SAMPLES_PER_REASON = 3

#: `event` is the fifth key FlowTrace writes, so an `exit` line says so in its
#: first bytes. Half of every trace is exits; skipping the JSON parse for them
#: is the single biggest saving on a multi-hundred-MB file. Only the head is
#: searched because `args` (which could legitimately nest this text) comes
#: after it.
_EXIT_MARKERS = (b'"event":"exit"', b'"event": "exit"')
_HEAD = 240

#: `trace_id` and `span_id` are the 2nd and 3rd keys of every FlowTrace event,
#: so an exit line yields both without parsing the JSON.
_EXIT_IDS = re.compile(rb'"trace_id": ?"([^"]*)", ?"span_id": ?"([^"]*)"')

#: Per-span state is ONE int: `symbol id << 33 | thread index << 1 | exited`.
#: Unresolved spans keep symbol 0. Thread index 0 = unknown.
_SYM_SHIFT = 33
_THREAD_MASK = (1 << 32) - 1
#: Distinct (file, thread name) pairs remembered. A Go program can name millions
#: of goroutines; past the cap a span's thread is "unknown" and the exited-parent
#: check is simply not applied to it (the safe direction: no edge is dropped).
_MAX_THREADS = 100_000

#: FlowTrace `lang` values whose callbacks legitimately outlive their parent
#: span on the SAME thread label: every Node continuation (a `setTimeout`, a
#: promise reaction) runs on `main` after the function that scheduled it has
#: returned, carrying its span as parent. "Parent already exited" is no
#: evidence of an artifact there.
_ASYNC_CONTINUATION_LANGS = frozenset({"node", "ts"})


@dataclass(frozen=True)
class TraceProblem:
    """Why an ingest cannot run. Shaped into `mcp_error` by the tool."""

    message: str
    hint: str | None = None


@dataclass
class TraceSource:
    """What one trace file contributed."""

    path: str
    content_hash: str | None = None
    size: int | None = None
    mtime: float | None = None
    events: int = 0
    enter_events: int = 0
    resolved_events: int = 0
    malformed_lines: int = 0
    hits: dict[int, int] = field(default_factory=dict)
    edges: dict[tuple[int, int], int] = field(default_factory=dict)


@dataclass
class TracePlan:
    """What ingesting these traces into this index would do. Computed, not applied."""

    origin: str = RUNTIME_ORIGIN
    sources: list[TraceSource] = field(default_factory=list)
    #: symbol id -> times entered, across every source.
    hits: dict[int, int] = field(default_factory=dict)
    #: (caller id, callee id) -> times observed, across every source.
    edges: dict[tuple[int, int], int] = field(default_factory=dict)
    events_read: int = 0
    enter_events: int = 0
    malformed_lines: int = 0
    traces: int = 0
    langs: dict[str, int] = field(default_factory=dict)
    #: `enter` events that carried a `file`.
    with_location: int = 0
    #: How resolved events were resolved: `location` (file + line),
    #: `file_name` (file, then name inside it), `name` (no file at all).
    resolved_by: dict[str, int] = field(default_factory=dict)
    unresolved: dict[str, int] = field(default_factory=dict)
    unresolved_samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Parent/child pairs that produced no edge, by reason.
    edges_skipped: dict[str, int] = field(default_factory=dict)
    #: Spans dropped as order-impossible (`parent_not_yet_entered`,
    #: `parent_already_exited`): no edge AND no hit — the callee did run, but
    #: only because the tracer ran it, so it is not evidence.
    discarded_spans: int = 0
    #: Filled by `classify_edges`.
    new_edges: list[tuple[int, int]] = field(default_factory=list)
    confirmed_edges: int = 0

    @property
    def resolved_events(self) -> int:
        return sum(self.resolved_by.values())

    def _skip_edge(self, reason: str) -> None:
        self.edges_skipped[reason] = self.edges_skipped.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Which files to read
# ---------------------------------------------------------------------------


def expand_trace_paths(
    workspace: Path, trace_path: str | Sequence[str] | None
) -> tuple[list[Path], TraceProblem | None]:
    """One path, a list, or a directory (every `*.jsonl` in it) -> files.

    Relative paths resolve against the workspace root. Spans of one trace can
    be spread over several files (one per process), which is why a list is a
    first-class input and not a convenience.
    """
    if trace_path is None or trace_path == "" or trace_path == []:
        return [], TraceProblem(
            "No trace to ingest.",
            hint=(
                "Pass trace_path=<flowtrace.jsonl>, a list of them, or a "
                "directory containing *.jsonl files."
            ),
        )
    raw = [trace_path] if isinstance(trace_path, str) else list(trace_path)
    out: list[Path] = []
    seen: set[Path] = set()
    for item in raw:
        p = Path(item).expanduser()
        if not p.is_absolute():
            p = workspace / p
        if p.is_dir():
            found = sorted(q for q in p.glob("*.jsonl") if q.is_file())
            if not found:
                return [], TraceProblem(
                    f"No *.jsonl files in directory: {p}",
                    hint="FlowTrace writes flowtrace.jsonl by default.",
                )
        elif p.is_file():
            found = [p]
        else:
            return [], TraceProblem(
                f"Trace file not found: {p}",
                hint="Relative paths resolve against the workspace root.",
            )
        for q in found:
            key = q.resolve()
            if key not in seen:
                seen.add(key)
                out.append(q)
    return out, None


# ---------------------------------------------------------------------------
# Event -> symbol
# ---------------------------------------------------------------------------


def _plain(name: str) -> str:
    """Node reports a private method as `#name`; some extractors keep the `#`."""
    return name[1:] if name.startswith("#") else name


def _class_tail(cls: str) -> str:
    """`Outer$Inner` / `pkg.Outer.Inner` -> `Inner`."""
    return cls.replace("$", ".").rsplit(".", 1)[-1]


def _ends_with_segments(path: str, tail: str) -> bool:
    return path == tail or path.endswith("/" + tail)


def _module_matches(lang: str, module: str, path: str) -> bool | None:
    """Does this file belong to the event's `module`? None = cannot tell.

    `module` means something different per runtime, so each gets its own rule.
    None (rather than True) is returned where the module says nothing about
    the path — a Python script run as `__main__`, a Go `main` package — so the
    caller can skip narrowing instead of pretending it narrowed.
    """
    if not module:
        return None
    directory, _, base = path.rpartition("/")
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if lang == "python":
        if module == "__main__":
            return None
        dotted = module.replace(".", "/")
        without_ext = f"{directory}/{stem}" if directory else stem
        if stem == "__init__":
            return _ends_with_segments(directory, dotted)
        return _ends_with_segments(without_ext, dotted)
    if lang == "java":
        return _ends_with_segments(directory, module.replace(".", "/"))
    if lang == "go":
        if module == "main":
            return None
        return directory.rsplit("/", 1)[-1] == module
    if lang in ("node", "ts"):
        return stem == module or base == module
    if lang == "rust":
        # A crate name is not a path. It narrows only when some directory is
        # named after it; otherwise the crate is the workspace root itself.
        want = module.replace("-", "_")
        return any(seg.replace("-", "_") == want for seg in directory.split("/") if seg)
    return None


class SymbolResolver:
    """Resolve a trace event's identity to one livespec symbol id, or say why not.

    Results are cached per distinct `(lang, module, class, method, file, line)`
    — a trace repeats the same function millions of times, and the number of
    distinct keys is bounded by the size of the program, not of the trace.
    """

    def __init__(
        self, conn: sqlite3.Connection, project_ids: Sequence[int], workspace: Path | str
    ) -> None:
        self._conn = conn
        self._pids = list(project_ids)
        self._root = str(workspace).replace("\\", "/").rstrip("/")
        self._by_path: dict[str, list[int]] = {}
        self._by_base: dict[str, list[tuple[str, int]]] = {}
        placeholders = ",".join("?" * len(self._pids))
        for r in conn.execute(
            f"SELECT id, path FROM file WHERE project_id IN ({placeholders})",
            tuple(self._pids),
        ):
            path, fid = r["path"], int(r["id"])
            self._by_path.setdefault(path, []).append(fid)
            self._by_base.setdefault(path.rsplit("/", 1)[-1], []).append((path, fid))
        self._cache: dict[tuple, tuple[int | None, str]] = {}
        self._file_cache: dict[str, tuple[int | None, str]] = {}

    # -- files ------------------------------------------------------------

    def _suffix_matches(self, tail: str) -> list[int]:
        base = tail.rsplit("/", 1)[-1]
        return [fid for path, fid in self._by_base.get(base, ()) if _ends_with_segments(path, tail)]

    def resolve_file(self, file: str) -> tuple[int | None, str]:
        """Trace `file` -> indexed file id, or `(None, reason)`.

        Exact relative path first. Then a path-suffix match on whole segments,
        which is what a Java trace needs (`com/acme/Foo.java` is relative to
        the source root, not the repo) and what a monorepo needs when the
        traced project is a subdirectory. A suffix two files share is
        `ambiguous_file` — never the first one found.
        """
        cached = self._file_cache.get(file)
        if cached is not None:
            return cached
        result = self._resolve_file(file)
        self._file_cache[file] = result
        return result

    def _resolve_file(self, file: str) -> tuple[int | None, str]:
        needle = file.replace("\\", "/")
        absolute = needle.startswith("/") or (len(needle) > 2 and needle[1] == ":")
        if absolute:
            if self._root and needle.startswith(self._root + "/"):
                needle, absolute = needle[len(self._root) + 1 :], False
        else:
            while needle.startswith("./"):
                needle = needle[2:]
        if not needle:
            return None, "file_not_indexed"
        if not absolute:
            exact = self._by_path.get(needle)
            if exact:
                return (exact[0], "ok") if len(exact) == 1 else (None, "ambiguous_file")
            hits = self._suffix_matches(needle)
            if len(hits) == 1:
                return hits[0], "ok"
            return None, ("ambiguous_file" if hits else "file_not_indexed")
        # Absolute and outside the workspace (traced in a container, another
        # checkout): the longest tail that names an indexed file decides, and a
        # bare basename is never enough to claim a file.
        parts = [p for p in needle.split("/") if p]
        for start in range(1, max(len(parts) - 1, 1)):
            hits = self._suffix_matches("/".join(parts[start:]))
            if len(hits) == 1:
                return hits[0], "ok"
            if hits:
                return None, "ambiguous_file"
        return None, "file_not_indexed"

    # -- symbols ----------------------------------------------------------

    @staticmethod
    def _pick(rows: Iterable[Any], name: str, cls: str) -> list[Any]:
        """Callable rows named `name`, narrowed by `cls` the way the trace means it."""
        named = [r for r in rows if r["kind"] in CALLABLE_KINDS and _plain(r["name"]) == name]
        if cls:
            want = _class_tail(cls)
            return [r for r in named if (r["qualified_name"].split(".")[-2:-1] or [""])[0] == want]
        # A free function. Prefer symbols that are not methods; fall back to
        # everything only when the extractor models this language's functions
        # some other way — still subject to the unique-match rule.
        free = [r for r in named if r["kind"] == "function"]
        return free or named

    def _in_file(self, fid: int, name: str, cls: str) -> list[Any]:
        rows = self._conn.execute(
            "SELECT id, name, qualified_name, kind FROM symbol "
            "WHERE file_id = ? AND name IN (?, ?)",
            (fid, name, "#" + name),
        ).fetchall()
        return self._pick(rows, name, cls)

    def _by_name(self, lang: str, module: str, cls: str, name: str) -> tuple[int | None, str]:
        family = _LANG_FAMILY.get(lang)
        if family is None:
            return None, "unsupported_lang"
        pids = ",".join("?" * len(self._pids))
        langs = ",".join("?" * len(family))
        rows = self._conn.execute(
            f"SELECT s.id, s.name, s.qualified_name, s.kind, f.path "
            f"FROM symbol s JOIN file f ON f.id = s.file_id "
            f"WHERE s.name IN (?, ?) AND f.project_id IN ({pids}) "
            f"  AND f.language IN ({langs})",
            (name, "#" + name, *self._pids, *sorted(family)),
        ).fetchall()
        candidates = self._pick(rows, name, cls)
        if not candidates:
            return None, "no_match"
        verdicts = [(r, _module_matches(lang, module, r["path"])) for r in candidates]
        inside = [r for r, ok in verdicts if ok]
        if inside:
            candidates = inside
        elif lang != "rust":
            # The module names a place and none of the candidates are there.
            # Rust is the exception: a crate that IS the workspace root names
            # no directory, so "nowhere" proves nothing.
            candidates = [r for r, ok in verdicts if ok is None]
            if not candidates:
                return None, "no_match"
        if len(candidates) == 1:
            return int(candidates[0]["id"]), "name"
        return None, "ambiguous_name"

    def resolve(
        self,
        lang: str,
        module: str,
        cls: str,
        method: str,
        file: str | None,
        line: int | None,
    ) -> tuple[int | None, str]:
        """`(symbol_id, how)` on success, `(None, reason)` otherwise."""
        key = (lang, module, cls, method, file, line)
        cached = self._cache.get(key)
        if cached is None:
            cached = self._resolve(lang, module, cls, method, file, line)
            self._cache[key] = cached
        return cached

    def _resolve(
        self,
        lang: str,
        module: str,
        cls: str,
        method: str,
        file: str | None,
        line: int | None,
    ) -> tuple[int | None, str]:
        if lang == "browser":
            # Browser capture records http / router / error, not functions.
            return None, "unsupported_lang"
        name = _plain(method)
        if not name:
            return None, "no_method"
        if not file:
            return self._by_name(lang, module, cls, name)

        fid, reason = self.resolve_file(file)
        if fid is None:
            # Deliberately no name fallback: the trace named a file we do not
            # index (a dependency, generated code), so a same-named symbol
            # elsewhere is a different function.
            return None, reason
        failure = "no_match"
        if line is not None and line >= 1:
            spanning = [
                r
                for r in symbols_spanning(self._conn, self._pids, line, file_id=fid)
                if r["kind"] in CALLABLE_KINDS
            ]
            for r in spanning:  # innermost first
                if _plain(r["name"]) == name:
                    return int(r["id"]), "location"
            # Something callable spans the line under another name: the trace
            # and the index disagree about what lives there (the file moved on
            # since the trace was recorded). Try the name inside this file
            # before giving up — shifted lines do not move a unique function.
            failure = "location_name_mismatch" if spanning else "no_symbol_at_line"
        candidates = self._in_file(fid, name, cls)
        if len(candidates) == 1:
            return int(candidates[0]["id"]), "file_name"
        if len(candidates) > 1:
            return None, "ambiguous_name"
        return None, failure


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _as_line(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def read_traces(paths: Sequence[Path], resolver: SymbolResolver) -> TracePlan:
    """Stream the trace files once and build the plan. Writes nothing.

    Memory is bounded by the number of spans, never by the file: each line is
    parsed and dropped, and the only per-span state kept is one int per span
    (symbol id, thread, exited bit) so a later child can find its caller.
    Children that arrive before their parent — another process's file read
    first — wait in `pending` and are settled when the parent shows up.

    Two orders no real program produces are not evidence (`discard`):

    - a span whose `enter` is read before the `enter` of its parent *in the
      same file* (`parent_not_yet_entered`) — a caller cannot start after its
      callee. An older FlowTrace wrote exactly this for a user `__repr__` it
      ran itself while rendering a call's arguments. The same order across two
      files is a multi-process trace read in the other order, and settles
      normally;
    - a span entering on a thread after its parent had *exited* on that thread
      (`parent_already_exited`) — an older FlowTrace left a generator's span
      current after it yielded. Not applied to Node (continuations legitimately
      outlive the function that scheduled them) and never across threads or
      files (a hand-off to a worker is real). A parent whose `exit` is missing
      because the trace was cut is never "exited".

    A discarded span is no evidence at all: its callee gets no edge and no hit,
    so it cannot rescue a symbol from `find_dead_code`. What it called in turn
    is not followed — spans nested under one keep their evidence.
    """
    plan = TracePlan()
    spans: dict[str, dict[str, int]] = {}
    pending: dict[tuple[str, str], list[tuple[int, int]]] = {}
    threads: dict[tuple[int, str], int] = {}

    def thread_index(src_index: int, name: Any) -> int:
        if not isinstance(name, str):
            return 0
        key = (src_index, name)
        idx = threads.get(key)
        if idx is None:
            if len(threads) >= _MAX_THREADS:
                return 0
            idx = threads[key] = len(threads) + 1
        return idx

    def discard(src: TraceSource, callee: int, reason: str) -> None:
        """An order-impossible span: not an edge, and not a hit either."""
        plan._skip_edge(reason)
        plan.discarded_spans += 1
        left = src.hits.get(callee, 0) - 1
        if left > 0:
            src.hits[callee] = left
        else:
            src.hits.pop(callee, None)

    def edge(src: TraceSource, caller: int, callee: int) -> None:
        if caller == 0:
            plan._skip_edge("caller_unresolved")
        elif caller == callee:
            # Recursion is real but a self-loop tells no reader anything, and
            # the static resolver keeps them out too.
            plan._skip_edge("self_call")
        else:
            src.edges[(caller, callee)] = src.edges.get((caller, callee), 0) + 1

    for path in paths:
        src = TraceSource(path=str(path))
        try:
            stat = path.stat()
            src.size, src.mtime = stat.st_size, stat.st_mtime
        except OSError:
            pass
        digest = xxhash.xxh3_128()
        src_index = len(plan.sources)
        plan.sources.append(src)
        with path.open("rb") as fh:
            for raw in fh:
                digest.update(raw)
                if not raw.strip():
                    continue
                head = raw[:_HEAD]
                if _EXIT_MARKERS[0] in head or _EXIT_MARKERS[1] in head:
                    src.events += 1
                    m = _EXIT_IDS.search(head)
                    if m is not None:
                        mark = spans.get(m.group(1).decode("utf-8", "replace"))
                        if mark is not None:
                            key = m.group(2).decode("utf-8", "replace")
                            val = mark.get(key)
                            if val is not None:
                                mark[key] = val | 1
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    src.malformed_lines += 1
                    continue
                if not isinstance(ev, dict):
                    src.malformed_lines += 1
                    continue
                kind = ev.get("event")
                if kind == "exit":
                    src.events += 1
                    mark = (
                        spans.get(ev.get("trace_id"))
                        if isinstance(ev.get("trace_id"), str)
                        else None
                    )
                    if mark is not None and isinstance(ev.get("span_id"), str):
                        val = mark.get(ev["span_id"])
                        if val is not None:
                            mark[ev["span_id"]] = val | 1
                    continue
                trace_id, span_id = ev.get("trace_id"), ev.get("span_id")
                if kind != "enter" or not isinstance(trace_id, str) or not isinstance(span_id, str):
                    src.malformed_lines += 1
                    continue
                src.events += 1
                src.enter_events += 1

                lang = ev.get("lang") if isinstance(ev.get("lang"), str) else ""
                module = ev.get("module") if isinstance(ev.get("module"), str) else ""
                cls = ev.get("class") if isinstance(ev.get("class"), str) else ""
                method = ev.get("method") if isinstance(ev.get("method"), str) else ""
                file = ev.get("file") if isinstance(ev.get("file"), str) and ev["file"] else None
                line = _as_line(ev.get("line")) if file else None
                plan.langs[lang] = plan.langs.get(lang, 0) + 1
                if file:
                    plan.with_location += 1

                sid, how = resolver.resolve(lang, module, cls, method, file, line)
                if sid is None:
                    plan.unresolved[how] = plan.unresolved.get(how, 0) + 1
                    samples = plan.unresolved_samples.setdefault(how, [])
                    if len(samples) < _SAMPLES_PER_REASON:
                        sample: dict[str, Any] = {
                            "lang": lang,
                            "module": module,
                            "class": cls,
                            "method": method,
                        }
                        if file:
                            sample["file"] = file
                        if line is not None:
                            sample["line"] = line
                        if sample not in samples:
                            samples.append(sample)
                    sid = 0
                else:
                    plan.resolved_by[how] = plan.resolved_by.get(how, 0) + 1
                    src.resolved_events += 1
                    src.hits[sid] = src.hits.get(sid, 0) + 1

                trace_spans = spans.get(trace_id)
                if trace_spans is None:
                    trace_spans = spans[trace_id] = {}
                thread = thread_index(src_index, ev.get("thread"))
                trace_spans[span_id] = (sid << _SYM_SHIFT) | (thread << 1)
                waiting = pending.pop((trace_id, span_id), None)
                if waiting:
                    for child, child_src in waiting:
                        if child_src == src_index:
                            # Same file, child first: impossible order.
                            discard(src, child, "parent_not_yet_entered")
                        else:
                            edge(plan.sources[child_src], sid, child)
                parent = ev.get("parent_id")
                if sid and isinstance(parent, str) and parent:
                    state = trace_spans.get(parent)
                    if state is None:
                        pending.setdefault((trace_id, parent), []).append((sid, src_index))
                    elif (
                        state & 1
                        and thread
                        and (state >> 1) & _THREAD_MASK == thread
                        and lang not in _ASYNC_CONTINUATION_LANGS
                    ):
                        discard(src, sid, "parent_already_exited")
                    else:
                        edge(src, state >> _SYM_SHIFT, sid)
        src.content_hash = digest.hexdigest()

    # A parent that never appeared: a remote caller (incoming traceparent), or
    # the process whose file was not passed in.
    orphaned = sum(len(v) for v in pending.values())
    if orphaned:
        plan.edges_skipped["caller_not_in_traces"] = orphaned

    plan.traces = len(spans)
    for src in plan.sources:
        plan.events_read += src.events
        plan.enter_events += src.enter_events
        plan.malformed_lines += src.malformed_lines
        for sid, n in src.hits.items():
            plan.hits[sid] = plan.hits.get(sid, 0) + n
        for pair, n in src.edges.items():
            plan.edges[pair] = plan.edges.get(pair, 0) + n
    return plan


def classify_edges(plan: TracePlan, known: set[tuple[int, int]]) -> None:
    """Split observed edges into ones the graph already has and ones it lacks.

    `known` is every `(src, dst)` `calls` edge NOT owned by this origin — rows
    a previous trace ingest wrote are excluded so a second dry run of the same
    file reports the same answer as the first, exactly as the external ingest
    does. The new ones are the point: calls no static pass could see.
    """
    plan.new_edges = sorted(pair for pair in plan.edges if pair not in known)
    plan.confirmed_edges = len(plan.edges) - len(plan.new_edges)


def sample_new_edges(
    plan: TracePlan, sym_meta: dict[int, dict[str, Any]], limit: int = 20
) -> list[dict[str, Any]]:
    """The most-travelled new edges, readable — a dry run nobody can check is useless."""
    ranked = sorted(plan.new_edges, key=lambda pair: (-plan.edges[pair], pair))
    out: list[dict[str, Any]] = []
    for src, dst in ranked[:limit]:
        out.append(
            {
                "from": sym_meta.get(src, {}).get("qualified_name", str(src)),
                "to": sym_meta.get(dst, {}).get("qualified_name", str(dst)),
                "edge_type": "calls",
                "calls": plan.edges[(src, dst)],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Persistence (migration 29: runtime_source / runtime_hit / runtime_edge)
# ---------------------------------------------------------------------------


def _in(ids: Sequence[int]) -> str:
    return ",".join("?" * len(ids))


def _materialise(
    conn: sqlite3.Connection, project_ids: Sequence[int], origin: str
) -> tuple[int, int]:
    """Make `symbol_edge` agree with `runtime_edge`. Returns `(added, removed)`.

    INSERT OR IGNORE: a pair any other origin already holds — livespec's
    resolver above all — keeps its row and its label. The DELETE is scoped to
    this origin and to pairs no ingested trace supports any more, so it cannot
    reach a row someone else derived.
    """
    pids = tuple(project_ids)
    added = conn.execute(
        f"""INSERT OR IGNORE INTO
              symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight, origin)
            SELECT DISTINCT re.src_symbol_id, re.dst_symbol_id, 'calls', ?, ?
            FROM runtime_edge re JOIN runtime_source rs ON rs.id = re.source_id
            WHERE rs.origin = ? AND rs.project_id IN ({_in(pids)})""",
        (RUNTIME_EDGE_WEIGHT, origin, origin, *pids),
    ).rowcount
    removed = conn.execute(
        f"""DELETE FROM symbol_edge
            WHERE origin = ?
              AND src_symbol_id IN (
                SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id
                WHERE f.project_id IN ({_in(pids)}))
              AND NOT EXISTS (
                SELECT 1 FROM runtime_edge re
                WHERE re.src_symbol_id = symbol_edge.src_symbol_id
                  AND re.dst_symbol_id = symbol_edge.dst_symbol_id)""",
        (origin, *pids),
    ).rowcount
    return int(added or 0), int(removed or 0)


def restore_runtime_edges(conn: sqlite3.Connection, project_ids: Sequence[int]) -> int:
    """Re-assert runtime edges after another origin's rows were deleted.

    One row holds one origin. When an external graph and a trace both claim a
    pair, the first to arrive owns the row, so removing the external ingest
    would take a call that was observed at runtime out of the graph with it.
    Called after that delete, this puts such pairs back under this origin.
    A no-op (and tolerant of a pre-migration DB) when nothing was ingested.
    """
    try:
        added, _ = _materialise(conn, project_ids, RUNTIME_ORIGIN)
    except sqlite3.OperationalError:
        return 0
    return added


def apply_plan(
    conn: sqlite3.Connection,
    project_id: int,
    project_ids: Sequence[int],
    plan: TracePlan,
) -> dict[str, int]:
    """Write the plan. The caller holds the index lock and the transaction.

    Per source file: whatever that file (same path, or same content under
    another path) contributed before is deleted, then its current hits and
    edges are written. So re-ingesting an unchanged file is a no-op in effect,
    and re-ingesting a changed one replaces rather than adds.
    `first_ingested_at` survives the replacement — it is when the symbol was
    first seen running, which a refresh does not change.
    """
    now = time.time()
    replaced = 0
    # The code these traces saw = the index as of now (migration 35). Read once:
    # the caller holds the index lock, so no run lands in the middle.
    from livespec_mcp.domain.graph import _latest_run_id

    seen_run = _latest_run_id(conn, project_id)
    for src in plan.sources:
        prior = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM runtime_source WHERE project_id = ? AND origin = ? "
                "AND (path = ? OR content_hash = ?)",
                (project_id, plan.origin, src.path, src.content_hash),
            )
        ]
        first_seen: dict[int, float] = {}
        if prior:
            replaced += len(prior)
            for r in conn.execute(
                f"SELECT symbol_id, MIN(first_ingested_at) AS t FROM runtime_hit "
                f"WHERE source_id IN ({_in(prior)}) GROUP BY symbol_id",
                tuple(prior),
            ):
                first_seen[int(r["symbol_id"])] = float(r["t"])
            # FK cascade removes this source's hits and edges with it.
            conn.execute(f"DELETE FROM runtime_source WHERE id IN ({_in(prior)})", tuple(prior))
        source_id = conn.execute(
            """INSERT INTO runtime_source(
                   project_id, origin, path, content_hash, size, mtime, events,
                   resolved_events, symbols_hit, edges_observed, ingested_at,
                   index_run_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id,
                plan.origin,
                src.path,
                src.content_hash,
                src.size,
                src.mtime,
                src.enter_events,
                src.resolved_events,
                len(src.hits),
                len(src.edges),
                now,
                seen_run,
            ),
        ).lastrowid
        conn.executemany(
            "INSERT INTO runtime_hit(symbol_id, source_id, origin, calls, "
            "first_ingested_at, last_ingested_at) VALUES(?,?,?,?,?,?)",
            [
                (sid, source_id, plan.origin, n, first_seen.get(sid, now), now)
                for sid, n in src.hits.items()
            ],
        )
        conn.executemany(
            "INSERT INTO runtime_edge(src_symbol_id, dst_symbol_id, source_id, calls) "
            "VALUES(?,?,?,?)",
            [(a, b, source_id, n) for (a, b), n in src.edges.items()],
        )
    added, removed = _materialise(conn, project_ids, plan.origin)
    return {"edges_added": added, "edges_removed": removed, "sources_replaced": replaced}


def find_sources(
    conn: sqlite3.Connection,
    project_ids: Sequence[int],
    workspace: Path,
    trace_path: str | Sequence[str],
    origin: str = RUNTIME_ORIGIN,
) -> tuple[list[int], list[str]]:
    """Ingested sources named by `trace_path` -> `(source ids, paths matching none)`.

    Same spelling rules as ingesting: relative paths resolve against the
    workspace, a directory names the `*.jsonl` sources directly inside it. The
    file need not exist any more — a trace deleted from disk is exactly the
    one whose evidence someone wants out.
    """
    pids = tuple(project_ids)
    rows = conn.execute(
        f"SELECT id, path FROM runtime_source WHERE origin = ? AND project_id IN ({_in(pids)})",
        (origin, *pids),
    ).fetchall()
    stored = [(int(r["id"]), Path(r["path"])) for r in rows]
    raw = [trace_path] if isinstance(trace_path, str) else list(trace_path)
    ids: list[int] = []
    unmatched: list[str] = []
    for item in raw:
        p = Path(item).expanduser()
        if not p.is_absolute():
            p = workspace / p
        want = p.resolve()
        hit = [
            sid
            for sid, sp in stored
            if sp.resolve() == want or (sp.suffix == ".jsonl" and sp.resolve().parent == want)
        ]
        if not hit:
            unmatched.append(str(item))
        ids.extend(i for i in hit if i not in ids)
    return ids, unmatched


def remove_runtime(
    conn: sqlite3.Connection,
    project_ids: Sequence[int],
    origin: str = RUNTIME_ORIGIN,
    source_ids: Sequence[int] | None = None,
) -> dict[str, int]:
    """Delete this origin's edges, hits and provenance, all of it or some sources.

    Without `source_ids`: everything the origin owns. With them: only those
    sources; the hits and `runtime_edge` rows cascade with each, and an edge
    row in `symbol_edge` goes only when no remaining source supports the pair.

    Edges go by label, so a pair the static resolver has since reclaimed to
    `livespec` stays — it is no longer ours to remove.
    """
    pids = tuple(project_ids)
    if source_ids is not None:
        ids = tuple(source_ids)
        if not ids:
            return {"edges": 0, "symbols_with_hits": 0, "sources": 0}
        hits = conn.execute(
            f"SELECT COUNT(DISTINCT symbol_id) AS c FROM runtime_hit WHERE source_id IN ({_in(ids)})",
            ids,
        ).fetchone()["c"]
        sources = conn.execute(f"DELETE FROM runtime_source WHERE id IN ({_in(ids)})", ids).rowcount
        _added, edges = _materialise(conn, pids, origin)
        return {
            "edges": int(edges),
            "symbols_with_hits": int(hits or 0),
            "sources": int(sources or 0),
        }
    edges = conn.execute(
        f"""DELETE FROM symbol_edge
            WHERE origin = ?
              AND src_symbol_id IN (
                SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id
                WHERE f.project_id IN ({_in(pids)}))""",
        (origin, *pids),
    ).rowcount
    hits = conn.execute(
        f"""SELECT COUNT(DISTINCT h.symbol_id) AS c FROM runtime_hit h
            JOIN runtime_source rs ON rs.id = h.source_id
            WHERE rs.origin = ? AND rs.project_id IN ({_in(pids)})""",
        (origin, *pids),
    ).fetchone()["c"]
    sources = conn.execute(
        f"DELETE FROM runtime_source WHERE origin = ? AND project_id IN ({_in(pids)})",
        (origin, *pids),
    ).rowcount
    return {
        "edges": int(edges or 0),
        "symbols_with_hits": int(hits or 0),
        "sources": int(sources or 0),
    }


# ---------------------------------------------------------------------------
# Reading the evidence back
# ---------------------------------------------------------------------------


def _has_runtime_evidence(conn: sqlite3.Connection) -> bool:
    """One-row probe, so tools pay nothing on an index nobody ingested into."""
    try:
        return conn.execute("SELECT 1 FROM runtime_hit LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def runtime_hits(conn: sqlite3.Connection, project_id: int) -> dict[int, dict[str, Any]]:
    """symbol id -> `{calls, qualified_name, file_path}` for a project. Empty when none."""
    if not _has_runtime_evidence(conn):
        return {}
    return {
        int(r["symbol_id"]): {
            "calls": int(r["calls"]),
            "qualified_name": r["qualified_name"],
            "file_path": r["path"],
        }
        for r in conn.execute(
            """SELECT h.symbol_id, SUM(h.calls) AS calls, s.qualified_name, f.path
               FROM runtime_hit h
               JOIN symbol s ON s.id = h.symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?
               GROUP BY h.symbol_id""",
            (project_id,),
        )
    }


def symbol_runtime_calls(conn: sqlite3.Connection, project_id: int, symbol_id: int) -> int | None:
    """Times this symbol was seen entered, or None when the project has no evidence."""
    try:
        if (
            conn.execute(
                "SELECT 1 FROM runtime_source WHERE project_id = ? AND origin = ? LIMIT 1",
                (project_id, RUNTIME_ORIGIN),
            ).fetchone()
            is None
        ):
            return None
    except sqlite3.OperationalError:
        return None
    row = conn.execute(
        "SELECT COALESCE(SUM(calls), 0) AS c FROM runtime_hit WHERE symbol_id = ?",
        (symbol_id,),
    ).fetchone()
    return int(row["c"])


def stale_runtime_sources(conn: sqlite3.Connection, project_id: int) -> dict[int, str]:
    """`{source id: path}` of trace files ingested before the latest code change.

    Rule (deliberately the simple one): a source is stale when an index run
    that changed any file finished after the run that was current when the
    source was ingested (`runtime_source.index_run_id`, migration 35). The
    finer rule — only runs that changed a file holding one of the symbol's
    former runtime callers — cannot be answered afterwards: `runtime_edge`
    cascades away with a re-extracted caller, which is exactly the case. So
    this over-reports (an unrelated edit marks every earlier source) and never
    under-reports. A source with no recorded run is never called stale.
    """
    from livespec_mcp.domain.graph import _latest_run_id

    latest = _latest_run_id(conn, project_id)
    try:
        rows = conn.execute(
            """SELECT id, path FROM runtime_source
               WHERE project_id = ? AND origin = ?
                 AND index_run_id IS NOT NULL AND index_run_id < ?""",
            (project_id, RUNTIME_ORIGIN, latest),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(r["id"]): r["path"] for r in rows}


def hit_sources(conn: sqlite3.Connection, symbol_ids: Sequence[int]) -> dict[int, set[int]]:
    """symbol id -> ids of the sources with a hit on it."""
    out: dict[int, set[int]] = {}
    ids = list(symbol_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        for r in conn.execute(
            f"SELECT symbol_id, source_id FROM runtime_hit WHERE symbol_id IN ({_in(chunk)})",
            tuple(chunk),
        ):
            out.setdefault(int(r["symbol_id"]), set()).add(int(r["source_id"]))
    return out


def stale_evidence_for(
    conn: sqlite3.Connection, project_id: int, symbol_ids: Sequence[int]
) -> list[str] | None:
    """Paths of the sources behind a symbol's hits when ALL of them are stale, else None.

    `symbol_ids` is the symbol plus whatever defined inside it that was hit (a
    rescue by nested hit). Nothing to say (None) when any source with a hit is
    current, or when there are no hits at all.
    """
    stale = stale_runtime_sources(conn, project_id)
    if not stale:
        return None
    sources: set[int] = set()
    for srcs in hit_sources(conn, symbol_ids).values():
        sources |= srcs
    if not sources or not sources <= set(stale):
        return None
    return sorted(stale[s] for s in sources)


def runtime_state(conn: sqlite3.Connection, project_id: int) -> dict[str, Any] | None:
    """Has runtime evidence been ingested for this project, and how much survives?"""
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS n, MAX(ingested_at) AS last,
                      COALESCE(SUM(symbols_hit), 0) AS recorded,
                      COALESCE(SUM(edges_observed), 0) AS edges_recorded
               FROM runtime_source WHERE project_id = ? AND origin = ?""",
            (project_id, RUNTIME_ORIGIN),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row["n"]:
        return None
    live = conn.execute(
        """SELECT COUNT(DISTINCT h.symbol_id) AS symbols, COUNT(*) AS hit_rows
           FROM runtime_hit h JOIN runtime_source rs ON rs.id = h.source_id
           WHERE rs.project_id = ? AND rs.origin = ?""",
        (project_id, RUNTIME_ORIGIN),
    ).fetchone()
    live_edges = conn.execute(
        """SELECT COUNT(*) AS c FROM runtime_edge re
           JOIN runtime_source rs ON rs.id = re.source_id
           WHERE rs.project_id = ? AND rs.origin = ?""",
        (project_id, RUNTIME_ORIGIN),
    ).fetchone()["c"]
    return {
        "origin": RUNTIME_ORIGIN,
        "trace_files": int(row["n"]),
        "symbols_with_hits": int(live["symbols"]),
        "last_ingested_at": row["last"],
        "_hit_rows_recorded": int(row["recorded"]),
        "_hit_rows_now": int(live["hit_rows"]),
        "_edges_recorded": int(row["edges_recorded"]),
        "_edges_now": int(live_edges),
    }


def public_runtime_state(
    conn: sqlite3.Connection,
    project_id: int,
    is_test_path: Callable[[str], bool] | None = None,
) -> dict[str, Any] | None:
    """`runtime_state` without the bookkeeping, for a tool payload.

    With `is_test_path` (the project's test-path rule, injected so this module
    stays free of `tools/`) it adds the denominator for coverage:
    `symbols_non_test` — functions and methods outside test paths — and
    `symbols_with_hits_non_test` — how many of those were seen running.
    """
    state = runtime_state(conn, project_id)
    if state is None:
        return None
    out = {k: v for k, v in state.items() if not k.startswith("_")}
    if is_test_path is not None:
        non_test: set[int] = {
            int(r["id"])
            for r in conn.execute(
                """SELECT s.id, f.path FROM symbol s JOIN file f ON f.id = s.file_id
                   WHERE f.project_id = ? AND s.kind IN ('function', 'method')""",
                (project_id,),
            )
            if not is_test_path(r["path"])
        }
        total = len(non_test)
        hit = len(non_test & runtime_hits(conn, project_id).keys())
        out["symbols_non_test"] = total
        out["symbols_with_hits_non_test"] = hit
    return out


def runtime_freshness(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """`{origin: detail}` when a re-extract has dropped runtime evidence, else `{}`.

    Only one way to go stale is reported, on purpose. A deleted or rewritten
    trace file is normal — traces are scratch output — and says nothing about
    whether what was observed still holds. A re-extracted symbol does: its
    evidence was removed with it because its body changed, and until the
    program is traced again the graph knows less than it did at ingest time.
    """
    state = runtime_state(conn, project_id)
    if state is None:
        return {}
    lost_hits = state["_hit_rows_recorded"] - state["_hit_rows_now"]
    lost_edges = state["_edges_recorded"] - state["_edges_now"]
    if lost_hits <= 0 and lost_edges <= 0:
        return {}
    return {
        RUNTIME_ORIGIN: {
            "status": ["evidence_lost"],
            "ingested_at": state["last_ingested_at"],
            "hits_lost": max(lost_hits, 0),
            "edges_lost": max(lost_edges, 0),
            "hint": (
                "Files were re-extracted since the traces were ingested (they "
                "changed, or the index was rebuilt). The runtime evidence for "
                "their symbols went with them, by design: an observation "
                "describes the body that ran, not the one there now. Run the "
                "traced program again and re-run ingest_traces to get it "
                "back; evidence for files that were not re-extracted is intact."
            ),
        }
    }
