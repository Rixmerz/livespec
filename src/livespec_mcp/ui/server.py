"""The `livespec ui` HTTP server (stdlib only).

One process, bound to loopback by default, serving:

    /, /p/<id>[/...], /g/<id>      the single-page UI (static package data)
    /static/<file>                 its CSS/JS
    /api/projects                  every project, grouped (registry + group DBs)
    /api/p/<id>                    project overview
    /api/p/<id>/coverage           audit_coverage summary (never recorded)
    /api/p/<id>/specs              Specs by capability, with coverage
    /api/p/<id>/specs/<spec_id>    one Spec: scenarios, symbols, dependencies
    /api/p/<id>/symbols?q=         symbol search
    /api/p/<id>/symbol?qname=      one symbol + who calls it / what it calls
    /api/p/<id>/endpoints          entry points
    /api/g/<id>                    a group DB: repos, cross-repo Specs, route hops
    POST /api/forget               drop a registry entry (the only write, and
                                   it is to the user-level registry)

Every project database is opened read-only per request; nothing is written to
any project directory. Requests whose ``Host`` is not this loopback server are
refused (DNS rebinding), and the one POST needs a custom header a cross-site
form cannot send.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from livespec_mcp import registry
from livespec_mcp.ui import data

_STATIC_FILES = {"app.css", "app.js", "favicon.svg"}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _static_bytes(name: str) -> bytes:
    return resources.files("livespec_mcp.ui").joinpath("static", name).read_bytes()


class _Handler(BaseHTTPRequestHandler):
    server_version = "livespec-ui"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("livespec ui: " + (format % args) + "\n")

    def _headers(self, status: int, ctype: str, length: int, *, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, *, cache: bool = False) -> None:
        self._headers(status, ctype, len(body), cache=cache)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        allowed = set(_LOOPBACK) | {str(self.server.server_address[0]).lower()}
        return name in allowed

    # -- routing ----------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
            return
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if not parts or parts[0] in {"p", "g"}:
                self._send(200, _static_bytes("index.html"), "text/html; charset=utf-8")
                return
            if parts[0] == "static" and len(parts) == 2 and parts[1] in _STATIC_FILES:
                ctype = mimetypes.guess_type(parts[1])[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                self._send(200, _static_bytes(parts[1]), ctype, cache=True)
                return
            if parts[0] == "api":
                self._json(self._api(parts[1:], query))
                return
            self._error(HTTPStatus.NOT_FOUND, f"no route for {url.path}")
        except data.NotFound as e:
            self._error(HTTPStatus.NOT_FOUND, str(e))
        except (sqlite3.Error, OSError) as e:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, f"{type(e).__name__}: {e}")
        except Exception as e:  # pragma: no cover — last-resort shape
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(e).__name__}: {e}")

    def _api(self, parts: list[str], query: dict[str, str]) -> Any:
        if parts == ["projects"]:
            return data.home()
        if len(parts) >= 2 and parts[0] == "p":
            pid, rest = parts[1], parts[2:]
            if not rest:
                return data.project_overview(pid)
            if rest == ["coverage"]:
                return data.project_coverage(pid)
            if rest == ["specs"]:
                return data.specs(pid)
            if len(rest) == 2 and rest[0] == "specs":
                return data.spec_detail(pid, rest[1])
            if rest == ["symbols"]:
                return data.search_symbols(pid, query.get("q", ""), int(query.get("limit") or 50))
            if rest == ["symbol"]:
                return data.symbol_detail(pid, query.get("qname", ""))
            if rest == ["endpoints"]:
                return data.endpoints(pid)
        if len(parts) == 2 and parts[0] == "g":
            return data.group(parts[1])
        raise data.NotFound("unknown API route /api/" + "/".join(parts))

    def do_POST(self) -> None:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
            return
        if self.headers.get("X-Livespec-UI") != "1":
            self._error(HTTPStatus.FORBIDDEN, "missing X-Livespec-UI header")
            return
        if urlsplit(self.path).path != "/api/forget":
            self._error(HTTPStatus.NOT_FOUND, "no such action")
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 64_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._error(HTTPStatus.BAD_REQUEST, "body must be JSON")
            return
        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str) or not path:
            self._error(HTTPStatus.BAD_REQUEST, "body needs a 'path'")
            return
        removed = registry.forget(path)
        if not removed:
            self._error(HTTPStatus.NOT_FOUND, f"{path} is not in the registry")
            return
        self._json({"forgotten": path})


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    quiet = False


def make_server(host: str = "127.0.0.1", port: int = 0, *, quiet: bool = False) -> UIServer:
    srv = UIServer((host, port), _Handler)
    srv.quiet = quiet
    return srv


def server_url(srv: UIServer) -> str:
    host, port = srv.server_address[:2]
    shown = f"[{host}]" if ":" in str(host) else host
    return f"http://{shown}:{port}/"


def serve_in_thread(srv: UIServer) -> threading.Thread:
    t = threading.Thread(target=srv.serve_forever, name="livespec-ui", daemon=True)
    t.start()
    return t


__all__ = ["UIServer", "make_server", "serve_in_thread", "server_url"]
