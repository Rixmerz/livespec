"""Workspace configuration.

Project root comes from the ``workspace`` argument on each MCP tool call.

v0.14: per-repo overrides live in ``.livespec.toml`` at the workspace root
(`[index]` table: ``ignore``, ``languages``, ``max_file_bytes``). Loaded
fresh on every ``index_project`` call so edits apply without restarts.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — py3.10 fallback
    import tomli as tomllib


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


# ---------- Per-repo config (.livespec.toml) ----------

REPO_CONFIG_FILENAME = ".livespec.toml"
DEFAULT_MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class RepoConfig:
    """Parsed ``.livespec.toml``. All fields optional; defaults preserve
    pre-v0.14 behaviour exactly (an absent file yields ``RepoConfig()``)."""

    ignore: tuple[str, ...] = ()
    languages: frozenset[str] | None = None
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    def as_payload(self) -> dict:
        return {
            "ignore": list(self.ignore),
            "languages": sorted(self.languages) if self.languages is not None else None,
            "max_file_bytes": self.max_file_bytes,
        }


def _config_error(msg: str) -> ValueError:
    return ValueError(f"Invalid {REPO_CONFIG_FILENAME}: {msg}")


def load_repo_config(workspace: Path) -> RepoConfig:
    """Read ``<workspace>/.livespec.toml``; missing file → defaults.

    Schema::

        [index]
        ignore = ["assets/", "*.min.js"]   # gitignore syntax, beats .gitignore
        languages = ["python", "typescript"]  # extractor labels; empty/absent = all
        max_file_bytes = 2000000

    Malformed content raises ``ValueError`` with an actionable message —
    silently ignoring a typoed config would be worse than failing the call.
    """
    path = workspace / REPO_CONFIG_FILENAME
    if not path.is_file():
        return RepoConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise _config_error(str(e)) from e

    index = data.get("index", {})
    if not isinstance(index, dict):
        raise _config_error("[index] must be a table")
    unknown = set(index) - {"ignore", "languages", "max_file_bytes"}
    if unknown:
        raise _config_error(
            f"unknown [index] keys: {sorted(unknown)} "
            "(valid: ignore, languages, max_file_bytes)"
        )

    ignore = index.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(x, str) for x in ignore):
        raise _config_error("[index].ignore must be a list of strings")

    languages = index.get("languages")
    if languages is not None:
        if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
            raise _config_error("[index].languages must be a list of strings")
        from livespec_mcp.domain.languages import EXT_LANGUAGE

        valid = set(EXT_LANGUAGE.values())
        bad = sorted(set(languages) - valid)
        if bad:
            raise _config_error(
                f"unknown languages {bad} (valid: {sorted(valid)})"
            )

    max_file_bytes = index.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or max_file_bytes <= 0:
        raise _config_error("[index].max_file_bytes must be a positive integer")

    return RepoConfig(
        ignore=tuple(ignore),
        languages=frozenset(languages) if languages else None,
        max_file_bytes=max_file_bytes,
    )
