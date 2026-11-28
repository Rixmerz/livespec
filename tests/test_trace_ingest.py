"""Runtime evidence from FlowTrace traces: `ingest_traces` and what reads it.

A static call graph cannot see `getattr(module, "handle_" + name)(…)`. The
symbol it lands on has zero callers, `who_calls` says nobody uses it, and
`find_dead_code` proposes deleting code that runs on every request. A trace of
the program running knows better, and `ingest_traces` is how that knowledge
gets into the graph.

What these tests protect is the same thing the external-ingest tests protect,
because the ways to go wrong are the same: an ingest that guesses between two
symbols, that cannot be undone exactly, that counts a trace twice when it is
ingested twice, or that changes a payload on an index nobody traced, makes the
product's numbers partly someone else's without saying so.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain.external_ingest import EXTERNAL_ORIGIN
from livespec_mcp.domain.trace_ingest import RUNTIME_ORIGIN
from livespec_mcp.server import mcp
from tests.conftest import requires_grammar

TRACE_A = "a" * 32
TRACE_B = "b" * 32

# --------------------------------------------------------------------------
# Fixture: `dispatch` reaches the handlers only through getattr, so the static
# graph has no edge into them.
# --------------------------------------------------------------------------


def _repo(workspace: Path) -> None:
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "handlers.py").write_text(
        "def handle_pay(amount):\n"  # 1
        "    return amount\n"
        "\n"
        "\n"
        "def handle_refund(amount):\n"  # 5
        "    return -amount\n"
        "\n"
        "\n"
        "def never_called():\n"  # 9
        "    return 0\n"
    )
    (pkg / "registry.py").write_text(
        "from pkg import handlers\n"
        "\n"
        "\n"
        "def dispatch(name, amount):\n"  # 4
        "    fn = getattr(handlers, 'handle_' + name)\n"
        "    return fn(amount)\n"
    )
    (pkg / "main.py").write_text(
        "from pkg.registry import dispatch\n"
        "\n"
        "\n"
        "def run():\n"  # 4
        "    return dispatch('pay', 1)\n"
    )
    (pkg / "idle.py").write_text("def lonely():\n    return 1\n")
    # Two `render`s in two modules, and two `total`s in one: the name fallback
    # must refuse both unless something narrows them.
    (pkg / "a.py").write_text("def render():\n    return 'a'\n")
    (pkg / "b.py").write_text("def render():\n    return 'b'\n")
    (pkg / "billing.py").write_text(
        "class Invoice:\n"
        "    def total(self):\n"  # 2
        "        return 1\n"
        "\n"
        "\n"
        "class Receipt:\n"
        "    def total(self):\n"  # 7
        "        return 2\n"
    )


class _Trace:
    """Hand-written FlowTrace v2 JSONL, with matching `exit` events.

    Spans stay open until `close` (or `write`, which closes whatever is still
    open, innermost first), so a callee's `enter` lands while its caller is
    still running — the only order a real program produces, and the one the
    ingest's order checks rely on.
    """

    def __init__(self, trace_id: str = TRACE_A) -> None:
        self.trace_id = trace_id
        self.lines: list[str] = []
        self._n = 0
        self._open: list[dict] = []

    def enter(
        self,
        method: str,
        *,
        parent: str | None = None,
        cls: str = "",
        module: str = "",
        lang: str = "python",
        file: str | None = None,
        line: int | None = None,
        span: str | None = None,
        thread: str = "main",
    ) -> str:
        self._n += 1
        span = span or f"{self._n:016x}"
        ev: dict = {
            "ts": 1700000000.0 + self._n,
            "trace_id": self.trace_id,
            "span_id": span,
            "parent_id": parent,
            "event": "enter",
            "thread": thread,
            "lang": lang,
            "module": module,
            "class": cls,
            "method": method,
            "visibility": "public",
            "args": {},
            "depth": 0,
        }
        if file is not None:
            ev["file"] = file
        if line is not None:
            ev["line"] = line
        self.lines.append(json.dumps(ev, separators=(",", ":")))
        exit_ev = {k: v for k, v in ev.items() if k not in ("file", "line")}
        exit_ev.update(event="exit", result={}, duration_ns=10)
        self._open.append(exit_ev)
        return span

    def close(self, span: str) -> None:
        """Write the `exit` of one span now (it must still be open)."""
        for i, ev in enumerate(self._open):
            if ev["span_id"] == span:
                self.lines.append(json.dumps(self._open.pop(i), separators=(",", ":")))
                return
        raise AssertionError(f"span {span} is not open")

    def raw(self, text: str) -> None:
        self.lines.append(text)

    def write(self, path: Path) -> str:
        while self._open:
            self.lines.append(json.dumps(self._open.pop(), separators=(",", ":")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n")
        return str(path)


def _dispatch_trace(calls: int = 3) -> _Trace:
    """run -> dispatch -> handle_pay (x calls), every event carrying file + line."""
    t = _Trace()
    run = t.enter("run", module="pkg.main", file="pkg/main.py", line=4)
    for _ in range(calls):
        d = t.enter("dispatch", parent=run, module="pkg.registry", file="pkg/registry.py", line=4)
        t.enter("handle_pay", parent=d, module="pkg.handlers", file="pkg/handlers.py", line=1)
    return t


def _state(workspace: Path):
    from livespec_mcp.state import get_state

    return get_state(str(workspace))


def _edge_rows(workspace: Path) -> list[tuple]:
    st = _state(workspace)
    return [
        tuple(r)
        for r in st.conn.execute(
            """SELECT a.qualified_name, b.qualified_name, e.edge_type, e.weight, e.origin
               FROM symbol_edge e
               JOIN symbol a ON a.id = e.src_symbol_id
               JOIN symbol b ON b.id = e.dst_symbol_id
               ORDER BY 1, 2, 3"""
        )
    ]


def _hits(workspace: Path) -> dict[str, int]:
    st = _state(workspace)
    return {
        r["qualified_name"]: int(r["calls"])
        for r in st.conn.execute(
            """SELECT s.qualified_name, SUM(h.calls) AS calls
               FROM runtime_hit h JOIN symbol s ON s.id = h.symbol_id
               GROUP BY s.qualified_name"""
        )
    }


def _count(workspace: Path, table: str) -> int:
    return int(_state(workspace).conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


async def _call(client: Client, tool: str, **args) -> dict:
    return (await client.call_tool(tool, args)).data


async def _callers(client: Client, qname: str) -> list[dict]:
    return (await _call(client, "who_calls", qname=qname))["callers"]


@pytest.fixture
def repo(workspace: Path) -> Path:
    _repo(workspace)
    return workspace


# --------------------------------------------------------------------------
# The plan, and the default that writes nothing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        before = _edge_rows(repo)
        out = await _call(c, "ingest_traces", trace_path=trace)

    assert out["dry_run"] is True
    assert out["origin"] == RUNTIME_ORIGIN
    assert out["events_read"] == 14 and out["enter_events"] == 7
    assert out["malformed_lines"] == 0
    assert out["traces"] == 1
    assert out["langs"] == {"python": 7}
    assert out["events_with_location"] == 7
    assert out["resolved_by"] == {"location": 7}
    assert out["unresolved_events"] == 0 and out["unresolved"] == {}
    assert out["symbols_hit"] == 3
    # run -> dispatch is a static call the trace confirms; dispatch ->
    # handle_pay goes through getattr and is the one the graph lacks.
    assert out["edges_observed"] == 2
    assert out["already_known"] == 1
    assert out["edges_to_add"] == 1
    assert out["sample"] == [
        {
            "from": "pkg.registry.dispatch",
            "to": "pkg.handlers.handle_pay",
            "edge_type": "calls",
            "calls": 3,
        }
    ]
    assert "next" in out

    assert _edge_rows(repo) == before
    for table in ("runtime_source", "runtime_hit", "runtime_edge"):
        assert _count(repo, table) == 0


@pytest.mark.asyncio
async def test_a_dynamic_dispatch_call_reaches_who_calls_only_after_ingest(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        assert await _callers(c, "pkg.handlers.handle_pay") == []

        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        assert out["edges_added"] == 1 and out["edges_removed"] == 0

        callers = await _callers(c, "pkg.handlers.handle_pay")
        callees = (await _call(c, "who_does_this_call", qname="pkg.registry.dispatch"))["callees"]
        impact = await _call(
            c, "analyze_impact", target_type="symbol", target="pkg.handlers.handle_pay"
        )
        static_callers = await _callers(c, "pkg.registry.dispatch")
        payload = await _call(c, "who_calls", qname="pkg.handlers.handle_pay")

    assert [(r["qualified_name"], r["via_external_edge"]) for r in callers] == [
        ("pkg.registry.dispatch", RUNTIME_ORIGIN)
    ]
    assert [(r["qualified_name"], r.get("via_external_edge")) for r in callees] == [
        ("pkg.handlers.handle_pay", RUNTIME_ORIGIN)
    ]
    assert "pkg.registry.dispatch" in json.dumps(impact)
    # The edge livespec derived itself is still livespec's: confirmed, not re-tagged.
    assert [r["qualified_name"] for r in static_callers] == ["pkg.main.run"]
    assert "via_external_edge" not in static_callers[0]
    assert ("pkg.main.run", "pkg.registry.dispatch", "calls", 1.0, "livespec") in _edge_rows(repo)
    # ...and the read tools say part of the answer was observed, not derived.
    assert payload["external_edges"]["by_origin"] == {RUNTIME_ORIGIN: 1}
    assert "ingest_traces" in payload["external_edges"]["hint"]
    assert _hits(repo) == {
        "pkg.main.run": 1,
        "pkg.registry.dispatch": 3,
        "pkg.handlers.handle_pay": 3,
    }


# --------------------------------------------------------------------------
# Resolution: location first, a unique name otherwise, never a guess
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_without_line_resolves_by_name_inside_that_file(repo: Path):
    t = _Trace()
    t.enter("total", cls="Invoice", module="pkg.billing", file="pkg/billing.py")
    # No class to tell the two `total`s apart: refused, not guessed.
    t.enter("total", module="pkg.billing", file="pkg/billing.py")
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["resolved_by"] == {"file_name": 1}
    assert out["unresolved"] == {"ambiguous_name": 1}
    assert _hits(repo) == {"pkg.billing.Invoice.total": 1}


@pytest.mark.asyncio
async def test_name_fallback_accepts_a_unique_match_and_refuses_an_ambiguous_one(repo: Path):
    t = _Trace()
    t.enter("render", module="pkg.a")  # narrowed by module -> unique
    t.enter("render")  # two candidates, nothing to narrow with
    t.enter("total", cls="Receipt", module="pkg.billing")  # narrowed by class
    t.enter("lonely", module="pkg.elsewhere")  # the module names another place
    t.enter("no_such_function", module="pkg.a")
    t.enter("navigate", lang="browser", module="router")
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["events_with_location"] == 0
    assert out["resolved_by"] == {"name": 2}
    assert out["unresolved"] == {"ambiguous_name": 1, "no_match": 2, "unsupported_lang": 1}
    assert out["unresolved_samples"]["ambiguous_name"] == [
        {"lang": "python", "module": "", "class": "", "method": "render"}
    ]
    assert _hits(repo) == {"pkg.a.render": 1, "pkg.billing.Receipt.total": 1}


@pytest.mark.asyncio
async def test_a_line_that_now_belongs_to_another_function_is_not_trusted(repo: Path):
    """A trace recorded against older code points at lines that have moved.
    The method name has to agree with what the index has there."""
    t = _Trace()
    # Line 5 is handle_refund. The name is unique in the file, so the event is
    # still placed — by name, on the right symbol.
    t.enter("handle_pay", module="pkg.handlers", file="pkg/handlers.py", line=5)
    # A name the file does not have at all: nothing to fall back to.
    t.enter("handle_chargeback", module="pkg.handlers", file="pkg/handlers.py", line=5)
    # A line no function spans.
    t.enter("handle_chargeback", module="pkg.handlers", file="pkg/handlers.py", line=3)
    # A file livespec does not index: no name fallback into other files.
    t.enter("render", module="vendor.a", file="vendor/a.py", line=1)
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["resolved_by"] == {"file_name": 1}
    assert out["unresolved"] == {
        "file_not_indexed": 1,
        "location_name_mismatch": 1,
        "no_symbol_at_line": 1,
    }
    assert _hits(repo) == {"pkg.handlers.handle_pay": 1}


@pytest.mark.asyncio
async def test_absolute_and_ambiguous_file_paths(repo: Path):
    (repo / "other").mkdir()
    (repo / "other" / "a.py").write_text("def render():\n    return 'o'\n")
    t = _Trace()
    t.enter("run", file=str(repo / "pkg" / "main.py"), line=4)  # absolute, in the workspace
    t.enter("run", file="/ci/checkout/pkg/main.py", line=4)  # absolute, elsewhere
    t.enter("render", file="a.py", line=1)  # pkg/a.py and other/a.py both end with it
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["resolved_by"] == {"location": 2}
    assert out["unresolved"] == {"ambiguous_file": 1}
    assert _hits(repo) == {"pkg.main.run": 2}


@pytest.mark.asyncio
async def test_java_path_relative_to_the_source_root_matches_as_a_suffix(workspace: Path):
    requires_grammar("java")
    src = workspace / "service" / "src" / "main" / "java" / "com" / "acme"
    src.mkdir(parents=True)
    (src / "Foo.java").write_text(
        "package com.acme;\n"
        "public class Foo {\n"
        "    public int bar(int x) {\n"  # 3
        "        return baz(x);\n"  # 4: first body line, which is what Java reports
        "    }\n"
        "    public int baz(int x) {\n"  # 6
        "        return x;\n"
        "    }\n"
        "    public int baz(String s) {\n"  # 9
        "        return 1;\n"
        "    }\n"
        "}\n"
    )
    t = _Trace()
    bar = t.enter(
        "bar", cls="Foo", module="com.acme", lang="java", file="com/acme/Foo.java", line=4
    )
    t.enter(
        "baz", parent=bar, cls="Foo", module="com.acme", lang="java",
        file="com/acme/Foo.java", line=7,
    )  # fmt: skip
    # No location and two overloads: never guessed.
    t.enter("baz", cls="Foo", module="com.acme", lang="java")
    # No location, one `bar`, narrowed by package.
    t.enter("bar", cls="Foo", module="com.acme", lang="java")
    t.enter("bar", cls="Foo", module="com.other", lang="java")
    trace = t.write(workspace / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["resolved_by"] == {"location": 2, "name": 1}
    assert out["unresolved"] == {"ambiguous_name": 1, "no_match": 1}
    st = _state(workspace)
    hit_lines = {
        (r["name"], r["start_line"]): r["calls"]
        for r in st.conn.execute(
            """SELECT s.name, s.start_line, SUM(h.calls) AS calls
               FROM runtime_hit h JOIN symbol s ON s.id = h.symbol_id GROUP BY s.id"""
        )
    }
    assert hit_lines == {("bar", 3): 2, ("baz", 6): 1}


@pytest.mark.asyncio
async def test_typescript_private_method_and_basename_module(workspace: Path):
    requires_grammar("typescript")
    (workspace / "web").mkdir()
    (workspace / "web" / "cart.ts").write_text(
        "export class Cart {\n"
        "  #secret(): number { return 1; }\n"  # 2
        "  total(): number { return this.#secret(); }\n"  # 3
        "}\n"
        "export function addItem(x: number): number { return x; }\n"  # 5
    )
    t = _Trace()
    total = t.enter("total", cls="Cart", module="cart", lang="ts", file="web/cart.ts", line=3)
    t.enter(
        "#secret", parent=total, cls="Cart", module="cart", lang="ts", file="web/cart.ts", line=2
    )
    t.enter("#secret", cls="Cart", module="cart", lang="ts")  # name fallback, `#` and all
    t.enter("addItem", module="cart", lang="node")
    t.enter("addItem", module="basket", lang="node")  # another file's basename
    trace = t.write(workspace / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["langs"] == {"node": 2, "ts": 3}
    assert out["resolved_by"] == {"location": 2, "name": 2}
    assert out["unresolved"] == {"no_match": 1}
    assert _hits(workspace) == {
        "web.cart.Cart.total": 1,
        "web.cart.Cart.#secret": 2,
        "web.cart.addItem": 1,
    }


# --------------------------------------------------------------------------
# Reading real files: malformed lines, several processes, a directory
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_lines_are_counted_not_fatal(repo: Path):
    t = _dispatch_trace(calls=1)
    t.raw("{not json")
    t.raw('["an", "array"]')
    t.raw('{"event":"enter","span_id":"x"}')  # no trace_id
    t.raw('{"event":"error","trace_id":"t","span_id":"x"}')  # v1-style variant
    t.raw("")
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace)

    assert out["malformed_lines"] == 4
    assert out["enter_events"] == 3 and out["events_read"] == 6
    assert out["sources"][0]["malformed_lines"] == 4


@pytest.mark.asyncio
async def test_a_caller_in_another_process_file_resolves_in_either_order(repo: Path):
    """One trace, two JSONL files. The child process's file is read FIRST, so
    its parent span has not been seen yet when the child is."""
    parent = _Trace()
    d = parent.enter(
        "dispatch", module="pkg.registry", file="pkg/registry.py", line=4, span="p" * 16
    )
    child = _Trace()
    child.enter(
        "handle_refund", parent=d, module="pkg.handlers", file="pkg/handlers.py", line=5,
        span="c" * 16,
    )  # fmt: skip
    # A span whose caller is in neither file: counted, no edge.
    child.enter("lonely", parent="f" * 16, module="pkg.idle", file="pkg/idle.py", line=1)
    a = child.write(repo / "traces" / "a-child.jsonl")
    b = parent.write(repo / "traces" / "b-parent.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        split = await _call(c, "ingest_traces", trace_path=[a, b])
        as_dir = await _call(c, "ingest_traces", trace_path=str(repo / "traces"))
        alone = await _call(c, "ingest_traces", trace_path=a)

    assert split["trace_files"] == 2 and split["traces"] == 1
    assert split["edges_to_add"] == 1
    assert split["sample"][0]["from"] == "pkg.registry.dispatch"
    assert split["sample"][0]["to"] == "pkg.handlers.handle_refund"
    assert split["skipped"] == {"caller_not_in_traces": 1}
    # A directory means every *.jsonl in it.
    assert as_dir["trace_files"] == 2 and as_dir["edges_to_add"] == 1
    # Without the parent's file the edge cannot be known, and the payload says so.
    assert alone["edges_to_add"] == 0
    assert alone["skipped"] == {"caller_not_in_traces": 2}


# --------------------------------------------------------------------------
# Order-impossible spans are not evidence
#
# An older FlowTrace wrote spans nobody called: a user `__repr__` it ran itself
# while rendering a call's arguments came BEFORE the call's own `enter`, with
# that call's span as parent; a generator's span stayed "current" after it
# yielded, so a later call hung off a span that had already exited. Neither
# order can happen in a real program, so neither is allowed to become a
# caller -> callee edge or a hit that keeps a symbol out of find_dead_code.
# --------------------------------------------------------------------------


def _dispatch_with_artifact_repr() -> _Trace:
    """`handle_refund` plays the `__repr__`: entered before `dispatch`, parent = dispatch."""
    t = _Trace()
    run = t.enter("run", module="pkg.main", file="pkg/main.py", line=4)
    t.enter("handle_refund", parent="d" * 16, module="pkg.handlers", file="pkg/handlers.py", line=5)
    d = t.enter(
        "dispatch", parent=run, module="pkg.registry", file="pkg/registry.py", line=4, span="d" * 16
    )
    t.enter("handle_pay", parent=d, module="pkg.handlers", file="pkg/handlers.py", line=1)
    return t


@pytest.mark.asyncio
async def test_callee_entered_before_its_parent_is_no_edge_and_no_hit(repo: Path):
    trace = _dispatch_with_artifact_repr().write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        dead = await _call(c, "find_dead_code")
        refund_callers = await _callers(c, "pkg.handlers.handle_refund")

    assert out["skipped"] == {"parent_not_yet_entered": 1}
    assert out["discarded_spans"] == 1
    # The real chain survives: run -> dispatch -> handle_pay, no edge into the artifact.
    assert out["edges_observed"] == 2
    assert refund_callers == []
    assert _hits(repo) == {
        "pkg.main.run": 1,
        "pkg.registry.dispatch": 1,
        "pkg.handlers.handle_pay": 1,
    }
    # It ran, but only because the tracer ran it: it must stay a dead-code
    # candidate (same file as a symbol the trace did reach: `not_seen`, not rescued).
    tiers = {r["qualified_name"]: r["runtime"] for r in dead["dead_symbols"]}
    assert tiers["pkg.handlers.handle_refund"] == "not_seen"


@pytest.mark.asyncio
async def test_hit_counts_only_lose_the_discarded_occurrence(repo: Path):
    t = _Trace()
    run = t.enter("run", module="pkg.main", file="pkg/main.py", line=4)
    # One real call of handle_refund, one artifact of the same function.
    t.enter("handle_refund", parent=run, module="pkg.handlers", file="pkg/handlers.py", line=5)
    t.enter("handle_refund", parent="e" * 16, module="pkg.handlers", file="pkg/handlers.py", line=5)
    t.enter(
        "dispatch", parent=run, module="pkg.registry", file="pkg/registry.py", line=4, span="e" * 16
    )
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["skipped"] == {"parent_not_yet_entered": 1}
    assert _hits(repo)["pkg.handlers.handle_refund"] == 1
    assert out["edges_observed"] == 2  # run -> handle_refund, run -> dispatch


@pytest.mark.asyncio
async def test_callee_entering_after_its_parent_exited_is_no_edge_and_no_hit(repo: Path):
    t = _Trace()
    run = t.enter("run", module="pkg.main", file="pkg/main.py", line=4)
    gen = t.enter("dispatch", parent=run, module="pkg.registry", file="pkg/registry.py", line=4)
    t.close(gen)
    # `dispatch` has returned (a generator helper that yielded long ago), and a
    # later call is attributed to it on the same thread.
    t.enter("handle_refund", parent=gen, module="pkg.handlers", file="pkg/handlers.py", line=5)
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        dead = await _call(c, "find_dead_code")

    assert out["skipped"] == {"parent_already_exited": 1}
    assert out["discarded_spans"] == 1
    assert out["edges_observed"] == 1  # run -> dispatch only
    assert "pkg.handlers.handle_refund" not in _hits(repo)
    assert any(r["qualified_name"] == "pkg.handlers.handle_refund" for r in dead["dead_symbols"])


@pytest.mark.asyncio
async def test_a_parent_exited_on_another_thread_is_a_real_handoff(repo: Path):
    t = _Trace()
    run = t.enter("run", module="pkg.main", file="pkg/main.py", line=4, thread="main")
    t.close(run)
    # Work handed to a pool that outlives the function that submitted it.
    t.enter(
        "dispatch",
        parent=run,
        module="pkg.registry",
        file="pkg/registry.py",
        line=4,
        thread="worker-1",
    )
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["skipped"] == {} and out["discarded_spans"] == 0
    assert out["edges_observed"] == 1


@pytest.mark.asyncio
async def test_node_continuations_outliving_their_parent_are_kept(repo: Path):
    # A callback scheduled by a function that has already returned is how Node
    # code runs; the thread label is `main` for all of it.
    t = _Trace()
    run = t.enter("run", lang="node", module="main", file="pkg/main.py", line=4)
    t.close(run)
    t.enter("dispatch", parent=run, lang="node", module="registry", file="pkg/registry.py", line=4)
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace)

    assert out["skipped"] == {} and out["edges_observed"] == 1


@pytest.mark.asyncio
async def test_a_missing_exit_never_makes_a_parent_exited(repo: Path):
    # The trace was cut: `run` has an enter and no exit. Its children are fine.
    t = _Trace()
    run = t.enter("run", module="pkg.main", file="pkg/main.py", line=4)
    t.enter("dispatch", parent=run, module="pkg.registry", file="pkg/registry.py", line=4)
    t._open.clear()  # cut: neither span ever got its exit line
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace)

    assert out["skipped"] == {} and out["edges_observed"] == 1


@pytest.mark.asyncio
async def test_child_first_across_files_is_a_multi_process_trace_not_an_artifact(repo: Path):
    # Same shape as the artifact — the child is read before its parent — but
    # the parent is in ANOTHER file (another process, read second). It settles.
    child_file = _Trace()
    child_file.enter(
        "dispatch", parent="c" * 16, module="pkg.registry", file="pkg/registry.py", line=4
    )
    parent_file = _Trace()
    parent_file.enter("run", module="pkg.main", file="pkg/main.py", line=4, span="c" * 16)
    a = child_file.write(repo / "traces" / "1-child.jsonl")
    b = parent_file.write(repo / "traces" / "2-parent.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=[a, b])

    assert out["skipped"] == {} and out["discarded_spans"] == 0
    assert out["edges_observed"] == 1 and out["symbols_hit"] == 2


@pytest.mark.asyncio
async def test_a_parent_that_never_appears_is_still_caller_not_in_traces(repo: Path):
    t = _Trace()
    t.enter("dispatch", parent="f" * 16, module="pkg.registry", file="pkg/registry.py", line=4)
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    assert out["skipped"] == {"caller_not_in_traces": 1} and out["discarded_spans"] == 0
    assert _hits(repo) == {"pkg.registry.dispatch": 1}


@pytest.mark.asyncio
async def test_recursion_and_unresolved_callers_produce_no_edge(repo: Path):
    t = _Trace()
    outer = t.enter("handle_pay", module="pkg.handlers", file="pkg/handlers.py", line=1)
    t.enter("handle_pay", parent=outer, module="pkg.handlers", file="pkg/handlers.py", line=1)
    ghost = t.enter("framework_internal", module="werkzeug.serving")
    t.enter("lonely", parent=ghost, module="pkg.idle", file="pkg/idle.py", line=1)
    trace = t.write(repo / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        out = await _call(c, "ingest_traces", trace_path=trace)

    assert out["edges_observed"] == 0
    assert out["skipped"] == {"caller_unresolved": 1, "self_call": 1}


# --------------------------------------------------------------------------
# Idempotent, reversible, and separate from the other ingest
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reingesting_the_same_trace_changes_nothing(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        first = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        edges, hits = _edge_rows(repo), _hits(repo)
        first_seen = (
            _state(repo)
            .conn.execute("SELECT MIN(first_ingested_at) AS t FROM runtime_hit")
            .fetchone()["t"]
        )

        again = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        # The same content under another name is the same evidence.
        copy = shutil.copy(trace, repo / "traces" / "copy-of-run.jsonl")
        copied = await _call(c, "ingest_traces", trace_path=str(copy), dry_run=False)
        dry = await _call(c, "ingest_traces", trace_path=trace)

    assert first["edges_added"] == 1
    assert again["edges_added"] == 0 and again["edges_removed"] == 0
    assert again["sources_replaced"] == 1 and copied["sources_replaced"] == 1
    # The plan is a function of the trace, not of whether it was applied before.
    assert (dry["edges_to_add"], dry["already_known"]) == (1, 1)
    assert (again["edges_to_add"], again["already_known"]) == (1, 1)
    assert _edge_rows(repo) == edges
    assert _hits(repo) == hits
    assert _count(repo, "runtime_source") == 1
    st = _state(repo)
    row = st.conn.execute(
        "SELECT MIN(first_ingested_at) AS f, MAX(last_ingested_at) AS l FROM runtime_hit"
    ).fetchone()
    assert row["f"] == first_seen and row["l"] >= first_seen


@pytest.mark.asyncio
async def test_different_traces_accumulate_and_a_rewritten_one_replaces_itself(repo: Path):
    pay = _dispatch_trace(calls=2).write(repo / "traces" / "pay.jsonl")
    t = _Trace(TRACE_B)
    d = t.enter("dispatch", module="pkg.registry", file="pkg/registry.py", line=4)
    t.enter("handle_refund", parent=d, module="pkg.handlers", file="pkg/handlers.py", line=5)
    refund = t.write(repo / "traces" / "refund.jsonl")

    async with Client(mcp) as c:
        await _call(c, "index_project")
        await _call(c, "ingest_traces", trace_path=pay, dry_run=False)
        await _call(c, "ingest_traces", trace_path=refund, dry_run=False)
        both = _hits(repo)
        assert both["pkg.registry.dispatch"] == 3  # 2 + 1, from two sources
        assert {r["qualified_name"] for r in await _callers(c, "pkg.handlers.handle_refund")} == {
            "pkg.registry.dispatch"
        }

        # The refund run is recorded again and this time never refunds: the
        # edge only that file supported is retracted, the other file's stays.
        t2 = _Trace(TRACE_B)
        t2.enter("dispatch", module="pkg.registry", file="pkg/registry.py", line=4)
        t2.write(repo / "traces" / "refund.jsonl")
        out = await _call(c, "ingest_traces", trace_path=refund, dry_run=False)
        refund_callers = await _callers(c, "pkg.handlers.handle_refund")
        pay_callers = await _callers(c, "pkg.handlers.handle_pay")

    assert out["edges_removed"] == 1 and out["sources_replaced"] == 1
    assert refund_callers == []
    assert [r["qualified_name"] for r in pay_callers] == ["pkg.registry.dispatch"]
    assert _hits(repo) == {
        "pkg.main.run": 1,
        "pkg.registry.dispatch": 3,
        "pkg.handlers.handle_pay": 2,
    }


@pytest.mark.asyncio
async def test_remove_restores_the_prior_graph_and_leaves_the_other_origin_alone(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        # An edge a Graphify ingest wrote, between two symbols traces never link.
        st = _state(repo)
        ids = {
            r["qualified_name"]: r["id"]
            for r in st.conn.execute("SELECT id, qualified_name FROM symbol")
        }
        st.conn.execute(
            "INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight, origin) "
            "VALUES(?,?,?,?,?)",
            (ids["pkg.a.render"], ids["pkg.b.render"], "references", 0.9, EXTERNAL_ORIGIN),
        )
        st.conn.commit()
        before = _edge_rows(repo)

        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        assert len(_edge_rows(repo)) == len(before) + 1

        # The other ingest's remove does not reach runtime rows...
        await _call(c, "ingest_external_graph", remove=True)
        assert [e for e in _edge_rows(repo) if e[4] == RUNTIME_ORIGIN] != []
        assert [e for e in _edge_rows(repo) if e[4] == EXTERNAL_ORIGIN] == []
        st.conn.execute(
            "INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight, origin) "
            "VALUES(?,?,?,?,?)",
            (ids["pkg.a.render"], ids["pkg.b.render"], "references", 0.9, EXTERNAL_ORIGIN),
        )
        st.conn.commit()

        # ...and this one does not reach anyone else's.
        out = await _call(c, "ingest_traces", remove=True)
        callers = await _callers(c, "pkg.handlers.handle_pay")
        dead = await _call(c, "find_dead_code")

    assert out["removed"] == 1
    assert out["hits_removed"] == 3 and out["trace_files_removed"] == 1
    assert out["origin"] == RUNTIME_ORIGIN
    assert _edge_rows(repo) == before
    for table in ("runtime_source", "runtime_hit", "runtime_edge"):
        assert _count(repo, table) == 0
    assert callers == []
    assert "runtime" not in dead


@pytest.mark.asyncio
async def test_removing_an_external_ingest_does_not_take_an_observed_call_with_it(repo: Path):
    """One row holds one origin. If a graph claimed the pair first, the trace's
    claim to it lives only in `runtime_edge` — and must survive the graph's
    removal."""
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        st = _state(repo)
        ids = {
            r["qualified_name"]: r["id"]
            for r in st.conn.execute("SELECT id, qualified_name FROM symbol")
        }
        st.conn.execute(
            "INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight, origin) "
            "VALUES(?,?,?,?,?)",
            (
                ids["pkg.registry.dispatch"],
                ids["pkg.handlers.handle_pay"],
                "calls",
                0.9,
                EXTERNAL_ORIGIN,
            ),
        )
        st.conn.commit()

        out = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        assert (out["edges_to_add"], out["already_known"], out["edges_added"]) == (0, 2, 0)

        await _call(c, "ingest_external_graph", remove=True)
        callers = await _callers(c, "pkg.handlers.handle_pay")

    assert [(r["qualified_name"], r["via_external_edge"]) for r in callers] == [
        ("pkg.registry.dispatch", RUNTIME_ORIGIN)
    ]


# --------------------------------------------------------------------------
# find_dead_code: the tiers, and the guarantee when there are no traces
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_dead_code_is_unchanged_when_no_trace_was_ingested(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        baseline = await _call(c, "find_dead_code")
        baseline_summary = await _call(c, "find_dead_code", summary_only=True)
        orient = await _call(c, "quick_orient", qname="pkg.handlers.handle_pay")

        await _call(c, "ingest_traces", trace_path=trace)  # dry run
        after_dry = await _call(c, "find_dead_code")

        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        await _call(c, "ingest_traces", remove=True)
        after_remove = await _call(c, "find_dead_code")
        after_remove_summary = await _call(c, "find_dead_code", summary_only=True)
        orient_after = await _call(c, "quick_orient", qname="pkg.handlers.handle_pay")

    assert "runtime" not in baseline
    assert all("runtime" not in row for row in baseline["dead_symbols"])
    assert "handle_pay" in json.dumps(baseline)
    as_bytes = json.dumps(baseline, sort_keys=False)
    assert json.dumps(after_dry, sort_keys=False) == as_bytes
    assert json.dumps(after_remove, sort_keys=False) == as_bytes
    assert json.dumps(after_remove_summary) == json.dumps(baseline_summary)
    assert "runtime_calls" not in orient and orient_after == orient


@pytest.mark.asyncio
async def test_find_dead_code_runtime_tiers(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        before = await _call(c, "find_dead_code")
        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        after = await _call(c, "find_dead_code")
        summary = await _call(c, "find_dead_code", summary_only=True)
        page1 = await _call(c, "find_dead_code", limit=2)
        page2 = await _call(c, "find_dead_code", limit=2, cursor=page1["next_cursor"])
        orient = await _call(c, "quick_orient", qname="pkg.handlers.handle_pay")
        orient_unseen = await _call(c, "quick_orient", qname="pkg.handlers.never_called")
        overview = await _call(c, "get_project_overview")

    names_before = [r["qualified_name"] for r in before["dead_symbols"]]
    tiers = {r["qualified_name"]: r["runtime"] for r in after["dead_symbols"]}
    # `run` has no caller and no edge into it, but it ran: not dead.
    assert "pkg.main.run" in names_before
    assert "pkg.main.run" not in tiers
    # handle_pay left earlier, through the runtime edge that now reaches it.
    assert "pkg.handlers.handle_pay" in names_before
    assert "pkg.handlers.handle_pay" not in tiers
    # Same file as handle_pay, traced run reached it, these never executed.
    assert tiers["pkg.handlers.handle_refund"] == "not_seen"
    assert tiers["pkg.handlers.never_called"] == "not_seen"
    # No trace touched these files.
    assert tiers["pkg.idle.lonely"] == "no_evidence"
    assert tiers["pkg.a.render"] == "no_evidence"

    runtime = after["runtime"]
    assert runtime["origin"] == RUNTIME_ORIGIN
    assert runtime["rescued_by_runtime"] == 1
    assert runtime["not_seen"] == sum(1 for t in tiers.values() if t == "not_seen")
    assert runtime["no_evidence"] == sum(1 for t in tiers.values() if t == "no_evidence")
    assert after["count"] == len(tiers) == runtime["not_seen"] + runtime["no_evidence"]
    # Order is the order it always had: the survivors, in the same sequence.
    assert list(tiers) == [n for n in names_before if n in tiers]

    # Counts are exact whatever the page, and summary mode carries them too.
    assert "dead_symbols" not in summary and summary["runtime"] == runtime
    assert page1["runtime"] == runtime and page1["count"] == after["count"]
    assert [r["qualified_name"] for r in page1["dead_symbols"] + page2["dead_symbols"]] == list(
        tiers
    )[:4]

    assert orient["runtime_calls"] == 3 and orient_unseen["runtime_calls"] == 0
    assert overview["runtime_evidence"]["symbols_with_hits"] == 3
    assert overview["runtime_evidence"]["trace_files"] == 1


@pytest.mark.asyncio
async def test_a_class_whose_method_ran_is_not_dead(workspace: Path):
    (workspace / "plugins.py").write_text(
        "class Exporter:\n"
        "    def export(self):\n"  # 2
        "        return 1\n"
        "\n"
        "\n"
        "class Importer:\n"
        "    def load(self):\n"  # 7
        "        return 2\n"
    )
    t = _Trace()
    t.enter("export", cls="Exporter", module="plugins", file="plugins.py", line=2)
    trace = t.write(workspace / "traces" / "t.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        before = {r["qualified_name"] for r in (await _call(c, "find_dead_code"))["dead_symbols"]}
        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        after = await _call(c, "find_dead_code")

    tiers = {r["qualified_name"]: r["runtime"] for r in after["dead_symbols"]}
    assert {"plugins.Exporter", "plugins.Exporter.export"} <= before
    assert "plugins.Exporter" not in tiers and "plugins.Exporter.export" not in tiers
    assert after["runtime"]["rescued_by_runtime"] == 2
    assert tiers["plugins.Importer"] == "not_seen"


# --------------------------------------------------------------------------
# Evidence follows the code
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reindexing_an_edited_file_drops_its_evidence_and_reclaims_the_edge(repo: Path):
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

        # The dynamic call becomes a plain one: static extraction now derives
        # the very edge the trace supplied.
        (repo / "pkg" / "registry.py").write_text(
            "from pkg.handlers import handle_pay\n"
            "\n"
            "\n"
            "def dispatch(name, amount):\n"
            "    return handle_pay(amount)\n"
        )
        reindex = await _call(c, "index_project")
        callers = await _callers(c, "pkg.handlers.handle_pay")
        payload = await _call(c, "who_calls", qname="pkg.handlers.handle_pay")
        dead = await _call(c, "find_dead_code")
        status = json.loads((await c.read_resource("project://index/status"))[0].text)

    assert reindex["external_edges_stale"]["by_origin"] == {RUNTIME_ORIGIN: 1}
    assert reindex["external_edges_stale"]["dropped_by_reextract"] == 1
    assert "ingest_traces" in reindex["external_edges_stale"]["hint"]

    assert [r["qualified_name"] for r in callers] == ["pkg.registry.dispatch"]
    assert "via_external_edge" not in callers[0]
    assert [e[4] for e in _edge_rows(repo) if e[1] == "pkg.handlers.handle_pay"] == ["livespec"]
    # dispatch was re-extracted, so what was observed about its old body is gone;
    # the files that did not change keep theirs.
    assert _hits(repo) == {"pkg.main.run": 1, "pkg.handlers.handle_pay": 3}
    # No runtime edge is left to count, and the note is still there — saying why.
    stale = payload["external_edges"]["stale"][RUNTIME_ORIGIN]
    assert stale["status"] == ["evidence_lost"]
    assert stale["hits_lost"] == 1 and stale["edges_lost"] == 2
    assert "re-extracted" in stale["hint"]
    assert dead["runtime"]["rescued_by_runtime"] == 1
    assert status["runtime_evidence"]["symbols_with_hits"] == 2


@pytest.mark.asyncio
async def test_the_resolver_reclaims_a_runtime_row_it_derives_itself(repo: Path):
    """The rule the external ingest lives under, for the second origin too: the
    moment livespec's own resolver derives an edge, the row is livespec's, and
    `remove=True` can no longer delete it.

    Driven at the resolver's own statement for the reason the external test
    gives — a runtime row only survives a re-index when neither endpoint's file
    changed, so staging the collision end to end means engineering a partial
    re-index whose only visible effect is which of two identical rows won."""
    trace = _dispatch_trace().write(repo / "traces" / "run.jsonl")
    async with Client(mcp) as c:
        await _call(c, "index_project")
        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)

    st = _state(repo)
    src, dst = st.conn.execute(
        "SELECT src_symbol_id, dst_symbol_id FROM symbol_edge WHERE origin = ?",
        (RUNTIME_ORIGIN,),
    ).fetchone()
    # Verbatim the upsert `_resolve_refs` runs for every edge it derives.
    st.conn.execute(
        """INSERT INTO symbol_edge(src_symbol_id, dst_symbol_id, edge_type, weight)
           VALUES(?,?,?,?)
           ON CONFLICT(src_symbol_id, dst_symbol_id, edge_type)
           DO UPDATE SET weight = MAX(symbol_edge.weight, excluded.weight),
                         origin = 'livespec'""",
        (src, dst, "calls", 0.9),
    )
    st.conn.commit()
    reclaimed = ("pkg.registry.dispatch", "pkg.handlers.handle_pay", "calls", 1.0, "livespec")
    assert reclaimed in _edge_rows(repo)

    async with Client(mcp) as c:
        again = await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        removed = await _call(c, "ingest_traces", remove=True)

    # Re-ingesting does not take it back, and removing does not delete it.
    assert (again["edges_to_add"], again["already_known"], again["edges_added"]) == (0, 2, 0)
    assert removed["removed"] == 0
    assert reclaimed in _edge_rows(repo)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def _assert_canonical_error(payload: dict) -> None:
    assert payload.get("isError") is True, payload
    assert isinstance(payload.get("error"), str) and "\n" not in payload["error"]
    assert set(payload) <= {"error", "isError", "did_you_mean", "hint"}, payload
    assert isinstance(payload.get("hint"), str)


@pytest.mark.asyncio
async def test_error_shapes(repo: Path):
    only_exits = _Trace()
    only_exits.raw('{"ts":1,"trace_id":"t","span_id":"s","event":"exit","result":{}}')
    only_exits.raw("garbage")
    no_enter = only_exits.write(repo / "traces" / "exits.jsonl")
    foreign = _Trace()
    foreign.enter("somebody_elses_function", module="other.app", file="other/app.py", line=3)
    foreign.enter("render")
    unresolved = foreign.write(repo / "traces" / "foreign.jsonl")
    (repo / "empty_dir").mkdir()

    async with Client(mcp) as c:
        await _call(c, "index_project")
        missing = await _call(c, "ingest_traces", trace_path="traces/nope.jsonl")
        nothing = await _call(c, "ingest_traces")
        empty_dir = await _call(c, "ingest_traces", trace_path="empty_dir")
        exits = await _call(c, "ingest_traces", trace_path=no_enter)
        zero = await _call(c, "ingest_traces", trace_path=unresolved, dry_run=False)

    for payload in (missing, nothing, empty_dir, exits, zero):
        _assert_canonical_error(payload)
    assert "not found" in missing["error"]
    assert "enter" in exits["error"] and "1 malformed" in exits["hint"]
    assert "None of the 2 enter events" in zero["error"]
    for needle in ("ambiguous_name=1", "file_not_indexed=1", "`file` / `line`", "unique"):
        assert needle in zero["hint"], needle
    # A failed ingest wrote nothing.
    assert _count(repo, "runtime_source") == 0


# --------------------------------------------------------------------------
# A function registered with `@obj.something` is called by the framework that
# owns `obj`. A trace that never drove the framework says nothing about it, so
# it must not be offered as the strongest removal candidate.
# --------------------------------------------------------------------------


def _plugin_module(repo: Path) -> None:
    (repo / "pkg" / "plugin.py").write_text(
        "manager = object()\n"
        "\n"
        "\n"
        "@manager.user_loader\n"  # 4
        "def load_user(uid):\n"  # 5
        "    return uid\n"
        "\n"
        "\n"
        "@manager.hook('x')\n"  # 9
        "def on_x():\n"  # 10
        "    return 1\n"
        "\n"
        "\n"
        "def plain_unused():\n"  # 14
        "    return 2\n"
        "\n"
        "\n"
        "def touched():\n"  # 18
        "    return 3\n"
    )


def _touch_plugin_trace(repo: Path) -> str:
    t = _Trace()
    t.enter("touched", module="pkg.plugin", file="pkg/plugin.py", line=18)
    return t.write(repo / "traces" / "plugin.jsonl")


@pytest.mark.asyncio
async def test_decorator_registered_function_is_not_a_removal_candidate(repo: Path):
    _plugin_module(repo)
    trace = _touch_plugin_trace(repo)
    async with Client(mcp) as c:
        await _call(c, "index_project")
        await _call(c, "ingest_traces", trace_path=trace, dry_run=False)
        default = await _call(c, "find_dead_code", include_public=True)
        wide = await _call(c, "find_dead_code", include_public=True, include_infrastructure=True)

    names = {r["qualified_name"]: r["runtime"] for r in default["dead_symbols"]}
    # Same file as a traced symbol, zero callers, zero hits: not_seen for a plain
    # function, absent for the two that a framework calls back.
    assert names["pkg.plugin.plain_unused"] == "not_seen"
    assert "pkg.plugin.load_user" not in names
    assert "pkg.plugin.on_x" not in names
    assert default["filtered_out"]["infrastructure"] >= 2

    # With include_infrastructure they are listed, but under their own tier.
    wide_tiers = {r["qualified_name"]: r["runtime"] for r in wide["dead_symbols"]}
    assert wide_tiers["pkg.plugin.load_user"] == "registered_by_decorator"
    assert wide_tiers["pkg.plugin.on_x"] == "registered_by_decorator"
    assert wide_tiers["pkg.plugin.plain_unused"] == "not_seen"
    assert wide["runtime"]["registered_by_decorator"] == 2


@pytest.mark.asyncio
async def test_stdlib_module_decorators_do_not_protect(repo: Path):
    (repo / "pkg" / "wrapped.py").write_text(
        "import functools\n"
        "\n"
        "\n"
        "@functools.lru_cache(maxsize=None)\n"
        "def cached_unused():\n"
        "    return 1\n"
    )
    async with Client(mcp) as c:
        await _call(c, "index_project")
        dead = await _call(c, "find_dead_code", include_public=True)
    assert "pkg.wrapped.cached_unused" in {r["qualified_name"] for r in dead["dead_symbols"]}
