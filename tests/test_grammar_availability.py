"""A missing tree-sitter grammar must be loud, and must be retried.

`tree-sitter-language-pack` 1.x stopped bundling grammars in the wheel and
downloads each one from GitHub on first use. On a machine with no network, or
behind a proxy whose CA its native downloader does not trust, `get_parser`
raises for every language — and Python keeps working, because `_py_extract`
uses the stdlib `ast`.

Before this, `_ts_extract` swallowed that and returned an empty result, which
the indexer persisted: zero symbols recorded AND the content hash advanced, so
the next run saw the file as unchanged and never retried it. One offline index
turned a polyglot repo permanently Python-only, and `find_dead_code` then
reported every TypeScript symbol in it as dead — with nothing anywhere in the
payload saying why.

These tests pin the three halves of the fix: the failure is distinguishable
(`GrammarUnavailableError`, not a bare Exception), it is never persisted, and
`index_project` says so in a field an agent reads.

They simulate the failure rather than depending on the environment, so they
mean the same thing on a CI runner that CAN download grammars and on a
developer laptop that cannot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.domain import extractors
from livespec_mcp.domain.extractors import extract
from livespec_mcp.domain.languages import (
    EXTRACTOR_SUPPORTED,
    GrammarUnavailableError,
    get_parser,
)
from livespec_mcp.server import mcp

from .conftest import requires_grammar


def _raise_grammar_unavailable(language: str):
    raise GrammarUnavailableError(language, RuntimeError("no network"))


def _indexed_paths(workspace: Path) -> set[str]:
    """File rows this workspace actually persisted."""
    from livespec_mcp.state import get_state

    st = get_state(str(workspace))
    return {
        r["path"]
        for r in st.conn.execute("SELECT path FROM file WHERE project_id=?", (st.project_id,))
    }


@pytest.fixture
def no_grammars(monkeypatch):
    """Every tree-sitter grammar fails to load; Python (stdlib ast) still works."""

    def _boom(language: str):
        raise GrammarUnavailableError(language, RuntimeError("no network"))

    monkeypatch.setattr(extractors, "get_parser", _boom)


def test_a_missing_grammar_is_its_own_error_type(monkeypatch):
    """Not a bare Exception: the caller has to be able to tell this apart from
    a file it read and could not parse."""
    import tree_sitter_language_pack

    def _boom(name):
        raise OSError("download failed: certificate verify failed")

    monkeypatch.setattr(tree_sitter_language_pack, "get_parser", _boom)
    get_parser.cache_clear()
    try:
        with pytest.raises(GrammarUnavailableError) as exc:
            get_parser("rust")
        assert exc.value.language == "rust"
        assert isinstance(exc.value.cause, OSError)
    finally:
        get_parser.cache_clear()


def test_extract_reports_the_language_instead_of_an_empty_success(tmp_path: Path, no_grammars):
    src = "export function hello() { return 1 }\n"
    p = tmp_path / "a.ts"
    p.write_text(src)

    _, result = extract(p, src, tmp_path)

    assert result.grammar_missing == "typescript"
    assert result.parse_error is True
    assert result.symbols == []


def test_python_is_unaffected_because_it_does_not_use_tree_sitter(tmp_path: Path, no_grammars):
    """The failure is per-language. A Python-only repo must index normally even
    when no grammar on earth will load."""
    src = "def hello():\n    return 1\n"
    p = tmp_path / "a.py"
    p.write_text(src)

    _, result = extract(p, src, tmp_path)

    assert result.grammar_missing is None
    assert [s.name for s in result.symbols] == ["hello"]


@pytest.mark.asyncio
async def test_index_says_which_languages_it_could_not_read(workspace: Path, no_grammars):
    (workspace / "app.py").write_text("def py_fn():\n    return 1\n")
    (workspace / "app.ts").write_text("export function tsFn() { return 1 }\n")
    (workspace / "lib.rs").write_text("pub fn rs_fn() -> i32 { 1 }\n")

    async with Client(mcp) as c:
        payload = (await c.call_tool("index_project", {})).data

    assert payload["languages_failed"] == {"typescript": 1, "rust": 1}
    assert "languages_failed_hint" in payload
    assert "livespec grammars" in payload["languages_failed_hint"]
    # The Python file still indexed; the failure is per-language, not fatal.
    assert payload["symbols_total"] >= 1


@pytest.mark.asyncio
async def test_a_readable_repo_never_mentions_grammar_failures(workspace: Path):
    """Silent on the happy path — a field that is always present is a field
    nobody reads when it matters."""
    (workspace / "app.py").write_text("def py_fn():\n    return 1\n")

    async with Client(mcp) as c:
        payload = (await c.call_tool("index_project", {})).data

    assert "languages_failed" not in payload
    assert "languages_failed_hint" not in payload


@pytest.mark.asyncio
async def test_the_file_is_left_unindexed_so_the_next_run_retries_it(workspace: Path, monkeypatch):
    """The regression that motivated all of this.

    A file skipped for a missing grammar must NOT get a persisted row with an
    advanced content hash. If it does, the next index — even one run after the
    grammar is finally available — treats it as unchanged and skips it forever,
    and nothing short of `force=True` ever recovers those symbols.
    """
    (workspace / "app.ts").write_text("export function tsFn() { return 1 }\n")

    # A LOCAL monkeypatch context, not the fixture. `monkeypatch.undo()` on the
    # fixture reverts every patch it holds — including the autouse one in
    # conftest that binds tool calls to this workspace — so the second
    # `index_project({})` came back as a shaped "workspace required" error and
    # the assertion died on a missing key rather than on the behaviour. Caught
    # only on CI, because the grammar this test needs does not exist locally
    # and the skip below hid it.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(extractors, "get_parser", _raise_grammar_unavailable)
        async with Client(mcp) as c:
            first = (await c.call_tool("index_project", {})).data
    assert first["languages_failed"] == {"typescript": 1}
    assert first["symbols_total"] == 0

    # Nothing was persisted, which is WHY the next run retries: no file row
    # means no stored content hash to compare against.
    assert _indexed_paths(workspace) == set()

    # The grammar becomes available (someone ran `livespec grammars`), and the
    # SAME unchanged file must now be extracted without force=True.
    requires_grammar("typescript")

    async with Client(mcp) as c:
        second = (await c.call_tool("index_project", {})).data
    assert second["files_changed"] == 1, "the skipped file was never retried"
    assert second["symbols_total"] >= 1
    assert "languages_failed" not in second


@pytest.mark.asyncio
async def test_nothing_is_persisted_for_a_file_that_was_never_read(workspace: Path):
    """The mechanism behind the retry, checkable without any grammar at all.

    A file row carries the content hash. Persisting one for a file the parser
    never opened is what made the skip permanent: the hash said "seen, and
    unchanged" about bytes nothing had looked at.
    """
    (workspace / "keep.py").write_text("def kept():\n    return 1\n")
    (workspace / "app.ts").write_text("export function tsFn() { return 1 }\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(extractors, "get_parser", _raise_grammar_unavailable)
        async with Client(mcp) as c:
            payload = (await c.call_tool("index_project", {})).data

    # The Python file indexed; the unreadable one left no trace whatsoever.
    assert _indexed_paths(workspace) == {"keep.py"}
    assert payload["languages_failed"] == {"typescript": 1}
    assert payload["files_skipped"] >= 1

    # A second run reaches the file again, because there is no stored hash
    # claiming it is unchanged. It fails the same way here (still no grammar),
    # and that is the point: the skip is retried, not remembered.
    #
    # This second call also guards the trap that made the CI-only failure
    # above: it must be a real index payload, not the shaped "workspace
    # required" error a clobbered conftest binding would produce.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(extractors, "get_parser", _raise_grammar_unavailable)
        async with Client(mcp) as c:
            again = (await c.call_tool("index_project", {})).data
    assert "files_changed" in again, again
    assert again["languages_failed"] == {"typescript": 1}


def test_prefetch_targets_what_livespec_extracts_not_the_whole_pack(monkeypatch):
    """The pack ships several hundred grammars; downloading them all to index a
    Python repo is a large download for parsers nothing here would ever load."""
    import tree_sitter_language_pack

    asked: list[str] = []
    monkeypatch.setattr(tree_sitter_language_pack, "download", lambda names: asked.extend(names))
    from livespec_mcp.domain.languages import prefetch_grammars

    result = prefetch_grammars()

    assert set(asked) == set(EXTRACTOR_SUPPORTED)
    assert result["failed"] == {}


def test_prefetch_reports_a_failure_rather_than_claiming_success(monkeypatch):
    import tree_sitter_language_pack

    def _boom(names):
        raise OSError("certificate verify failed")

    monkeypatch.setattr(tree_sitter_language_pack, "download", _boom)
    from livespec_mcp.domain.languages import prefetch_grammars

    result = prefetch_grammars(["rust"])

    assert result["downloaded"] == []
    assert "rust" in result["failed"]


def test_grammars_check_exits_nonzero_when_something_is_missing(monkeypatch, capsys):
    """So a Docker build or a provisioning script fails loudly instead of
    shipping an image that will quietly index Python only."""
    from livespec_mcp import cli

    monkeypatch.setattr("livespec_mcp.domain.languages.downloaded_grammars", lambda: ["python"])
    assert cli.main(["grammars", "--check"]) == 1
    out = capsys.readouterr().out
    assert "missing" in out

    monkeypatch.setattr(
        "livespec_mcp.domain.languages.downloaded_grammars",
        lambda: sorted(EXTRACTOR_SUPPORTED),
    )
    assert cli.main(["grammars", "--check"]) == 0
