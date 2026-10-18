"""A parser that cannot be loaded is reported with its error and a fix.

Found on a Deno Fresh repo behind a TLS-intercepting proxy:
`tree-sitter-language-pack`'s native downloader rejected the proxy's CA
(`invalid peer certificate: UnknownIssuer`), and `index_project` came back
with `languages: {typescript: 136, tsx: 91}` next to `symbols_total: 0` —
which reads as a clean index of an empty repo. Every Spec feature after it
silently did nothing.

These pin: `parser_errors: [{language, files, error, hint}]` and a top-level
`warning` when a language had files but 0 symbols; one failed language never
stops the others; and livespec's own Python-side fetch (which honours
SSL_CERT_FILE / REQUESTS_CA_BUNDLE) is tried once and then used.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain import extractors, grammar_fetch, languages
from livespec_mcp.domain.languages import GrammarUnavailableError, get_parser
from livespec_mcp.server import mcp

from .conftest import requires_grammar

CERT_ERROR = (
    "Download error: Failed to fetch manifest from https://example.invalid/parsers.json: "
    "io: invalid peer certificate: UnknownIssuer"
)


def _fail_for(*failing: str):
    real = extractors.get_parser

    def _get(language: str):
        if language in failing:
            raise GrammarUnavailableError(language, RuntimeError(CERT_ERROR))
        return real(language)

    return _get


@pytest.mark.asyncio
async def test_parser_errors_and_warning_are_shaped(workspace: Path):
    (workspace / "app.py").write_text("def py_fn():\n    return 1\n")
    (workspace / "a.ts").write_text("export const a = () => 1;\n")
    (workspace / "b.ts").write_text("export function b() { return 2 }\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(extractors, "get_parser", _fail_for("typescript"))
        async with Client(mcp) as c:
            payload = (await c.call_tool("index_project", {})).data

    assert payload["languages"]["typescript"] == 2
    errs = payload["parser_errors"]
    assert [e["language"] for e in errs] == ["typescript"]
    entry = errs[0]
    assert set(entry) == {"language", "files", "error", "hint"}
    assert entry["files"] == 2
    assert "UnknownIssuer" in entry["error"]
    # A certificate failure gets the CA-bundle fix, not "go online".
    assert "SSL_CERT_FILE" in entry["hint"]
    assert "REQUESTS_CA_BUNDLE" in entry["hint"]
    assert "libtree_sitter_" in entry["hint"]  # the pre-populate route
    warning = payload["warning"]
    assert "typescript (2 file(s))" in warning
    assert "parser_errors" in warning
    # Python indexed anyway: one language failing is never fatal.
    assert payload["symbols_total"] >= 1


@pytest.mark.asyncio
async def test_a_mixed_repo_still_indexes_the_other_tree_sitter_language(workspace: Path):
    requires_grammar("go")
    (workspace / "a.ts").write_text("export function tsFn() { return 1 }\n")
    (workspace / "main.go").write_text("package main\n\nfunc GoFn() int { return 1 }\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(extractors, "get_parser", _fail_for("typescript"))
        async with Client(mcp) as c:
            payload = (await c.call_tool("index_project", {})).data

    assert payload["languages_failed"] == {"typescript": 1}
    assert [e["language"] for e in payload["parser_errors"]] == ["typescript"]
    assert "go" not in payload["warning"]
    assert "typescript" in payload["warning"]
    async with Client(mcp) as c:
        found = (await c.call_tool("find_symbol", {"query": "GoFn"})).data
    assert found, "the Go file must index although TypeScript failed"


@pytest.mark.asyncio
async def test_a_readable_repo_has_no_parser_errors_or_warning(workspace: Path):
    requires_grammar("typescript")
    (workspace / "a.ts").write_text("export function tsFn() { return 1 }\n")
    async with Client(mcp) as c:
        payload = (await c.call_tool("index_project", {})).data
    assert "parser_errors" not in payload
    assert "warning" not in payload


def test_cli_index_json_carries_the_same_fields(workspace: Path, monkeypatch, capsys):
    from livespec_mcp import cli

    (workspace / "a.ts").write_text("export function tsFn() { return 1 }\n")
    monkeypatch.setattr(extractors, "get_parser", _fail_for("typescript"))
    assert cli.main(["index", str(workspace)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parser_errors"][0]["language"] == "typescript"
    assert "warning" in payload


# ---------- get_parser: the Python-side fetch and the failure memo ----------


@pytest.fixture
def clean_parser_cache():
    get_parser.cache_clear()
    yield
    get_parser.cache_clear()


def test_native_cert_failure_falls_back_to_python_fetch_once(monkeypatch, clean_parser_cache):
    import tree_sitter_language_pack

    installed: set[str] = set()
    native_calls: list[str] = []
    fetch_calls: list[list[str]] = []

    def native(name):
        native_calls.append(name)
        if name not in installed:
            raise RuntimeError(CERT_ERROR)
        return f"parser:{name}"

    def fake_fetch(langs, cache_dir):
        fetch_calls.append(list(langs))
        installed.update(langs)
        return {"installed": sorted(langs)}

    monkeypatch.setenv(grammar_fetch.FALLBACK_ENV, "1")
    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", native)
    monkeypatch.setattr(grammar_fetch, "fetch_grammars", fake_fetch)
    monkeypatch.setattr(languages, "grammar_cache_dir", lambda: "/nonexistent/cache")

    assert get_parser("typescript") == "parser:typescript"
    assert get_parser("go") == "parser:go"  # fetched in the same bundle pass
    assert len(fetch_calls) == 1
    assert "typescript" in fetch_calls[0] and "go" in fetch_calls[0]


def test_failure_is_memoised_within_a_run_and_retried_after_reset(monkeypatch, clean_parser_cache):
    """500 TypeScript files must cost one doomed attempt, not 500 — and the
    next index run (which resets) must try again."""
    import tree_sitter_language_pack

    calls: list[str] = []

    def native(name):
        calls.append(name)
        raise RuntimeError(CERT_ERROR)

    monkeypatch.setenv(grammar_fetch.FALLBACK_ENV, "0")
    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", native)
    for _ in range(5):
        with pytest.raises(GrammarUnavailableError) as exc:
            get_parser("typescript")
    assert calls == ["typescript"]
    assert "disabled by LIVESPEC_GRAMMAR_FALLBACK=0" in exc.value.error
    languages.reset_grammar_failures()
    with pytest.raises(GrammarUnavailableError):
        get_parser("typescript")
    assert calls == ["typescript", "typescript"]


def test_fallback_failure_is_part_of_the_error_and_hint(monkeypatch, clean_parser_cache):
    import tree_sitter_language_pack

    def native(name):
        raise RuntimeError(CERT_ERROR)

    def fake_fetch(langs, cache_dir):
        raise grammar_fetch.GrammarFetchError(grammar_fetch._NO_ZSTD)

    monkeypatch.setenv(grammar_fetch.FALLBACK_ENV, "1")
    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", native)
    monkeypatch.setattr(grammar_fetch, "fetch_grammars", fake_fetch)
    monkeypatch.setattr(languages, "grammar_cache_dir", lambda: "/nonexistent/cache")
    with pytest.raises(GrammarUnavailableError) as exc:
        get_parser("rust")
    assert "UnknownIssuer" in exc.value.error
    assert "zstd decoder" in exc.value.error
    assert "zstandard" in exc.value.hint


def test_a_non_download_failure_does_not_trigger_a_fetch(monkeypatch, clean_parser_cache):
    import tree_sitter_language_pack

    def native(name):
        raise RuntimeError("symbol tree_sitter_x not found in library")

    def fake_fetch(langs, cache_dir):  # pragma: no cover - must not run
        raise AssertionError("fetched for a non-network failure")

    monkeypatch.setenv(grammar_fetch.FALLBACK_ENV, "1")
    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", native)
    monkeypatch.setattr(grammar_fetch, "fetch_grammars", fake_fetch)
    with pytest.raises(GrammarUnavailableError) as exc:
        get_parser("ruby")
    assert exc.value.fallback_error is None


# ---------- grammar_fetch.fetch_grammars against a fake release ----------


def _bundle(files: dict[str, bytes]) -> bytes:
    zstandard = pytest.importorskip("zstandard")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return zstandard.ZstdCompressor().compress(raw.getvalue())


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _serve(monkeypatch, bundle: bytes, *, sha: str | None = None):
    key = grammar_fetch.platform_key()
    manifest = {
        "version": "9.9.9",
        "platforms": {
            key: {
                "url": "https://example.invalid/bundle.tar.zst",
                "sha256": sha or hashlib.sha256(bundle).hexdigest(),
            }
        },
    }
    seen: list[str] = []

    def fake_open(url):
        seen.append(url)
        if url.endswith("parsers.json"):
            return _Resp(json.dumps(manifest).encode())
        return _Resp(bundle)

    monkeypatch.setattr(grammar_fetch, "_open", fake_open)
    monkeypatch.setattr(grammar_fetch, "pack_version", lambda: "9.9.9")
    return seen


def test_fetch_unpacks_only_the_requested_grammars(monkeypatch, tmp_path: Path):
    bundle = _bundle(
        {
            "./libtree_sitter_typescript.so": b"TS",
            "./libtree_sitter_tsx.so": b"TSX",
            "./libtree_sitter_elixir.so": b"EX",
            "./../../evil.so": b"no",
        }
    )
    seen = _serve(monkeypatch, bundle)
    out = grammar_fetch.fetch_grammars(["typescript", "tsx", "go"], tmp_path / "libs")
    assert out["installed"] == ["tsx", "typescript"]
    assert out["missing_from_bundle"] == ["go"]
    assert sorted(p.name for p in (tmp_path / "libs").iterdir()) == [
        "libtree_sitter_tsx.so",
        "libtree_sitter_typescript.so",
    ]
    assert seen[0].endswith("/v9.9.9/parsers.json")


def test_fetch_refuses_a_bundle_whose_checksum_does_not_match(monkeypatch, tmp_path: Path):
    bundle = _bundle({"./libtree_sitter_typescript.so": b"TS"})
    _serve(monkeypatch, bundle, sha="0" * 64)
    with pytest.raises(grammar_fetch.GrammarFetchError, match="checksum mismatch"):
        grammar_fetch.fetch_grammars(["typescript"], tmp_path / "libs")
    assert not (tmp_path / "libs" / "libtree_sitter_typescript.so").exists()


def test_ca_bundle_is_read_from_the_env_vars_in_order(monkeypatch, tmp_path: Path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    for var in grammar_fetch.CA_BUNDLE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert grammar_fetch.ca_bundle_from_env() is None
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))  # not a file: skipped
    assert grammar_fetch.ca_bundle_from_env() == ("REQUESTS_CA_BUNDLE", str(ca))
    monkeypatch.setenv("LIVESPEC_CA_BUNDLE", str(ca))
    assert grammar_fetch.ca_bundle_from_env() == ("LIVESPEC_CA_BUNDLE", str(ca))


def test_prefetch_uses_the_python_fetch_when_the_native_download_fails(monkeypatch):
    import tree_sitter_language_pack

    from livespec_mcp.domain.languages import prefetch_grammars

    def native_download(names):
        raise RuntimeError(CERT_ERROR)

    monkeypatch.setenv(grammar_fetch.FALLBACK_ENV, "1")
    monkeypatch.setattr(tree_sitter_language_pack, "download", native_download)
    monkeypatch.setattr(languages, "grammar_cache_dir", lambda: "/nonexistent/cache")
    monkeypatch.setattr(
        grammar_fetch, "fetch_grammars", lambda langs, cache: {"installed": sorted(langs)}
    )
    out = prefetch_grammars(["go", "rust"])
    assert out["failed"] == {}
    assert out["downloaded_via_python"] == ["go", "rust"]
