"""Language detection by file extension and tree-sitter parser cache."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

# Map extension -> (language_id used by tree-sitter-language-pack, label)
EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
}


def detect_language(path: Path) -> str | None:
    return EXT_LANGUAGE.get(path.suffix.lower())


# Languages with a real extractor behind them. EXT_LANGUAGE maps more
# extensions (c, cpp, c_sharp, kotlin, swift, scala) but those have no
# symbol extraction yet — indexing them would parse for zero symbols.
# v0.14: such files are skipped and reported as `languages_unsupported`
# in the index_project payload instead of silently producing nothing.
EXTRACTOR_SUPPORTED: frozenset[str] = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "tsx",
        "go",
        "java",
        "rust",
        "ruby",
        "php",
    }
)


# Languages whose extractor populates `symbol.docstring` so the @spec:
# annotation matcher can find tags. Used by audit_coverage to separate
# "actually un-covered" from "extractor can't see annotations here yet".
ANNOTATION_SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "tsx",
        "java",
    }
)


class GrammarUnavailableError(RuntimeError):
    """A tree-sitter grammar could not be loaded for this language.

    Distinct from a syntax error in the file: the file was never parsed at
    all. It exists because `tree-sitter-language-pack` 1.x stopped shipping
    every grammar inside the wheel and now DOWNLOADS each one from GitHub on
    first use, caching it under `cache_dir()`. On a machine with no network,
    behind a proxy whose CA the pack's native downloader does not trust, or in
    an air-gapped build, every non-Python language silently produces zero
    symbols — Python is unaffected because `_py_extract` uses the stdlib `ast`.

    Swallowing that was the worst possible failure mode. The file got persisted
    with no symbols AND with its content hash advanced, so the next index run
    considered it unchanged and never retried: one offline index quietly turned
    a polyglot repo into a Python-only one, and `find_dead_code` then reported
    every TypeScript symbol in it as dead. Callers must be able to tell "this
    file has no symbols" from "this file was never read".
    """

    def __init__(self, language: str, cause: BaseException | None = None) -> None:
        super().__init__(f"tree-sitter grammar for {language!r} is unavailable: {cause}")
        self.language = language
        self.cause = cause


@lru_cache(maxsize=64)
def get_parser(language: str):
    """Return a tree-sitter Parser configured for the given language id.

    Uses tree-sitter-language-pack (Goldziher). Note that 1.x fetches each
    grammar on first use rather than bundling them in the wheel, so this can
    fail on a machine that has never had network access for it — see
    `GrammarUnavailableError` and `livespec grammars` for the prefetch.

    Raises `GrammarUnavailableError` for any failure to produce a parser, so a
    missing grammar is never mistaken for an unparseable file.
    """
    try:
        from tree_sitter_language_pack import get_parser as _get_parser
    except ImportError as e:  # pragma: no cover - dependency is required
        raise RuntimeError(
            "tree-sitter-language-pack not installed. Run: pip install tree-sitter-language-pack"
        ) from e
    try:
        return _get_parser(language)
    except Exception as e:
        raise GrammarUnavailableError(language, e) from e


def grammar_cache_dir() -> str | None:
    """Where the language pack caches downloaded grammars, if it will say."""
    try:
        from tree_sitter_language_pack import cache_dir

        return str(cache_dir())
    except Exception:
        return None


def downloaded_grammars() -> list[str]:
    """Grammars already cached on this machine (empty if the pack won't say)."""
    try:
        from tree_sitter_language_pack import downloaded_languages

        return sorted(downloaded_languages())
    except Exception:
        return []


def prefetch_grammars(languages: list[str] | None = None) -> dict[str, Any]:
    """Download the grammars livespec needs, so indexing works offline later.

    Defaults to `EXTRACTOR_SUPPORTED` — the languages livespec can actually
    extract — rather than the pack's full set of several hundred, which is a
    large download for grammars nothing here would ever load.
    """
    wanted = sorted(languages or EXTRACTOR_SUPPORTED)
    try:
        from tree_sitter_language_pack import download
    except ImportError as e:  # pragma: no cover - dependency is required
        raise RuntimeError("tree-sitter-language-pack not installed") from e

    ok: list[str] = []
    failed: dict[str, str] = {}
    for lang in wanted:
        try:
            download([lang])
            ok.append(lang)
        except Exception as e:
            failed[lang] = str(e)
    return {
        "requested": wanted,
        "downloaded": ok,
        "failed": failed,
        "cache_dir": grammar_cache_dir(),
        "cached_now": downloaded_grammars(),
    }
