"""Language detection by file extension and tree-sitter parser cache."""

from __future__ import annotations

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
    ".sql": "sql",
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

# Languages read only for their schema declarations, by a pure-Python parser
# (domain/sql_schema.py). Kept out of EXTRACTOR_SUPPORTED because that set
# drives the tree-sitter grammar prefetch, and these never load a grammar.
SCHEMA_ONLY_LANGUAGES: frozenset[str] = frozenset({"sql"})


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
        "sql",
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

    `hint` says how to fix it, shaped by what the failure looked like (a
    certificate the native downloader rejected, no network, no zstd decoder
    for livespec's own fallback fetch).
    """

    def __init__(
        self,
        language: str,
        cause: BaseException | None = None,
        *,
        fallback_error: str | None = None,
    ) -> None:
        super().__init__(f"tree-sitter grammar for {language!r} is unavailable: {cause}")
        self.language = language
        self.cause = cause
        self.fallback_error = fallback_error

    @property
    def error(self) -> str:
        """The underlying failure, one line, with livespec's fallback if it ran."""
        msg = str(self.cause) if self.cause is not None else "unknown error"
        if self.fallback_error:
            msg += f" | livespec's own fetch also failed: {self.fallback_error}"
        return msg

    @property
    def hint(self) -> str:
        return grammar_fix_hint(self.error)


_CERT_MARKERS = (
    "unknownissuer",
    "invalid peer certificate",
    "certificate verify failed",
    "self signed certificate",
    "self-signed certificate",
    "unable to get local issuer",
)
_DOWNLOAD_MARKERS = (
    "download",
    "failed to fetch",
    "manifest",
    "dns",
    "connection",
    "timed out",
)


def _looks_like_cert_error(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _CERT_MARKERS)


def _looks_like_download_error(text: str) -> bool:
    t = text.lower()
    return _looks_like_cert_error(t) or any(k in t for k in _DOWNLOAD_MARKERS)


def grammar_fix_hint(error: str) -> str:
    """How to make a grammar load, for the failure `error` describes."""
    from livespec_mcp.domain.grammar_fetch import CA_BUNDLE_ENV_VARS, FALLBACK_ENV

    cache = grammar_cache_dir() or "~/.cache/tree-sitter-language-pack/v<version>/libs"
    prefill = (
        "Or pre-populate the cache: put the `libtree_sitter_<lang>.so` (.dylib / "
        ".dll) files from the pack's release bundle "
        "(github.com/kreuzberg-dev/tree-sitter-language-pack/releases, "
        "parsers-<platform>.tar.zst for the installed pack version) into "
        f"{cache} — or copy that directory from a machine where `livespec "
        "grammars` worked. XDG_CACHE_HOME moves the cache root. Then re-index; "
        "the skipped files are retried."
    )
    low = error.lower()
    if "zstd decoder" in low:
        return (
            "livespec can fetch the grammar bundle itself but has no zstd decoder to unpack it: "
            "install `zstandard` into livespec's environment (`uv pip install "
            "zstandard`) or the `zstd` CLI, then run `livespec grammars`. " + prefill
        )
    if _looks_like_cert_error(error):
        vars_ = " / ".join(CA_BUNDLE_ENV_VARS)
        return (
            "A TLS certificate was rejected — typically a proxy that intercepts "
            "HTTPS with its own CA. tree-sitter-language-pack's native downloader "
            "trusts only its built-in roots and ignores SSL_CERT_FILE / "
            "REQUESTS_CA_BUNDLE, so livespec retries the download with Python's "
            f"TLS: point one of {vars_} at your proxy's CA bundle (PEM) in the "
            "environment of the livespec process (for an MCP host, its `env` "
            f"block; and not {FALLBACK_ENV}=0), then run `livespec grammars` or "
            "re-index. " + prefill
        )
    return (
        "The grammar could not be downloaded (tree-sitter-language-pack fetches "
        "grammars from GitHub on first use). Run `livespec grammars` once with "
        "network access (HTTPS_PROXY is honoured), then re-index. " + prefill
    )


_PARSERS: dict[str, Any] = {}
# language -> the error from this process's last failed load. Memoised so an
# index over 500 TypeScript files makes ONE doomed network attempt, not 500
# (each one a manifest fetch with its own timeout). Cleared by
# `reset_grammar_failures()` — the indexer does that at the start of every run,
# so `livespec grammars` in another terminal is picked up by the next index.
_FAILED: dict[str, GrammarUnavailableError] = {}
_FALLBACK_STATE: dict[str, Any] = {"tried": False, "error": None}


def reset_grammar_failures() -> None:
    _FAILED.clear()
    _FALLBACK_STATE["tried"] = False
    _FALLBACK_STATE["error"] = None


def _clear_all() -> None:
    _PARSERS.clear()
    reset_grammar_failures()


def _python_fallback(language: str) -> str | None:
    """Try livespec's own fetch once per run. Returns an error string, or None
    when it installed `language` (the caller then retries the pack)."""
    from livespec_mcp.domain import grammar_fetch

    if not grammar_fetch.fallback_enabled():
        return f"disabled by {grammar_fetch.FALLBACK_ENV}=0"
    if _FALLBACK_STATE["tried"]:
        return _FALLBACK_STATE["error"] or f"{language!r} is not in the downloaded bundle"
    _FALLBACK_STATE["tried"] = True
    cache = grammar_cache_dir()
    if not cache:
        err = "tree-sitter-language-pack did not report its cache_dir"
    else:
        try:
            result = grammar_fetch.fetch_grammars(sorted(EXTRACTOR_SUPPORTED | {language}), cache)
        except Exception as e:
            err = str(e)
        else:
            if language in result["installed"]:
                return None
            err = f"{language!r} is not in the downloaded bundle"
    _FALLBACK_STATE["error"] = err
    return err


def get_parser(language: str):
    """Return a tree-sitter Parser configured for the given language id.

    Uses tree-sitter-language-pack (Goldziher). Note that 1.x fetches each
    grammar on first use rather than bundling them in the wheel, so this can
    fail on a machine that has never had network access for it — see
    `GrammarUnavailableError` and `livespec grammars` for the prefetch.

    When the pack's own (native) download fails, livespec retries ONCE per
    index run with Python's TLS stack (`domain/grammar_fetch.py`), which
    honours SSL_CERT_FILE / REQUESTS_CA_BUNDLE — the fix for TLS-intercepting
    proxies, whose CA the native client never trusts.

    Raises `GrammarUnavailableError` for any failure to produce a parser, so a
    missing grammar is never mistaken for an unparseable file. Successful
    parsers are cached; failures are memoised until `reset_grammar_failures()`.
    """
    cached = _PARSERS.get(language)
    if cached is not None:
        return cached
    failed = _FAILED.get(language)
    if failed is not None:
        raise failed
    try:
        from tree_sitter_language_pack import get_parser as _get_parser
    except ImportError as e:  # pragma: no cover - dependency is required
        raise RuntimeError(
            "tree-sitter-language-pack not installed. Run: pip install tree-sitter-language-pack"
        ) from e
    try:
        parser = _get_parser(language)
    except Exception as e:
        fallback_error: str | None = None
        if _looks_like_download_error(str(e)):
            fallback_error = _python_fallback(language)
            if fallback_error is None:
                try:
                    parser = _get_parser(language)
                except Exception as e2:
                    fallback_error = f"installed, but the pack still could not load it: {e2}"
                else:
                    _PARSERS[language] = parser
                    return parser
        err = GrammarUnavailableError(language, e, fallback_error=fallback_error)
        _FAILED[language] = err
        raise err from e
    _PARSERS[language] = parser
    return parser


# Kept for callers and tests written against the old `lru_cache` wrapper.
get_parser.cache_clear = _clear_all  # type: ignore[attr-defined]


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
    via_python: list[str] = []
    fallback_error: str | None = None
    if failed and any(_looks_like_download_error(v) for v in failed.values()):
        # The native client failed (typically: it does not trust a proxy's CA).
        # Retry everything that failed through Python's TLS stack.
        from livespec_mcp.domain import grammar_fetch

        cache = grammar_cache_dir()
        if not grammar_fetch.fallback_enabled():
            fallback_error = f"disabled by {grammar_fetch.FALLBACK_ENV}=0"
        elif not cache:
            fallback_error = "tree-sitter-language-pack did not report its cache_dir"
        else:
            try:
                res = grammar_fetch.fetch_grammars(sorted(failed), cache)
                via_python = res["installed"]
            except Exception as e:
                fallback_error = str(e)
        for lang in via_python:
            failed.pop(lang, None)
            ok.append(lang)
    reset_grammar_failures()
    out: dict[str, Any] = {
        "requested": wanted,
        "downloaded": sorted(ok),
        "failed": failed,
        "cache_dir": grammar_cache_dir(),
        "cached_now": downloaded_grammars(),
    }
    if via_python:
        out["downloaded_via_python"] = via_python
    if failed:
        if fallback_error:
            out["fallback_error"] = fallback_error
        first = next(iter(failed.values()))
        out["hint"] = grammar_fix_hint(first + (f" | {fallback_error}" if fallback_error else ""))
    return out
