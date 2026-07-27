"""Workspace configuration.

Project root comes from the ``workspace`` argument on each MCP tool call.

v0.14: per-repo overrides live in ``.livespec.toml`` at the workspace root
(`[index]` table: ``ignore``, ``languages``, ``max_file_bytes``). Loaded
fresh on every ``index_project`` call so edits apply without restarts.
"""

from __future__ import annotations

import json
import re
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
    # True when db_path is a shared group DB (`[workspace] group_db`) holding
    # several repo roots — spec-symbol resolution then spans the whole group.
    grouped: bool = False

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
    # [explorer] — Spec Explorer served at /explorer on FastAPI apps
    explorer_auto_mount: bool = True
    explorer_mount_path: str = "/explorer"
    # [agent] — per-workspace agent instrumentation (off by default)
    agent_log_calls: bool = False
    # [specs] — post-index markdown sync + optional links seed
    specs_sync_from: tuple[str, ...] = ()
    specs_links_seed: str | None = None
    # [specs] openspec_dir — an OpenSpec tree re-synced (specs + changes) after
    # each index_project. Relative paths resolve against the workspace root.
    specs_openspec_dir: str | None = None
    # [workspace] — cross-project grouping: a shared DB path lets several repo
    # roots live in one database (each its own project_id) so a Spec can link
    # symbols across repos. Relative paths resolve against the workspace root.
    group_db: str | None = None

    def as_payload(self) -> dict:
        return {
            "ignore": list(self.ignore),
            "languages": sorted(self.languages) if self.languages is not None else None,
            "max_file_bytes": self.max_file_bytes,
            "explorer": {
                "auto_mount": self.explorer_auto_mount,
                "mount_path": self.explorer_mount_path,
            },
            "agent": {
                "log_calls": self.agent_log_calls,
            },
            "specs": {
                "sync_from": list(self.specs_sync_from),
                "links_seed": self.specs_links_seed,
                "openspec_dir": self.specs_openspec_dir,
            },
            "workspace": {
                "group_db": self.group_db,
            },
        }


def _config_error(msg: str) -> ValueError:
    return ValueError(f"Invalid {REPO_CONFIG_FILENAME}: {msg}")


# ---------- Ecosystem build-output exclusions (deno.json / tsconfig.json) ----------
#
# Bug #12: the indexer only honored .gitignore + .livespec.toml, so a project
# that declares its build output via `deno.json`'s `exclude` (Fresh's
# `_fresh/`) or `tsconfig.json`'s `exclude` (`dist/`, etc.) — rather than
# .gitignore — got its minified bundles indexed as if they were source. This
# folds those declarations into `RepoConfig.ignore` so `indexer.py` needs no
# changes at all: it already routes `cfg.ignore` through a GitIgnoreSpec that
# outranks .gitignore.


def _strip_jsonc_comments(text: str) -> str:
    """Best-effort strip of ``//`` and ``/* */`` comments outside string
    literals, so ``json.loads`` can parse a ``.jsonc``-flavored file
    (deno.json/tsconfig.json both allow comments + trailing commas).

    Dependency-free by design (ponytail: stdlib only). Not a full JSONC
    parser — good enough for the config shapes these tools actually emit.
    """
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """``,}``/``,]`` (with optional whitespace between) -> ``}``/``]``."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _load_jsonc(path: Path) -> dict | None:
    """Parse a JSON/JSONC file; ``None`` on any error (missing, malformed,
    not an object) — a malformed config file must never break indexing."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(_strip_trailing_commas(_strip_jsonc_comments(raw)))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _normalize_exclude_pattern(pattern: str) -> str | None:
    """Translate one deno.json/tsconfig.json ``exclude`` entry to gitignore
    syntax. Both tools already use gitignore-compatible globs for the common
    cases (bare name, ``**/x/*``, trailing ``/``); the one real gap is a
    leading ``./`` that gitignore patterns don't use."""
    if not isinstance(pattern, str):
        return None
    p = pattern.strip()
    if not p:
        return None
    if p.startswith("./"):
        p = p[2:]
    return p or None


