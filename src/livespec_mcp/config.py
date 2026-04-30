"""Workspace configuration.

DOCS_BRAIN_WORKSPACE env var sets the root of the project being documented.
Defaults to current working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    state_dir: Path
    db_path: Path
    docs_dir: Path
    models_dir: Path

    @classmethod
    def load(cls) -> Settings:
        ws_raw = os.environ.get("LIVESPEC_WORKSPACE") or os.environ.get(
            "DOCS_BRAIN_WORKSPACE", os.getcwd()
        )
        workspace = Path(ws_raw).expanduser().resolve()
        state_dir = workspace / ".mcp-docs"
        return cls(
            workspace=workspace,
            state_dir=state_dir,
            db_path=state_dir / "docs.db",
            docs_dir=state_dir / "docs",
            models_dir=state_dir / "models",
        )

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    def safe_path(self, rel: str | Path) -> Path:
        """Resolve a path inside workspace; raise if it escapes."""
        p = (self.workspace / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        if not p.is_relative_to(self.workspace):
            raise ValueError(f"Path {p} escapes workspace {self.workspace}")
        return p
