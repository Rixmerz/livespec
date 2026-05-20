"""Workspace configuration.

Project root comes from the ``workspace`` argument on each MCP tool call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    state_dir: Path
    db_path: Path
    docs_dir: Path
    models_dir: Path

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    def safe_path(self, rel: str | Path) -> Path:
        """Resolve a path inside workspace; raise if it escapes."""
        p = (self.workspace / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        if not p.is_relative_to(self.workspace):
            raise ValueError(f"Path {p} escapes workspace {self.workspace}")
        return p