def _ecosystem_ignore_patterns(workspace: Path) -> tuple[str, ...]:
    """``exclude`` arrays from ``deno.json``/``deno.jsonc`` and
    ``tsconfig.json`` at the workspace root, translated to gitignore syntax.

    ``include`` allowlists (tsconfig's "only these paths" mode) are NOT
    honored — that inverts the walk from denylist to allowlist, a materially
    different code path this fix doesn't need: every project this bug was
    found against declares its build output via `exclude`.
    ponytail: revisit only if a real project needs include-only semantics.
    """
    patterns: list[str] = []
    for name in ("deno.json", "deno.jsonc"):
        data = _load_jsonc(workspace / name)
        if data is None:
            continue
        exclude = data.get("exclude")
        if isinstance(exclude, list):
            patterns.extend(
                p for p in (_normalize_exclude_pattern(x) for x in exclude) if p
            )
        break  # deno.json wins over deno.jsonc if both exist (Deno's own rule)

    ts_data = _load_jsonc(workspace / "tsconfig.json")
    if ts_data is not None:
        exclude = ts_data.get("exclude")
        if isinstance(exclude, list):
            patterns.extend(
                p for p in (_normalize_exclude_pattern(x) for x in exclude) if p
            )

    # Dedup, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def load_repo_config(workspace: Path) -> RepoConfig:
    """Read ``<workspace>/.livespec.toml``; missing file → defaults.

        Schema::

        [index]
        ignore = ["assets/", "*.min.js"]   # gitignore syntax, beats .gitignore
        languages = ["python", "typescript"]  # extractor labels; empty/absent = all
        max_file_bytes = 2000000

        [explorer]
        auto_mount = true          # append mount_explorer(app) to FastAPI main (default true)
        mount_path = "/explorer"   # URL prefix served by mount_explorer()

        [specs]
        sync_from = ["docs/REQUISITOS_FUNCIONALES.md"]  # re-import on each index_project
        links_seed = "docs/requirements/spec-links.json"  # optional bulk_link replay

        [agent]
        log_calls = false          # append tool-call lines to .mcp-docs/agent_log.jsonl

    Malformed content raises ``ValueError`` with an actionable message —
    silently ignoring a typoed config would be worse than failing the call.

    v0.24: ``deno.json``/``deno.jsonc``/``tsconfig.json`` ``exclude`` arrays
    at the workspace root are folded into ``.ignore`` too (see
    ``_ecosystem_ignore_patterns``) — a build-output declaration shouldn't
    need to be repeated in ``.livespec.toml`` to keep bundles out of the
    index. They're applied FIRST so an explicit ``[index].ignore`` entry in
    ``.livespec.toml`` (including a ``!re-include``) still wins, matching
    that file's documented precedence over every other ignore source.
    """
    eco_ignore = _ecosystem_ignore_patterns(workspace)
    path = workspace / REPO_CONFIG_FILENAME
    if not path.is_file():
        return RepoConfig(ignore=eco_ignore) if eco_ignore else RepoConfig()
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
        from livespec_mcp.domain.languages import EXTRACTOR_SUPPORTED

        bad = sorted(set(languages) - EXTRACTOR_SUPPORTED)
        if bad:
            raise _config_error(
                f"unsupported languages {bad} — no extractor yet "
                f"(valid: {sorted(EXTRACTOR_SUPPORTED)})"
            )

    max_file_bytes = index.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or max_file_bytes <= 0:
        raise _config_error("[index].max_file_bytes must be a positive integer")

    explorer = data.get("explorer", {})
    if not isinstance(explorer, dict):
        raise _config_error("[explorer] must be a table")
    unknown_explorer = set(explorer) - {"auto_mount", "mount_path"}
    if unknown_explorer:
        raise _config_error(
            f"unknown [explorer] keys: {sorted(unknown_explorer)} "
            "(valid: auto_mount, mount_path)"
        )
    explorer_auto_mount = explorer.get("auto_mount", True)
    if not isinstance(explorer_auto_mount, bool):
        raise _config_error("[explorer].auto_mount must be a boolean")
    explorer_mount_path = explorer.get("mount_path", "/explorer")
    if not isinstance(explorer_mount_path, str) or not explorer_mount_path.startswith("/"):
        raise _config_error("[explorer].mount_path must be a path starting with /")

    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        raise _config_error("[agent] must be a table")
    unknown_agent = set(agent) - {"log_calls"}
    if unknown_agent:
        raise _config_error(
            f"unknown [agent] keys: {sorted(unknown_agent)} (valid: log_calls)"
        )
    agent_log_calls = agent.get("log_calls", False)
    if not isinstance(agent_log_calls, bool):
        raise _config_error("[agent].log_calls must be a boolean")

    specs = data.get("specs", {})
    if not isinstance(specs, dict):
        raise _config_error("[specs] must be a table")
    unknown_specs = set(specs) - {"sync_from", "links_seed", "openspec_dir"}
    if unknown_specs:
        raise _config_error(
            f"unknown [specs] keys: {sorted(unknown_specs)} "
            "(valid: sync_from, links_seed, openspec_dir)"
        )
    sync_from = specs.get("sync_from", [])
    if not isinstance(sync_from, list) or not all(isinstance(x, str) for x in sync_from):
        raise _config_error("[specs].sync_from must be a list of strings")
    links_seed = specs.get("links_seed")
    if links_seed is not None and not isinstance(links_seed, str):
        raise _config_error("[specs].links_seed must be a string path")
    openspec_dir = specs.get("openspec_dir")
    if openspec_dir is not None and (
        not isinstance(openspec_dir, str) or not openspec_dir.strip()
    ):
        raise _config_error("[specs].openspec_dir must be a non-empty string path")

    workspace_tbl = data.get("workspace", {})
    if not isinstance(workspace_tbl, dict):
        raise _config_error("[workspace] must be a table")
    unknown_ws = set(workspace_tbl) - {"group_db"}
    if unknown_ws:
        raise _config_error(
            f"unknown [workspace] keys: {sorted(unknown_ws)} (valid: group_db)"
        )
    group_db = workspace_tbl.get("group_db")
    if group_db is not None and (not isinstance(group_db, str) or not group_db.strip()):
        raise _config_error("[workspace].group_db must be a non-empty string path")

    return RepoConfig(
        ignore=eco_ignore + tuple(ignore),
        languages=frozenset(languages) if languages else None,
        max_file_bytes=max_file_bytes,
        explorer_auto_mount=explorer_auto_mount,
        explorer_mount_path=explorer_mount_path,
        agent_log_calls=agent_log_calls,
        specs_sync_from=tuple(sync_from),
        specs_links_seed=links_seed,
        specs_openspec_dir=openspec_dir,
        group_db=group_db,
    )
