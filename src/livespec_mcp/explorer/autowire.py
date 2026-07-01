"""Auto-append ``mount_explorer(app)`` to FastAPI entry modules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Bounded patterns (ReDoS-safe): module-level `app = FastAPI(`.
_FASTAPI_APP = re.compile(
    r"^(\s*)(?P<var>[A-Za-z_][A-Za-z0-9_]{0,63})\s*=\s*FastAPI\s*\(",
    re.MULTILINE,
)
_MOUNT_MARKER = "livespec_mcp.explorer"
_SKIP_DIRS = frozenset(
    {".venv", "venv", "node_modules", ".git", "dist", "build", "__pycache__"}
)
_ENTRY_NAMES = ("main.py", "app.py")


@dataclass(frozen=True)
class AutowireResult:
    wired: bool
    file: str | None = None
    app_var: str | None = None
    reason: str | None = None


def _should_skip(path: Path) -> bool:
    return any(part in _SKIP_DIRS for part in path.parts)


def find_fastapi_entrypoints(workspace: Path) -> list[tuple[Path, str]]:
    """Return ``(file, app_variable_name)`` for ``var = FastAPI(...)`` modules."""
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for name in _ENTRY_NAMES:
        for path in workspace.glob(f"**/{name}"):
            if _should_skip(path) or path in seen:
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "FastAPI" not in text:
                continue
            match = _FASTAPI_APP.search(text)
            if match:
                found.append((path, match.group("var")))
    return found


def _already_wired(text: str) -> bool:
    return _MOUNT_MARKER in text or "mount_explorer(" in text


def wire_explorer_mount(
    path: Path,
    app_var: str,
    *,
    mount_path: str = "/explorer",
) -> AutowireResult:
    """Append a ``mount_explorer`` block to ``path`` if not already present."""
    rel = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return AutowireResult(wired=False, file=rel, reason=str(e))

    if _already_wired(text):
        return AutowireResult(
            wired=False, file=rel, app_var=app_var, reason="already_wired"
        )

    block = (
        "\n\n"
        "# livespec: RF Explorer at "
        f"{mount_path} (auto-wired by export_explorer / index_project)\n"
        "try:\n"
        "    from livespec_mcp.explorer import mount_explorer\n"
        f'    mount_explorer({app_var}, prefix="{mount_path}")\n'
        "except ImportError:\n"
        "    pass  # livespec-mcp not installed in this runtime\n"
        "except FileNotFoundError:\n"
        "    pass  # run export_explorer to generate .mcp-docs/explorer/\n"
    )
    path.write_text(text + block, encoding="utf-8")
    return AutowireResult(wired=True, file=rel, app_var=app_var)


def autowire_fastapi_explorer(
    workspace: Path,
    *,
    auto_mount: bool,
    mount_path: str = "/explorer",
) -> AutowireResult:
    """If ``auto_mount``, wire the first FastAPI entry module found."""
    if not auto_mount:
        return AutowireResult(wired=False, reason="auto_mount_disabled")
    entries = find_fastapi_entrypoints(workspace)
    if not entries:
        return AutowireResult(wired=False, reason="no_fastapi_entrypoint")
    path, app_var = entries[0]
    return wire_explorer_mount(path, app_var, mount_path=mount_path)
