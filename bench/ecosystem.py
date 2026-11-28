#!/usr/bin/env python3
"""Ecosystem benchmark: livespec + FlowTrace (+ delta-cube) on real repositories.

What it measures, per pinned repository (see README, "Ecosystem benchmark"):

1. static     index time, symbols, call edges, `find_dead_code` candidates
2. characterise  the repo's own test suite plain vs. under FlowTrace
3. ingest     `ingest_traces` dry-run then applied: resolution, new runtime edges
4. after      `find_dead_code` again, `who_calls` before/after, wrong-edge audit
5. payload    what an agent would have to READ to answer 6-8 comprehension
              tasks: whole files (search + read) vs. livespec tool responses.
              This is a payload proxy, not an agent session (see README).
6. delta-cube optional `hotspots` top 10

Usage:
    uv run python bench/ecosystem.py                       # both repos
    uv run python bench/ecosystem.py --repo flask
    uv run python bench/ecosystem.py --quick               # click only, trimmed
    uv run python bench/ecosystem.py --json bench/results-ecosystem.json

Everything livespec-side runs in a fresh subprocess (`--worker`) so peak RSS is
per step and no in-process cache can go stale across an ingest.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import re
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

DEFAULT_FLOWTRACE = Path(os.environ.get("FLOWTRACE_DIR", "/home/user/flowtrace-debugger"))
DEFAULT_DELTA_CUBE = Path(os.environ.get("DELTA_CUBE_DIR", "/home/user/delta-cube"))
RUNTIME_ORIGIN = "runtime:flowtrace"

# Proxy / TLS variables are removed from every test run: httpx, requests and
# friends read them and then fail tests that assume a direct connection. The
# suites must run offline and identically on any machine.
SCRUBBED_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
    "all_proxy", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)  # fmt: skip

# ---------------------------------------------------------------------------
# Repositories (pinned by tag AND commit; the clone is verified against the sha)
# ---------------------------------------------------------------------------

REPOS: dict[str, dict[str, Any]] = {
    "click": {
        "url": "https://github.com/pallets/click.git",
        "ref": "8.1.8",
        "sha": "934813e4d421071a1b3db3973c02fe2721359a6e",
        "prefix": "click",
        "requirements": "requirements/tests.txt",  # pinned by the project itself
    },
    "flask": {
        "url": "https://github.com/pallets/flask.git",
        "ref": "3.1.0",
        "sha": "ab8149664182b662453a563161aa89013c806dc9",
        "prefix": "flask",
        "requirements": "requirements/tests.txt",
        # flask's own requirements/tests.txt leaves its runtime dependencies
        # unpinned; these are the versions the committed baseline ran with
        "constraints": {
            "werkzeug": "3.1.9",
            "click": "8.5.1",
            "jinja2": "3.1.6",
            "markupsafe": "3.0.4",
            "itsdangerous": "2.2.0",
            "blinker": "1.9.0",
        },
    },
}

# Why these two (from probing click, flask, rich, requests, httpx on the same
# machine, offline suites, same flags; numbers are from the run that picked
# them and are repeated in bench/README.md):
#   click    628 tests in ~2s; 113K spans, 88MB at max-arg-length 64; callbacks,
#            ParamType.convert polymorphism, decorators registering commands.
#   flask    490 tests in ~2s; 63K spans, 49MB; view/ctx/session dispatch through
#            properties, descriptors, setupmethod wrappers, signals.
#   rich     one suite run produced >5GB of trace in under 2 minutes and had not
#            finished (stopped): hot inner loops traced at every call.
#   httpx    1417 tests; 5 fail before any tracing here; 163MB trace.
#   requests 72s plain, 8 env-sensitive failures; slowest of the group.
# rich/httpx/requests stay selectable in principle but are not pinned here.


# ---------------------------------------------------------------------------
# Comprehension tasks for the payload proxy (defined ONCE, from real symbols).
#
# by-file rule: a careful search-and-read approach greps the repo's .py files
# (src/ + tests/) for each `terms` identifier (word match) and opens every
# matching file whole, plus every file in `extra` (files a reader must open
# although grep for the term does not find them, e.g. the dispatcher of a
# runtime-only edge). Characters = sum of sizes of those files.
# by-tool rule: the livespec calls in `calls` are the minimal sequence; every
# call counts, characters = length of the text the tool returned.
# `check` receives the list of parsed tool results and returns True when the
# tool's answer is correct against the source; `truth` says how it was checked.
# ---------------------------------------------------------------------------


def _callers(res: dict[str, Any]) -> set[str]:
    return {c["qualified_name"] for c in res.get("callers", [])}


def _runtime_callers(res: dict[str, Any]) -> set[str]:
    return {c["qualified_name"] for c in res.get("callers", []) if c.get("via_external_edge")}


def _impact_names(res: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("callers", "impacted_symbols", "impacted", "affected_symbols"):
        for c in res.get(key, []) or []:
            if isinstance(c, dict) and "qualified_name" in c:
                names.add(c["qualified_name"])
    return names


def _has(*names: str) -> Callable[[list[dict[str, Any]]], bool]:
    """Answer is right when the first call lists every `names` as caller/callee."""

    def check(rs: list[dict[str, Any]]) -> bool:
        r = rs[0]
        got = {c["qualified_name"] for c in (r.get("callers") or r.get("callees") or [])}
        return set(names) <= got

    return check


def _task(id_: str, kind: str, question: str, calls: list[tuple[str, dict[str, Any]]], terms: list[str],
          check: Callable[[list[dict[str, Any]]], bool], truth: str, runtime_only: bool,
          extra: list[tuple[str, str]] | None = None, scope: tuple[str, ...] = ("src", "tests"),
          wrong: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    return {"id": id_, "kind": kind, "question": question, "calls": calls, "terms": terms,
            "check": check, "truth": truth, "runtime_only": runtime_only,
            "extra": extra or [], "scope": scope, "wrong": wrong or []}  # fmt: skip


C = "src.click."
F = "src.flask."
TASKS: dict[str, list[dict[str, Any]]] = {
    "click": [
        _task(
            "click-1",
            "who_calls",
            "Who calls StringParamType.convert?",
            [("who_calls", {"qname": C + "types.StringParamType.convert"})],
            ["convert"],
            _has(C + "types.ParamType.__call__"),
            "types.py ParamType.__call__ does `self.convert(value, param, ctx)`: polymorphic dispatch to every subclass",
            True,
        ),
        _task(
            "click-2",
            "who_calls",
            "Who calls Group.get_command?",
            [("who_calls", {"qname": C + "core.Group.get_command"})],
            ["get_command"],
            _has(
                C + "core.MultiCommand.resolve_command",
                C + "core.MultiCommand.format_commands",
                C + "core.MultiCommand.to_info_dict",
            ),
            "core.py: MultiCommand.resolve_command/format_commands/to_info_dict call `self.get_command(...)`, which only Group overrides",
            True,
        ),
        _task(
            "click-3",
            "impact",
            "What breaks if I change Parameter.process_value?",
            [("who_calls", {"qname": C + "core.Parameter.process_value", "max_depth": 2})],
            ["process_value"],
            _has(C + "core.Parameter.handle_parse_result"),
            "core.py handle_parse_result calls `self.process_value(ctx, value)` (and Option.prompt_for_value via lambda)",
            False,
        ),
        _task(
            "click-4",
            "dead_code",
            "Is _compat._get_windows_console_stream unused?",
            [("who_calls", {"qname": C + "_compat._get_windows_console_stream"})],
            ["_get_windows_console_stream"],
            _has(
                C + "_compat.get_text_stdout",
                C + "_compat.get_text_stderr",
                C + "_compat.get_text_stdin",
            ),
            "NOT unused: _compat.py get_text_stdin/stdout/stderr call it; defined under `if WIN:`/else so the static resolver misses the call",
            True,
        ),
        _task(
            "click-5",
            "what_does",
            "What does Parameter.type_cast_value do?",
            [("get_symbol_source", {"qname": C + "core.Parameter.type_cast_value"})],
            ["type_cast_value"],
            lambda rs: (
                "def type_cast_value" in rs[0].get("source", "")
                and "convert" in rs[0].get("source", "")
            ),
            "source returned is the method itself (core.py:2298); it converts a raw value via self.type.convert/__call__",
            False,
            scope=("src",),
        ),
        _task(
            "click-6",
            "entry_point",
            "What does calling a Command object (cli()) dispatch to?",
            [("who_does_this_call", {"qname": C + "core.BaseCommand.__call__"})],
            ["__call__"],
            _has(C + "core.BaseCommand.main"),
            "core.py:1159 BaseCommand.__call__ is `return self.main(*args, **kwargs)`",
            False,
            scope=("src",),
        ),
        _task(
            "click-7",
            "who_calls",
            "Who invokes HelpOption.show_help?",
            [("who_calls", {"qname": C + "decorators.HelpOption.show_help"})],
            ["show_help"],
            _has(C + "core.Parameter.process_value"),
            "decorators.py:542 registers show_help as the option's callback; core.py:2365 process_value calls `self.callback(ctx, self, value)`",
            True,
            extra=[
                (
                    "src/click/core.py",
                    "grep for show_help does not find the caller; the reader must find where Parameter.callback runs",
                )
            ],
        ),
    ],
    "flask": [
        _task(
            "flask-1",
            "who_calls",
            "Who calls Flask.ensure_sync via current_app?",
            [("who_calls", {"qname": F + "app.Flask.ensure_sync"})],
            ["ensure_sync"],
            _has(F + "views.View.as_view.view", F + "views.MethodView.dispatch_request"),
            "views.py:110,116,191 call `current_app.ensure_sync(...)` through the LocalProxy",
            True,
        ),
        _task(
            "flask-2",
            "dead_code",
            "Is sansio/app.py _make_timedelta unused?",
            [("who_calls", {"qname": F + "sansio.app._make_timedelta"})],
            ["_make_timedelta"],
            _has(F + "config.ConfigAttribute.__get__"),
            "NOT unused: sansio/app.py:227 passes it as get_converter=; ConfigAttribute.__get__ calls it",
            True,
        ),
        _task(
            "flask-3",
            "dead_code",
            "Is app.py _make_timedelta (line 74) unused?",
            [("who_calls", {"qname": F + "app._make_timedelta"})],
            ["_make_timedelta"],
            lambda rs: rs[0].get("count") == 0,
            "unused: only the def at app.py:74 (the used one is the duplicate in sansio/app.py); no importer in src/tests/docs",
            False,
        ),
        _task(
            "flask-4",
            "who_calls",
            "Who calls Flask.make_response?",
            [("who_calls", {"qname": F + "app.Flask.make_response"})],
            ["make_response"],
            _has(F + "app.Flask.finalize_request", F + "helpers.make_response"),
            "app.py:939 finalize_request; helpers.py:185 `current_app.make_response(args)` through the proxy",
            True,
        ),
        _task(
            "flask-5",
            "what_does",
            "What does Flask.wsgi_app do?",
            [("get_symbol_source", {"qname": F + "app.Flask.wsgi_app"})],
            ["wsgi_app"],
            lambda rs: (
                "def wsgi_app" in rs[0].get("source", "")
                and "full_dispatch_request" in rs[0].get("source", "")
            ),
            "source returned is the method itself (app.py:1479): pushes the request context, full_dispatch_request, finalizes",
            False,
        ),
        _task(
            "flask-6",
            "entry_point",
            "What does calling the app object (WSGI entry) dispatch to?",
            [("who_does_this_call", {"qname": F + "app.Flask.__call__"})],
            ["__call__"],
            _has(F + "app.Flask.wsgi_app"),
            "app.py:1536 Flask.__call__ is `return self.wsgi_app(environ, start_response)`",
            False,
            scope=("src",),
        ),
        _task(
            "flask-7",
            "who_calls",
            "Who calls a view built by View.as_view?",
            [("who_calls", {"qname": F + "views.View.as_view.view"})],
            ["as_view"],
            _has(F + "app.Flask.dispatch_request"),
            "app.py:902 dispatch_request calls `self.view_functions[rule.endpoint](**view_args)`: the registry filled by add_url_rule(view_func=...)",
            True,
            extra=[
                (
                    "src/flask/app.py",
                    "the registry lookup is `view_functions[rule.endpoint]`, no textual link to as_view",
                )
            ],
        ),
    ],
}

# ---------------------------------------------------------------------------
# Manual source review of the top runtime-edge targets (see report). Keyed by
# qualified name so a different top-5 shows up as "unreviewed" instead of
# silently inheriting a verdict.
# ---------------------------------------------------------------------------


WRONG_EXAMPLES: dict[str, list[dict[str, str]]] = {
    "click": [
        {
            "edge": "core.augment_usage_errors -> core.Parameter.process_value",
            "why": "augment_usage_errors is an @contextmanager generator; its span stays current while the with-body runs, so the body's calls are attributed to it. Source: core.py:2403 `value = self.process_value(ctx, value)` sits in handle_parse_result inside `with augment_usage_errors(...)`",
        },
    ],
    "flask": [
        {
            "edge": "app.Flask.dispatch_request -> sessions.SecureCookieSession.__init__.on_update",
            "why": "on_update is invoked by werkzeug's CallbackDict (untraced); the edge goes to the nearest TRACED ancestor, not a call site in flask",
        },
        {
            "edge": "helpers.stream_with_context -> wrappers.Request._load_form_data (and 1115 other enters)",
            "why": "all 1115 enters whose parent span had already exited have the exited span of stream_with_context as parent; flask code after it returned is still attributed to it",
        },
    ],
}


def _rv(verdict: str, mechanism: str, evidence: str) -> dict[str, str]:
    return {
        "verdict": verdict,
        "mechanism": mechanism,
        "evidence": evidence,
        "reviewed_by": "hand, against the pinned sources",
    }


REVIEWS: dict[str, dict[str, dict[str, Any]]] = {
    "click": {
        "src.click.core.Parameter.__repr__": _rv(
            "wrong edge",
            "serializer artifact (enter-time)",
            "trace: Parameter.__repr__ enter (line 637) precedes the enter of augment_usage_errors (639) whose args hold '<Argument dst>'; nothing in click calls repr(param) there",
        ),
        "src.click.exceptions.ClickException.__str__": _rv(
            "wrong edge",
            "serializer artifact (exit-time)",
            "1116 of 1124 enters are within 6 events before the exit of a span that raised; FlowTrace calls str(exc) to fill error.msg. 8 are roots called from untraced code",
        ),
        "src.click.testing._NamedTextIOWrapper.mode": _rv(
            "wrong edge",
            "serializer artifact (exit-time, C-level repr)",
            "callers are functions that RETURN the wrapper; their exit `result` is \"<_NamedTextIOWrapper name='<stdout>' mode='w' ...>\", i.e. TextIOWrapper.__repr__ read .name/.mode. No source line reads .mode (grep)",
        ),
        "src.click.testing._NamedTextIOWrapper.name": _rv(
            "wrong edge", "serializer artifact (exit-time, C-level repr)", "same as .mode"
        ),
        "src.click.core.BaseCommand.__repr__": _rv(
            "wrong edge",
            "serializer artifact (enter-time)",
            "callers take a command as argument; same orphan-parent pattern as Parameter.__repr__",
        ),
        "src.click._compat._FixupStream.__getattr__": _rv(
            "real, indirect",
            "__getattr__ delegation",
            "_compat.py:98 `return getattr(self._stream, name)`; reached when C-level io.TextIOWrapper probes attributes the wrapper lacks. Recorded caller is the nearest traced frame, not a call site in click",
        ),
        "src.click.types.ParamType.to_info_dict": _rv(
            "real",
            "inheritance (super())",
            "types.py:171,251,365,439: `info_dict = super().to_info_dict()` in each subclass; the static resolver does not link super() calls",
        ),
        "src.click.testing.EchoingStdin.__getattr__": _rv(
            "real, indirect",
            "__getattr__ delegation",
            "testing.py:27 `return getattr(self._input, x)`; attributes not defined on EchoingStdin fall through",
        ),
    },
    "flask": {
        "src.flask.sansio.scaffold.Scaffold.__repr__": _rv(
            "wrong edge",
            "serializer artifact (enter-time)",
            "callers take the app/blueprint as an argument; enter precedes the parent's enter in the trace",
        ),
        "src.flask.sansio.scaffold.setupmethod.wrapper_func": _rv(
            "real",
            "decorator wrapper",
            "scaffold.py:45: @setupmethod replaces the method (add_url_rule, route, errorhandler, ...) by wrapper_func via update_wrapper, so `self.add_url_rule(...)` runs wrapper_func; the static graph links the call to the undecorated name",
        ),
        "src.flask.sansio.app.App.debug": _rv(
            "real",
            "property",
            "app.py:406,495,653 read `self.debug`; sansio/app.py:550 is a @property, an attribute read is not a call to the static resolver",
        ),
        "src.flask.sansio.app.App.jinja_env": _rv(
            "real",
            "cached_property",
            "templating.py:149,161,203,218 `app.jinja_env.get_or_select_template(...)`; sansio/app.py:469 is a @cached_property",
        ),
        "src.flask.wrappers.Request.blueprints": _rv(
            "real",
            "property through the `request` LocalProxy",
            "app.py:521,774,804 `request.blueprints`; wrappers.py:181 is a @property; `request` is a werkzeug LocalProxy",
        ),
        "src.flask.sansio.app.App.select_jinja_autoescape": _rv(
            "real, indirect",
            "callback handed to Jinja",
            "passed as autoescape= when the Environment is created; Jinja calls it, the recorded caller is the traced function that first touched jinja_env",
        ),
        "src.flask.debughelpers.DebugFilesKeyError.__str__": _rv(
            "wrong edge",
            "serializer artifact (exit-time)",
            "str(exc) called to fill error.msg of the raising span",
        ),
    },
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def die(msg: str) -> None:
    print(f"ecosystem bench: {msg}", file=sys.stderr)
    raise SystemExit(2)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env["PYTHONHASHSEED"] = "0"
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    if extra:
        env.update(extra)
    return env


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def mb(n: float) -> float:
    return round(n / (1024 * 1024), 2)


def peak_rss_mb() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(ru / (1024 * 1024) if sys.platform == "darwin" else ru / 1024, 1)


def check_prereqs(flowtrace: Path, delta_cube: Path, need_delta: bool) -> dict[str, Any]:
    for tool in ("git", "uv"):
        if shutil.which(tool) is None:
            die(f"`{tool}` not found on PATH (needed to clone repos / build venvs)")
    stub = flowtrace / "capture" / "python" / "stub" / "sitecustomize.py"
    rt = flowtrace / "capture" / "python" / "flowtrace_runtime"
    if not stub.is_file() or not rt.is_dir():
        die(
            f"FlowTrace python capture layer not found under {flowtrace} "
            "(expected capture/python/stub/sitecustomize.py and capture/python/flowtrace_runtime). "
            "Pass --flowtrace <dir> or set FLOWTRACE_DIR."
        )
    info: dict[str, Any] = {"flowtrace_dir": str(flowtrace)}
    git = run(["git", "-C", str(flowtrace), "rev-parse", "HEAD"])
    info["flowtrace_commit"] = git.stdout.strip() if git.returncode == 0 else None
    dc_py = delta_cube / ".venv" / "bin" / "python"
    info["delta_cube_available"] = dc_py.is_file()
    if need_delta and not dc_py.is_file():
        info["delta_cube_skip_reason"] = (
            f"{dc_py} not found (pass --delta-cube or set DELTA_CUBE_DIR)"
        )
    git = run(["git", "-C", str(delta_cube), "rev-parse", "HEAD"])
    info["delta_cube_commit"] = git.stdout.strip() if git.returncode == 0 else None
    return info


# ---------------------------------------------------------------------------
# livespec worker: a fresh process per step (peak RSS per step, no stale caches)
# ---------------------------------------------------------------------------


def worker_main() -> None:
    import asyncio

    req = json.loads(sys.stdin.read())

    async def go() -> dict[str, Any]:
        from fastmcp import Client

        from livespec_mcp.server import mcp

        out: list[dict[str, Any]] = []
        async with Client(mcp) as client:
            rss0 = peak_rss_mb()
            for call in req["calls"]:
                t0 = time.perf_counter()
                res = await client.call_tool(
                    call["tool"],
                    {"workspace": req["workspace"], **call["args"]},
                    raise_on_error=False,
                )
                ms = (time.perf_counter() - t0) * 1000
                text = "".join(getattr(b, "text", "") for b in res.content)
                try:
                    data = json.loads(text)
                except ValueError:
                    data = None
                out.append(
                    {
                        "id": call.get("id", call["tool"]),
                        "tool": call["tool"],
                        "args": call["args"],
                        "is_error": bool(res.is_error),
                        "ms": round(ms, 1),
                        "chars": len(text),
                        "data": data if call.get("keep", True) else None,
                    }
                )
        return {"calls": out, "rss_start_mb": rss0, "rss_peak_mb": peak_rss_mb()}

    Path(req["out"]).write_text(json.dumps(asyncio.run(go())), encoding="utf-8")


class Livespec:
    """Runs batches of tool calls in a worker subprocess against one workspace."""

    def __init__(self, workspace: Path, scratch: Path):
        self.workspace = workspace
        self.scratch = scratch
        self.env = clean_env({"LIVESPEC_HOME": str(scratch / "livespec-home")})
        self.n = 0

    def call(self, calls: list[dict[str, Any]]) -> dict[str, Any]:
        self.n += 1
        out = self.scratch / f"worker-{self.n}.json"
        req = {"workspace": str(self.workspace), "calls": calls, "out": str(out)}
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            input=json.dumps(req),
            text=True,
            capture_output=True,
            env=self.env,
            cwd=str(HERE.parent),
        )
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"livespec worker failed: {proc.stderr[-1500:]}")
        res = json.loads(out.read_text(encoding="utf-8"))
        out.unlink()
        return res

    def one(self, tool: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        r = self.call([{"tool": tool, "args": args or {}, **kw}])
        c = r["calls"][0]
        c["rss_start_mb"], c["rss_peak_mb"] = r["rss_start_mb"], r["rss_peak_mb"]
        return c

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.workspace / '.mcp-docs' / 'docs.db'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con


# ---------------------------------------------------------------------------
# repo preparation, tests, traces
# ---------------------------------------------------------------------------


def prepare_repo(name: str, cfg: dict[str, Any], work: Path) -> tuple[Path, Path, dict[str, Any]]:
    checkout = work / name / "checkout"
    venv = work / name / "venv"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    log(f"[{name}] clone {cfg['ref']}")
    p = run(
        ["git", "clone", "-q", "--depth", "1", "--branch", cfg["ref"], cfg["url"], str(checkout)]
    )
    if p.returncode != 0:
        die(f"[{name}] clone failed: {p.stderr.strip()[-400:]}")
    sha = run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
    if sha != cfg["sha"]:
        die(f"[{name}] {cfg['ref']} resolved to {sha}, pinned {cfg['sha']}: the tag moved")
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    log(f"[{name}] venv (python {pyver}) + pinned test requirements")
    p = run(["uv", "venv", "-q", "--python", pyver, str(venv)])
    if p.returncode != 0:
        die(f"[{name}] uv venv failed: {p.stderr.strip()[-400:]}")
    env = clean_env({"VIRTUAL_ENV": str(venv)})
    cons = checkout.parent / "constraints.txt"
    cons.write_text(
        "".join(f"{k}=={v}\n" for k, v in cfg.get("constraints", {}).items()), encoding="utf-8"
    )
    for args in (
        ["-r", str(checkout / cfg["requirements"]), "-c", str(cons)],
        ["-e", str(checkout), "-c", str(cons)],
    ):
        p = run(["uv", "pip", "install", "-q", *args], env=env, cwd=str(checkout))
        if p.returncode != 0:
            die(f"[{name}] pip install {' '.join(args)} failed: {p.stderr.strip()[-600:]}")
    frz = run(["uv", "pip", "freeze"], env=env).stdout.splitlines()
    versions = {line.split("==")[0].lower(): line.split("==")[1] for line in frz if "==" in line}
    return checkout, venv, {"commit": sha, "ref": cfg["ref"], "packages": versions}


def parse_junit(path: Path) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    if not path.exists():
        return outcomes
    for tc in ET.parse(path).getroot().iter("testcase"):
        key = f"{tc.get('classname')}::{tc.get('name')}"
        kind = "passed"
        for child in tc:
            if child.tag in ("failure", "error", "skipped"):
                kind = {"failure": "failed", "error": "error", "skipped": "skipped"}[child.tag]
                break
        outcomes[key] = kind
    return outcomes


def first_error(path: Path, keys: set[str]) -> str | None:
    if not path.exists():
        return None
    for tc in ET.parse(path).getroot().iter("testcase"):
        if f"{tc.get('classname')}::{tc.get('name')}" in keys:
            for child in tc:
                if child.tag in ("failure", "error"):
                    return (
                        f"{tc.get('classname')}::{tc.get('name')}: "
                        + ((child.get("message") or child.text or "")[:400])
                    )
    return None


def run_suite(
    checkout: Path, venv: Path, junit: Path, extra_env: dict[str, str] | None = None
) -> dict[str, Any]:
    cmd = [str(venv / "bin" / "python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
           "--junitxml", str(junit)]  # fmt: skip
    t0 = time.perf_counter()
    p = subprocess.run(
        cmd, cwd=checkout, env=clean_env(extra_env), text=True, capture_output=True, timeout=3600
    )
    dt = time.perf_counter() - t0
    outcomes = parse_junit(junit)
    counts = Counter(outcomes.values())
    return {
        "seconds": round(dt, 2),
        "returncode": p.returncode,
        "tests": len(outcomes),
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["error"],
        "skipped": counts["skipped"],
        "_outcomes": outcomes,
        "_tail": (p.stdout + p.stderr)[-600:],
    }


def analyse_trace(path: Path, clean_to: Path | None) -> dict[str, Any]:
    """Two streaming passes: size, events, and the ways spans lie about parents.

    enter-time serializer artifact: an `enter` whose parent_id has not been
      emitted yet. FlowTrace builds the parent's `args` JSON (calling `repr()`
      / properties of the arguments) after allocating the parent span but
      before writing its `enter`; an instrumented `__repr__` or property in the
      traced package becomes a child span the program never called.
    exit-time serializer artifact (heuristic): a `__str__` / `__repr__` enter
      within the last 12 events before its parent's `exit`. FlowTrace calls
      `str(exc)` to fill `error.msg` and `repr(value)` to fill `result`, after
      the body ran, while the parent span is still current. Checked by hand on
      click's ClickException.__str__: 1116 of 1124 enters match and all of those
      parents exited with an error; the other 8 are roots called from untraced
      code. It can also drop a real `repr()` that happens to be a function's
      last call, so the cleaned trace is a lower bound on runtime edges.
    Descendants of either kind are artifacts too; `clean_to` receives the trace
    without them.
    parent already exited: an `enter` whose parent span already emitted `exit`
      (resumed generators / callbacks): the call is real, the caller is not.
    """
    seen: set[str] = set()
    exited: set[str] = set()
    parent: dict[str, str | None] = {}
    order: list[str] = []
    enter_idx: dict[str, int] = {}
    loc: dict[str, tuple[Any, Any]] = {}
    exited_children: set[str] = set()
    exit_at: dict[str, int] = {}
    cand: dict[str, list[str]] = defaultdict(list)
    flagged: dict[str, str] = {}  # sid -> kind
    by_method: Counter[str] = Counter()
    events = enters = exited_parent = 0
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            events += 1
            e = json.loads(line)
            sid, pid = e["span_id"], e.get("parent_id")
            if e["event"] == "enter":
                enters += 1
                parent[sid] = pid
                order.append(sid)
                enter_idx[sid] = i
                loc[sid] = (e.get("file"), e.get("line"))
                if pid and pid not in seen:
                    flagged[sid] = "enter_time"
                    by_method[e["method"]] += 1
                elif pid in exited:
                    exited_parent += 1
                    exited_children.add(sid)
                if e["method"] in ("__str__", "__repr__") and pid:
                    cand[pid].append(sid)
                seen.add(sid)
            else:
                exited.add(sid)
                exit_at[sid] = i
    for pid, i_exit in exit_at.items():
        for sid in cand.get(pid, ()):
            if i_exit - enter_idx[sid] <= 12 and sid not in flagged:
                flagged[sid] = "exit_time"
    art = set(flagged)
    descendants = 0
    for sid in order:
        if sid not in art and parent.get(sid) in art:
            art.add(sid)
            descendants += 1
    kinds = Counter(flagged.values())
    pairs: dict[str, set[tuple[Any, Any]]] = {"normal": set(), "artifact": set(), "exited": set()}
    for sid in order:
        pid = parent.get(sid)
        if pid is None or pid not in loc or loc[pid] == loc[sid]:
            continue
        kind = "artifact" if sid in art else "exited" if sid in exited_children else "normal"
        pairs[kind].add((loc[pid], loc[sid]))
    all_pairs = pairs["normal"] | pairs["artifact"] | pairs["exited"]
    if clean_to is not None:
        with path.open(encoding="utf-8") as f, clean_to.open("w", encoding="utf-8") as out:
            for line in f:
                if json.loads(line)["span_id"] not in art:
                    out.write(line)
    n_art = len(art)
    return {
        "size_mb": mb(path.stat().st_size),
        "events": events,
        "enter_events": enters,
        "serializer_artifact_enters": n_art,
        "serializer_artifact_share_pct": round(100 * n_art / max(enters, 1), 1),
        "serializer_artifact_enter_time": kinds["enter_time"],
        "serializer_artifact_exit_time_heuristic": kinds["exit_time"],
        "serializer_artifact_descendants": descendants,
        "serializer_artifact_enter_time_by_method_top": dict(
            Counter({k: v for k, v in by_method.items() if v}).most_common(6)
        ),
        "parent_already_exited_enters": exited_parent,
        "distinct_caller_callee_pairs": len(all_pairs),
        "pairs_only_from_serializer_artifacts": len(
            pairs["artifact"] - pairs["normal"] - pairs["exited"]
        ),
        "pairs_only_from_exited_parent_events": len(
            pairs["exited"] - pairs["normal"] - pairs["artifact"]
        ),
        "_artifact_methods": [*sorted(k for k, v in by_method.items() if v), "__str__", "__repr__"],
    }


def generator_functions(checkout: Path, rel: str) -> list[tuple[int, int]]:
    """(first decorator line, def line) of functions that contain a direct yield."""
    try:
        tree = ast.parse((checkout / rel).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    def has_yield(fn: ast.AST) -> bool:
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                return True
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            stack.extend(ast.iter_child_nodes(n))
        return False

    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and has_yield(n):
            first = min([d.lineno for d in n.decorator_list] + [n.lineno])
            out.append((first, n.lineno))
    return out


def runtime_edge_audit(ls: Livespec, checkout: Path, artifact_methods: list[str]) -> dict[str, Any]:
    con = ls.db()
    try:
        rows = con.execute(
            """SELECT s1.qualified_name AS src, f1.path AS sf, s1.start_line AS sl,
                      s2.qualified_name AS dst, s2.name AS dname, f2.path AS df
                 FROM symbol_edge e
                 JOIN symbol s1 ON s1.id = e.src_symbol_id JOIN file f1 ON f1.id = s1.file_id
                 JOIN symbol s2 ON s2.id = e.dst_symbol_id JOIN file f2 ON f2.id = s2.file_id
                WHERE e.origin = ?""",
            (RUNTIME_ORIGIN,),
        ).fetchall()
    finally:
        con.close()
    gens: dict[str, list[tuple[int, int]]] = {}
    gen_src = art_dst = 0
    for r in rows:
        if r["sf"] not in gens:
            gens[r["sf"]] = generator_functions(checkout, r["sf"])
        if any(a <= r["sl"] <= b for a, b in gens[r["sf"]]):
            gen_src += 1
        if r["dname"] in artifact_methods:
            art_dst += 1
    return {
        "runtime_edges": len(rows),
        "edges_from_generator_functions": gen_src,
        "edges_into_serializer_artifact_methods": art_dst,
    }


def top_new_callers(ls: Livespec, n: int = 5) -> list[dict[str, Any]]:
    con = ls.db()
    try:
        rows = con.execute(
            """SELECT s2.qualified_name AS dst, f2.path AS path, s2.start_line AS line,
                      COUNT(DISTINCT e.src_symbol_id) AS callers,
                      GROUP_CONCAT(DISTINCT s1.qualified_name) AS who
                 FROM symbol_edge e
                 JOIN symbol s1 ON s1.id = e.src_symbol_id
                 JOIN symbol s2 ON s2.id = e.dst_symbol_id JOIN file f2 ON f2.id = s2.file_id
                WHERE e.origin = ? AND f2.path NOT LIKE 'tests/%'
                GROUP BY e.dst_symbol_id
                ORDER BY callers DESC, dst ASC LIMIT ?""",
            (RUNTIME_ORIGIN, n),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "qname": r["dst"],
            "file": r["path"],
            "line": r["line"],
            "new_runtime_callers": r["callers"],
            "sample_callers": sorted(r["who"].split(","))[:4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# payload proxy
# ---------------------------------------------------------------------------


def files_for_task(checkout: Path, task: dict[str, Any]) -> dict[str, Any]:
    pats = [re.compile(rf"\b{re.escape(t)}\b") for t in task["terms"]]
    hits: dict[str, int] = {}
    grep_chars = 0
    for sub in task.get("scope", ("src", "tests")):
        for p in sorted((checkout / sub).rglob("*.py")):
            text = p.read_text(encoding="utf-8", errors="replace")
            matched = [
                (n, ln)
                for n, ln in enumerate(text.splitlines(), 1)
                if any(x.search(ln) for x in pats)
            ]
            if matched:
                rel = str(p.relative_to(checkout))
                hits[rel] = len(text)
                grep_chars += sum(len(f"{rel}:{n}:{ln}\n") for n, ln in matched)
    extra: dict[str, int] = {}
    for rel, _why in task.get("extra", []):
        if rel not in hits:
            extra[rel] = len((checkout / rel).read_text(encoding="utf-8", errors="replace"))
    src_only = sum(v for k, v in hits.items() if k.startswith("src/")) + sum(extra.values())
    return {
        "grep_hit_files": len(hits),
        "extra_files": len(extra),
        "chars": sum(hits.values()) + sum(extra.values()),
        "src_only_chars": src_only,
        "grep_output_chars": grep_chars,
        "files": sorted(list(hits) + list(extra)),
    }


def run_tasks(ls: Livespec, tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in tasks:
        calls = [
            {"id": f"{t['id']}.{i}", "tool": tool, "args": args}
            for i, (tool, args) in enumerate(t["calls"])
        ]
        res = ls.call(calls)["calls"]
        try:
            ok = bool(t["check"]([c["data"] or {} for c in res]))
        except Exception as e:  # a malformed answer is an incorrect answer
            ok = False
            res[0]["check_error"] = repr(e)
        out[t["id"]] = {
            "calls": len(res),
            "chars": sum(c["chars"] for c in res),
            "ms": round(sum(c["ms"] for c in res), 1),
            "tools": [c["tool"] for c in res],
            "correct": ok and not any(c["is_error"] for c in res),
            "wrong_edges_in_answer": sorted(
                set(t.get("wrong", []))
                & {
                    x["qualified_name"]
                    for x in (
                        (res[0]["data"] or {}).get("callers")
                        or (res[0]["data"] or {}).get("callees")
                        or []
                    )
                }
            ),
            "answer": [
                ("*" if x.get("via_external_edge") else "") + x["qualified_name"]
                for x in (
                    (res[0]["data"] or {}).get("callers")
                    or (res[0]["data"] or {}).get("callees")
                    or []
                )
            ][:15],
        }
    return out


# ---------------------------------------------------------------------------
# delta-cube (optional)
# ---------------------------------------------------------------------------

DELTA_SNIPPET = r"""
import json, sys, time
from delta_cube.db.database import init_database
from delta_cube.cube import DeltaCodeCube
from delta_cube.cube.review import hotspots
path = sys.argv[1]
conn = init_database()
t0 = time.perf_counter()
pts = DeltaCodeCube(conn).index_directory(path, ["*.py"], True, None)
t1 = time.perf_counter()
r = hotspots(conn, project=path, limit=10, refresh=True)
t2 = time.perf_counter()
print(json.dumps({"indexed": len(pts), "index_s": round(t1 - t0, 2),
                  "hotspots_s": round(t2 - t1, 2),
                  "payload_chars": len(json.dumps(r, default=str)), "result": r}, default=str))
