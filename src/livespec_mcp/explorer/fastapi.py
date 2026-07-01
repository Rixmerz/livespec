"""FastAPI / Starlette helpers to mount RF Explorer without editing ``main.py`` by hand.

Three integration patterns (pick one):

**1. One-liner after ``app = FastAPI()``** — smallest explicit hook::

    from livespec_mcp.explorer.fastapi import enable_explorer

    app = FastAPI()
    enable_explorer(app)  # prefix from .livespec.toml [explorer].mount_path

**2. Lifespan** — mount during startup (FastAPI 0.93+ / Starlette lifespan)::

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from livespec_mcp.explorer.fastapi import explorer_lifespan

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with explorer_lifespan(app):
            yield

    app = FastAPI(lifespan=lifespan)

**3. ASGI middleware wrapper** — mount once when the stack is built (no ``main.py`` body edit
beyond wrapping the app)::

    from fastapi import FastAPI
    from livespec_mcp.explorer.fastapi import LivespecExplorerMiddleware

    app = LivespecExplorerMiddleware(FastAPI())

``enable_explorer`` and ``explorer_lifespan`` call :func:`mount_explorer` (or
:func:`try_mount_explorer` in the lifespan case so a missing bundle does not crash startup).
When ``prefix`` is omitted, it is read from ``[explorer].mount_path`` in ``.livespec.toml``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from livespec_mcp.config import load_repo_config
from livespec_mcp.explorer.asgi import mount_explorer, try_mount_explorer


def _resolve_prefix(workspace: Path | str | None, prefix: str | None) -> str:
    if prefix is not None:
        return prefix
    root = Path.cwd() if workspace is None else Path(workspace).resolve()
    return load_repo_config(root).explorer_mount_path


def enable_explorer(
    app: Any,
    *,
    workspace: Path | str | None = None,
    prefix: str | None = None,
) -> str:
    """Mount RF Explorer on ``app``. Returns the mount prefix used.

    Typical manual hook — one line after ``FastAPI()`` construction::

        enable_explorer(app, workspace="/path/to/repo")
    """
    resolved = _resolve_prefix(workspace, prefix)
    return mount_explorer(app, workspace=workspace, prefix=resolved)


@asynccontextmanager
async def explorer_lifespan(
    app: Any,
    *,
    workspace: Path | str | None = None,
    prefix: str | None = None,
) -> AsyncIterator[None]:
    """Async context manager for FastAPI ``lifespan=`` — mounts on enter, no-op on exit."""
    resolved = _resolve_prefix(workspace, prefix)
    try_mount_explorer(app, workspace=workspace, prefix=resolved)
    yield


class LivespecExplorerMiddleware:
    """ASGI middleware that mounts the explorer sub-app once at construction time.

    Wrap the FastAPI/Starlette instance **after** routes are registered::

        app = LivespecExplorerMiddleware(FastAPI(), workspace=".")
    """

    def __init__(
        self,
        app: Any,
        *,
        workspace: Path | str | None = None,
        prefix: str | None = None,
    ) -> None:
        resolved = _resolve_prefix(workspace, prefix)
        try_mount_explorer(app, workspace=workspace, prefix=resolved)
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)
