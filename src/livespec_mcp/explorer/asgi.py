"""Mount the static Spec Explorer bundle on any Starlette/FastAPI app at ``/explorer``.

Typical use (manual)::

    from fastapi import FastAPI
    from livespec_mcp.explorer import mount_explorer

    app = FastAPI()
    mount_explorer(app)  # -> GET /explorer/, /explorer/endpoints, …

``export_explorer`` / ``index_project(explorer=True)`` can auto-append the
``mount_explorer(app)`` call when a FastAPI ``app = FastAPI(...)`` module is
found (see ``autowire.py``).

When playground is enabled (always for ``livespec explorer serve``; opt-in
via ``[explorer] playground = true`` on mounts), also exposes::

    GET  /explorer/api/playground
    POST /explorer/api/call_tool
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from livespec_mcp.config import RepoConfig, load_repo_config
from livespec_mcp.explorer.playground import (
    call_playground_tool,
    playground_enabled,
    playground_mode,
)

_DEFAULT_PREFIX = "/explorer"
_BUNDLE_SUBDIR = Path(".mcp-docs") / "explorer"


def explorer_bundle_dir(workspace: Path | str | None = None) -> Path:
    """Absolute path to ``<workspace>/.mcp-docs/explorer/``."""
    root = Path.cwd() if workspace is None else Path(workspace).resolve()
    return root / _BUNDLE_SUBDIR


def _ensure_bundle(bundle_dir: Path) -> None:
    if not (bundle_dir / "index.html").is_file():
        raise FileNotFoundError(
            f"Spec Explorer bundle missing at {bundle_dir} — "
            "run export_explorer or index_project(explorer=True) first"
        )


def _playground_routes(
    workspace: Path,
    *,
    enabled: bool,
    mode: str,
) -> list[Route]:
    """API routes registered before the SPA catch-all."""

    async def playground_info(_request: Request) -> Response:
        return JSONResponse(
            {
                "enabled": enabled,
                "mode": mode if enabled else None,
                "workspace": str(workspace) if enabled else None,
                "hint": (
                    None
                    if enabled
                    else "Run `livespec explorer serve .` for live Try it, "
                    "or set [explorer] playground = true when mounting."
                ),
            }
        )

    async def call_tool(request: Request) -> Response:
        if not enabled:
            return JSONResponse(
                {
                    "error": "playground disabled",
                    "isError": True,
                    "hint": "livespec explorer serve enables Try it; "
                    "mounts need [explorer] playground = true",
                },
                status_code=403,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "invalid JSON body", "isError": True},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be a JSON object", "isError": True},
                status_code=400,
            )
        name = body.get("name") or body.get("tool")
        arguments = body.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return JSONResponse(
                {"error": "arguments must be a JSON object", "isError": True},
                status_code=400,
            )
        if not isinstance(name, str):
            return JSONResponse(
                {"error": "name must be a string", "isError": True},
                status_code=400,
            )
        status, payload = await call_playground_tool(
            name,
            arguments,
            workspace=workspace,
            mode=mode,  # type: ignore[arg-type]
        )
        return JSONResponse(payload, status_code=status)

    return [
        Route("/api/playground", playground_info, methods=["GET", "HEAD"]),
        Route("/api/call_tool", call_tool, methods=["POST"]),
    ]


def create_explorer_app(
    bundle_dir: Path,
    *,
    workspace: Path | str | None = None,
    playground: bool = False,
    playground_mode_value: str = "readonly",
) -> Starlette:
    """Starlette sub-app: SPA fallback + ``data.json`` (+ optional playground API)."""
    _ensure_bundle(bundle_dir)
    ws = Path(workspace).resolve() if workspace is not None else Path.cwd().resolve()

    async def index(_request: Request) -> Response:
        return FileResponse(bundle_dir / "index.html")

    async def data_json(_request: Request) -> Response:
        return FileResponse(
            bundle_dir / "data.json",
            media_type="application/json",
        )

    async def spa(request: Request) -> Response:
        rel = request.path_params.get("path") or ""
        if rel:
            candidate = (bundle_dir / rel).resolve()
            try:
                candidate.relative_to(bundle_dir.resolve())
            except ValueError:
                return FileResponse(bundle_dir / "index.html")
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(bundle_dir / "index.html")

    routes: list[Route] = [
        *_playground_routes(
            ws, enabled=playground, mode=playground_mode_value
        ),
        Route("/", index, methods=["GET", "HEAD"]),
        Route("/data.json", data_json, methods=["GET", "HEAD"]),
        Route("/{path:path}", spa, methods=["GET", "HEAD"]),
    ]
    return Starlette(routes=routes)


def create_explorer_host_app(
    workspace: Path | str | None = None,
    prefix: str = _DEFAULT_PREFIX,
    *,
    repo_cfg: RepoConfig | None = None,
) -> Starlette:
    """Standalone Starlette app for local preview.

    Serves the bundle at ``prefix`` (default ``/explorer``). Root ``/`` and
    ``/index.html`` redirect there so ``http://127.0.0.1:8765/explorer/`` works.
    Playground MCP bridge is always enabled for host serve.
    """
    ws = Path.cwd() if workspace is None else Path(workspace).resolve()
    bundle_dir = explorer_bundle_dir(ws)
    _ensure_bundle(bundle_dir)
    cfg = repo_cfg if repo_cfg is not None else load_repo_config(ws)
    mode = playground_mode(cfg)
    mount_path = prefix.rstrip("/") or _DEFAULT_PREFIX
    sub = create_explorer_app(
        bundle_dir,
        workspace=ws,
        playground=True,
        playground_mode_value=mode,
    )

    async def redirect_to_explorer(_request: Request) -> Response:
        return RedirectResponse(url=mount_path + "/", status_code=307)

    return Starlette(
        routes=[
            Route("/", redirect_to_explorer, methods=["GET", "HEAD"]),
            Route("/index.html", redirect_to_explorer, methods=["GET", "HEAD"]),
            Route(mount_path, redirect_to_explorer, methods=["GET", "HEAD"]),
            Mount(mount_path, sub),
        ]
    )


def serve_explorer(
    workspace: Path | str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    prefix: str = _DEFAULT_PREFIX,
) -> None:
    """Run a local HTTP server for the Spec Explorer bundle (+ MCP playground)."""
    import uvicorn

    ws = Path.cwd() if workspace is None else Path(workspace).resolve()
    app = create_explorer_host_app(ws, prefix=prefix)
    mount_path = prefix.rstrip("/") or _DEFAULT_PREFIX
    cfg = load_repo_config(ws)
    mode = playground_mode(cfg)
    print(f"Spec Explorer: http://{host}:{port}{mount_path}/", flush=True)
    print(f"MCP playground: enabled (mode={mode})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


def mount_explorer(
    app: Any,
    *,
    workspace: Path | str | None = None,
    prefix: str = _DEFAULT_PREFIX,
) -> str:
    """Mount the Spec Explorer under ``prefix`` (default ``/explorer``).

    Works with FastAPI and plain Starlette apps (``app.mount``). Returns the
    mount prefix actually used. Playground is off unless
    ``[explorer] playground = true`` in ``.livespec.toml``.
    """
    ws = Path.cwd() if workspace is None else Path(workspace).resolve()
    bundle_dir = explorer_bundle_dir(ws)
    _ensure_bundle(bundle_dir)
    cfg = load_repo_config(ws)
    enabled = playground_enabled(cfg, host_serve=False)
    mode = playground_mode(cfg)
    mount_path = prefix.rstrip("/") or _DEFAULT_PREFIX
    sub = create_explorer_app(
        bundle_dir,
        workspace=ws,
        playground=enabled,
        playground_mode_value=mode,
    )

    # Redirect bare prefix -> trailing slash so relative assets resolve.
    async def redirect_to_slash(_request: Request) -> Response:
        return RedirectResponse(url=mount_path + "/", status_code=307)

    if hasattr(app, "mount"):
        app.mount(mount_path, sub)
    else:
        raise TypeError("app must expose .mount() (Starlette or FastAPI)")

    # Prepend a redirect route on the parent app (Starlette matches in order).
    redirect = Route(mount_path, redirect_to_slash, methods=["GET", "HEAD"])
    app.router.routes.insert(0, redirect)
    return mount_path


def try_mount_explorer(
    app: Any,
    *,
    workspace: Path | str | None = None,
    prefix: str = _DEFAULT_PREFIX,
) -> str | None:
    """Like ``mount_explorer`` but returns ``None`` when the bundle is missing."""
    try:
        return mount_explorer(app, workspace=workspace, prefix=prefix)
    except FileNotFoundError:
        return None