"""


def delta_cube_block(delta_cube: Path, checkout: Path, scratch: Path) -> dict[str, Any]:
    py = delta_cube / ".venv" / "bin" / "python"
    if not py.is_file():
        return {"skipped": f"{py} not found"}
    data_dir = scratch / "dcc-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = clean_env({"DCC_DATA_DIR": str(data_dir)})
    p = subprocess.run(
        [str(py), "-c", DELTA_SNIPPET, str(checkout)],
        env=env, text=True, capture_output=True, cwd=str(scratch),
    )  # fmt: skip
    if p.returncode != 0:
        return {"skipped": f"delta-cube failed: {p.stderr.strip()[-500:]}"}
    r = json.loads(p.stdout.strip().splitlines()[-1])
    res = r.pop("result")
    top = res.get("hotspots") or res.get("files") or res.get("results") or []
    r["top10"] = top if isinstance(top, list) else res
    if res.get("error"):
        r["error"] = res["error"]
    return r


# ---------------------------------------------------------------------------
# per-repo benchmark
# ---------------------------------------------------------------------------


def dead(ls: Livespec, **flags: Any) -> dict[str, Any]:
    c = ls.one("find_dead_code", {"limit": 2000, **flags})
    d = c["data"] or {}
    rt = d.get("runtime") or {}
    return {
        "candidates": d.get("count"),
        "by_kind": d.get("by_kind"),
        "filtered_out": d.get("filtered_out"),
        "rescued_by_runtime": rt.get("rescued_by_runtime"),
        "not_seen": rt.get("not_seen"),
        "no_evidence": rt.get("no_evidence"),
        "ms": c["ms"],
        "chars": c["chars"],
        "_names": sorted(x["qualified_name"] for x in d.get("dead_symbols", [])),
    }


def strip_private(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip_private(v) for v in o]
    return o


def bench_repo(
    name: str, args: argparse.Namespace, work: Path, pre: dict[str, Any]
) -> dict[str, Any]:
    cfg = REPOS[name]
    flowtrace: Path = args.flowtrace
    res: dict[str, Any] = {}
    checkout, venv, res["repo"] = prepare_repo(name, cfg, work)
    scratch = work / name
    traces = scratch / "traces"
    traces.mkdir(exist_ok=True)
    ls = Livespec(checkout, scratch)

    # 1. static ------------------------------------------------------------
    log(f"[{name}] static index")
    idx = ls.one("index_project", {})
    d = idx["data"] or {}
    static_dead = {
        "default": dead(ls),
        "include_public": dead(ls, include_public=True),
        # extra variant: the default hides "infrastructure" (dunders, framework
        # hooks, ...) which on libraries is most of the graph
        "include_infrastructure": dead(ls, include_infrastructure=True),
    }
    res["static"] = {
        "index_ms": idx["ms"],
        "index_peak_rss_mb": idx["rss_peak_mb"],
        "files": d.get("files_total"),
        "symbols": d.get("symbols_total"),
        "call_edges": d.get("edges_total"),
        "find_dead_code": {
            k: {x: y for x, y in v.items() if not x.startswith("_")} for k, v in static_dead.items()
        },
    }

    # 2. characterise ---------------------------------------------------------
    log(f"[{name}] tests: plain")
    runs = [
        run_suite(checkout, venv, scratch / f"junit-plain-{i}.xml")
        for i in range(1 if args.quick else 2)
    ]
    plain = min(runs, key=lambda r: r["seconds"])
    log(f"[{name}] tests: under FlowTrace")
    trace = traces / f"{name}.jsonl"
    ft = flowtrace / "capture" / "python"
    traced = run_suite(
        checkout, venv, scratch / "junit-traced.xml",
        {
            "PYTHONPATH": os.pathsep.join([str(ft / "stub"), str(ft)]),
            "FLOWTRACE_ENABLE": "1",
            "FLOWTRACE_OUTPUT": str(trace),
            "FLOWTRACE_PACKAGE_PREFIX": cfg["prefix"],
            "FLOWTRACE_MAX_ARG_LENGTH": str(args.max_arg_length),
            "FLOWTRACE_ROOT": str(checkout),
        },
    )  # fmt: skip
    if not trace.exists():
        die(f"[{name}] FlowTrace wrote no trace; stderr tail: {traced['_tail']}")
    changed = sorted(
        k for k in set(plain["_outcomes"]) | set(traced["_outcomes"])
        if plain["_outcomes"].get(k) != traced["_outcomes"].get(k)
    )  # fmt: skip
    clean = traces / f"{name}.clean.jsonl"
    tinfo = analyse_trace(trace, None if args.quick else clean)
    git_dirty = "\n".join(
        ln
        for ln in run(["git", "-C", str(checkout), "status", "--porcelain"]).stdout.splitlines()
        if ".mcp-docs" not in ln
    )
    res["characterise"] = {
        "plain": {k: v for k, v in plain.items() if not k.startswith("_")},
        "plain_all_runs_s": [r["seconds"] for r in runs],
        "baseline_failures": sorted(k for k, v in plain["_outcomes"].items() if v in ("failed", "error")),
        "traced": {k: v for k, v in traced.items() if not k.startswith("_")},
        "overhead_factor": round(traced["seconds"] / max(plain["seconds"], 1e-9), 2),
        "overhead_factor_vs_first_plain_run": round(traced["seconds"] / max(runs[0]["seconds"], 1e-9), 2),
        "outcome_set_changed": bool(changed),
        "outcome_diffs": [
            {"test": k, "plain": plain["_outcomes"].get(k), "traced": traced["_outcomes"].get(k)}
            for k in changed[:10]
        ],
        "first_error_under_tracing": first_error(scratch / "junit-traced.xml", set(changed)) if changed else None,
        "max_arg_length": args.max_arg_length,
        "trace": {k: v for k, v in tinfo.items() if not k.startswith("_")},
        "checkout_paths_dirtied_by_runs": git_dirty.splitlines(),
    }  # fmt: skip

    # 3. ingest ---------------------------------------------------------------
    log(f"[{name}] ingest (dry-run, cleaned dry-run, apply)")
    dry = ls.one("ingest_traces", {"trace_path": str(trace), "dry_run": True})
    dry_clean = None
    if not args.quick and clean.exists():
        dry_clean = ls.one("ingest_traces", {"trace_path": str(clean), "dry_run": True})
    app = ls.one("ingest_traces", {"trace_path": str(trace), "dry_run": False})
    p = dry["data"] or {}
    pa = app["data"] or {}
    ev = p.get("enter_events") or 0
    res["ingest"] = {
        "enter_events": ev,
        "resolved_events": p.get("resolved_events"),
        "resolved_pct": round(100 * (p.get("resolved_events") or 0) / max(ev, 1), 2),
        "resolved_by": p.get("resolved_by"),
        "unresolved": p.get("unresolved"),
        "unresolved_samples": {k: v[:3] for k, v in (p.get("unresolved_samples") or {}).items()},
        "symbols_hit": p.get("symbols_hit"),
        "edges_observed": p.get("edges_observed"),
        "edges_to_add": p.get("edges_to_add"),
        "already_known": p.get("already_known"),
        "skipped": p.get("skipped"),
        "dry_run": {"ms": dry["ms"], "peak_rss_mb": dry["rss_peak_mb"], "rss_start_mb": dry["rss_start_mb"]},
        "applied": {
            "ms": app["ms"], "peak_rss_mb": app["rss_peak_mb"], "rss_start_mb": app["rss_start_mb"],
            "edges_added": pa.get("edges_added"), "edges_removed": pa.get("edges_removed"),
        },
        "serializer_artifacts_removed_dry_run": (
            None if dry_clean is None else {
                "enter_events": (dry_clean["data"] or {}).get("enter_events"),
                "symbols_hit": (dry_clean["data"] or {}).get("symbols_hit"),
                "edges_to_add": (dry_clean["data"] or {}).get("edges_to_add"),
                "already_known": (dry_clean["data"] or {}).get("already_known"),
            }
        ),
    }  # fmt: skip
    res["ingest"]["wrong_edge_audit"] = runtime_edge_audit(ls, checkout, tinfo["_artifact_methods"])

    # 4. after ----------------------------------------------------------------
    log(f"[{name}] find_dead_code after")
    after_dead = {
        "default": dead(ls),
        "include_public": dead(ls, include_public=True),
        "include_infrastructure": dead(ls, include_infrastructure=True),
    }
    rescued = {}
    for k in after_dead:
        gone = sorted(set(static_dead[k]["_names"]) - set(after_dead[k]["_names"]))
        rescued[k] = {"dropped_from_candidates": len(gone), "sample": gone[:8]}
    top = top_new_callers(ls, 5)
    tasks = TASKS.get(name, [])
    wc_calls = [{"id": t["qname"], "tool": "who_calls", "args": {"qname": t["qname"]}} for t in top]
    who_after = ls.call(wc_calls)["calls"]
    runtime_state = run_tasks(ls, tasks)

    # static-only state: remove runtime evidence, re-measure, restore
    rem = ls.one("ingest_traces", {"remove": True})
    who_before = ls.call(wc_calls)["calls"]
    static_state = run_tasks(ls, tasks)
    top_clean: list[dict[str, Any]] = []
    if not args.quick and clean.exists():
        # same ranking on the trace without serializer artifacts: what is left
        # are the targets worth reading (the raw top-5 can be all artifacts)
        ls.one("ingest_traces", {"trace_path": str(clean), "dry_run": False})
        top_clean = top_new_callers(ls, 5)
        cc = [
            {"id": t["qname"], "tool": "who_calls", "args": {"qname": t["qname"]}}
            for t in top_clean
        ]
        c_after = ls.call(cc)["calls"]
        ls.one("ingest_traces", {"remove": True})
        c_before = ls.call(cc)["calls"]
        for t, a, b in zip(top_clean, c_after, c_before, strict=True):
            t["who_calls_before"] = len((b["data"] or {}).get("callers", []))
            t["who_calls_after"] = len((a["data"] or {}).get("callers", []))
            t["source_review"] = REVIEWS.get(name, {}).get(t["qname"], {"verdict": "unreviewed"})
    restore = ls.one("ingest_traces", {"trace_path": str(trace), "dry_run": False})
    restored_ok = (restore["data"] or {}).get("edges_added") == pa.get("edges_added")

    reviews = REVIEWS.get(name, {})
    res["after"] = {
        "find_dead_code": {
            k: {
                **{x: y for x, y in after_dead[k].items() if not x.startswith("_")},
                **rescued[k],
                "static_candidates": static_dead[k]["candidates"],
            }
            for k in after_dead
        },
        "top_new_runtime_callers": [
            {
                **t,
                "who_calls_before": len((b["data"] or {}).get("callers", [])),
                "who_calls_after": len((a["data"] or {}).get("callers", [])),
                "who_calls_after_runtime_only": len(_runtime_callers(a["data"] or {})),
                "source_review": reviews.get(t["qname"], {"verdict": "unreviewed"}),
            }
            for t, a, b in zip(top, who_after, who_before, strict=True)
        ],
        "top_new_runtime_callers_artifact_free": top_clean,
        "wrong_edges_confirmed_in_source": WRONG_EXAMPLES.get(name, []),
        "remove_then_reingest_roundtrip": {
            "removed_edges": (rem["data"] or {}).get("removed"),
            "reingest_restored_same_edge_count": restored_ok,
        },
    }  # fmt: skip

    # 5. payload proxy --------------------------------------------------------
    rows = []
    for t in tasks:
        f = files_for_task(checkout, t)
        rt, st = runtime_state[t["id"]], static_state[t["id"]]
        rows.append(
            {
                "id": t["id"],
                "kind": t["kind"],
                "question": t["question"],
                "needs_runtime_edge": t["runtime_only"],
                "truth": t["truth"],
                "by_file": {"files": f["grep_hit_files"] + f["extra_files"], "chars": f["chars"],
                            "approx_tokens": f["chars"] // 4, "src_only_chars": f["src_only_chars"],
                            "grep_output_chars": f["grep_output_chars"], "file_list": f["files"]},
                "by_tool": {"calls": rt["calls"], "tools": rt["tools"], "chars": rt["chars"],
                            "approx_tokens": rt["chars"] // 4, "correct": rt["correct"],
                            "correct_static_only": st["correct"],
                            "chars_static_only": st["chars"],
                            "answer": rt["answer"], "answer_static_only": st["answer"],
                            "known_wrong_edges_in_answer": rt["wrong_edges_in_answer"]},
            }
        )  # fmt: skip
    tf = sum(r["by_file"]["chars"] for r in rows)
    tt = sum(r["by_tool"]["chars"] for r in rows)
    res["payload_proxy"] = {
        "note": (
            "PROXY: characters an agent would have to read, not an agent session. "
            "approx_tokens = chars/4 (approximate). by_file = whole files that a "
            "grep-then-read approach opens; by_tool = text returned by the minimal "
            "livespec call sequence. Leaves out: reasoning, wrong turns, partial "
            "reads (offset/limit), the prompt, tool-call overhead, caching, and "
            "whether the agent would ask for the right call in the first place."
        ),
        "tasks": rows,
        "total": {
            "by_file_chars": tf, "by_file_approx_tokens": tf // 4,
            "by_file_src_only_chars": sum(r["by_file"]["src_only_chars"] for r in rows),
            "grep_output_chars": sum(r["by_file"]["grep_output_chars"] for r in rows),
            "by_tool_chars": tt, "by_tool_approx_tokens": tt // 4,
            "by_tool_calls": sum(r["by_tool"]["calls"] for r in rows),
            "tool_correct": sum(r["by_tool"]["correct"] for r in rows),
            "tool_correct_static_only": sum(r["by_tool"]["correct_static_only"] for r in rows),
            "tasks": len(rows),
        },
    }  # fmt: skip

    # 6. delta-cube -----------------------------------------------------------
    if args.quick:
        res["delta_cube"] = {"skipped": "--quick"}
    elif not pre["delta_cube_available"]:
        res["delta_cube"] = {"skipped": pre.get("delta_cube_skip_reason", "not available")}
    else:
        log(f"[{name}] delta-cube hotspots")
        res["delta_cube"] = delta_cube_block(args.delta_cube, checkout, scratch)
    return res


def print_table(results: dict[str, Any]) -> None:
    hdr = (
        "repo",
        "symbols",
        "edges",
        "dead(st)",
        "dead(rt)",
        "not_seen",
        "no_evid",
        "res%",
        "new edges",
        "ovh x",
        "trace MB",
    )
    print("\n" + "  ".join(f"{h:>10}" for h in hdr))
    for n, r in results.items():
        if "error" in r:
            print(f"{n:>10}  ERROR {r['error']}")
            continue
        s, c, i, a = (
            r["static"],
            r["characterise"],
            r["ingest"],
            r["after"]["find_dead_code"]["default"],
        )
        row = (n, s["symbols"], s["call_edges"], a["static_candidates"], a["candidates"], a["not_seen"],
               a["no_evidence"], i["resolved_pct"], i["edges_to_add"], c["overhead_factor"], c["trace"]["size_mb"])  # fmt: skip
        print("  ".join(f"{v!s:>10}" for v in row))
    for n, r in results.items():
        pp = r.get("payload_proxy")
        if not pp:
            continue
        print(f"\n[{n}] payload proxy (chars; tokens ~ chars/4)")
        print(
            f"{'task':<10}{'by-file':>10}{'by-tool':>9}{'calls':>6}{'ok':>5}{'ok(static)':>11}  question"
        )
        for t in pp["tasks"]:
            print(f"{t['id']:<10}{t['by_file']['chars']:>10}{t['by_tool']['chars']:>9}{t['by_tool']['calls']:>6}"
                  f"{('yes' if t['by_tool']['correct'] else 'NO'):>5}{('yes' if t['by_tool']['correct_static_only'] else 'NO'):>11}  {t['question'][:60]}")  # fmt: skip
        tot = pp["total"]
        print(f"{'TOTAL':<10}{tot['by_file_chars']:>10}{tot['by_tool_chars']:>9}{tot['by_tool_calls']:>6}"
              f"{tot['tool_correct']:>5}/{tot['tasks']}")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--repo", choices=sorted(REPOS), action="append", help="repeatable; default: all"
    )
    ap.add_argument("--quick", action="store_true",
                    help="click only; single plain run, no cleaned-trace comparison, no delta-cube")  # fmt: skip
    ap.add_argument("--json", type=Path, default=None, help="write the report here")
    ap.add_argument(
        "--keep", action="store_true", help="keep the scratch directory (venvs, traces, dbs)"
    )
    ap.add_argument(
        "--workdir", type=Path, default=None, help="scratch directory (default: a fresh temp dir)"
    )
    ap.add_argument(
        "--flowtrace", type=Path, default=DEFAULT_FLOWTRACE, help="flowtrace-debugger checkout"
    )
    ap.add_argument(
        "--delta-cube",
        type=Path,
        default=DEFAULT_DELTA_CUBE,
        help="delta-cube checkout (with .venv)",
    )
    ap.add_argument(
        "--max-arg-length", type=int, default=64, help="FLOWTRACE_MAX_ARG_LENGTH (default 64)"
    )
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        worker_main()
        return

    targets = args.repo or (["click"] if args.quick else list(REPOS))
    if args.quick:
        targets = targets[:1]
    pre = check_prereqs(args.flowtrace, args.delta_cube, need_delta=not args.quick)
    work = args.workdir or Path(tempfile.mkdtemp(prefix="livespec-eco-"))
    work.mkdir(parents=True, exist_ok=True)
    log(f"scratch: {work}")

    report: dict[str, Any] = {
        "meta": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "livespec_commit": run(
                ["git", "-C", str(HERE.parent), "rev-parse", "HEAD"]
            ).stdout.strip(),
            "quick": args.quick,
            "scrubbed_env": list(SCRUBBED_ENV),
            "timings_are_single_runs": True,
            **pre,
        },
        "results": {},
    }
    try:
        for name in targets:
            try:
                report["results"][name] = strip_private(bench_repo(name, args, work, pre))
            except SystemExit:
                raise
            except Exception as e:  # keep the other repo's numbers
                report["results"][name] = {"error": repr(e)}
                log(f"[{name}] FAILED: {e!r}")
    finally:
        if args.keep or args.workdir:
            log(f"scratch kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    print_table(report["results"])
    if args.json:
        args.json.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        log(f"wrote {args.json}")


if __name__ == "__main__":
    main()
