"""P1 cross-project: a shared `[workspace] group_db` lets a Spec in one repo
link + surface symbols that live in another repo of the same group."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp import state as state_module
from livespec_mcp.config import load_repo_config
from livespec_mcp.server import mcp


def _make_repo(root: Path, pkg_mod: str, func: str, *, group_db: Path | None) -> Path:
    """Create a tiny 1-function Python repo, optionally in a shared group DB."""
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / f"{pkg_mod}.py").write_text(
        f"def {func}(x):\n    return x\n"
    )
    if group_db is not None:
        (root / ".livespec.toml").write_text(
            f'[workspace]\ngroup_db = "{group_db}"\n'
        )
    return root


@pytest.mark.asyncio
async def test_spec_links_symbol_across_repos_in_a_group(tmp_path):
    shared = tmp_path / "grp" / "shared.db"
    back = _make_repo(tmp_path / "back", "back", "handler", group_db=shared)
    front = _make_repo(tmp_path / "front", "front", "caller", group_db=shared)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(back)})
        await c.call_tool("index_project", {"workspace": str(front)})

        # Both repos landed in the one shared DB.
        assert back.joinpath(".mcp-docs").exists()  # docs stay per-repo
        assert shared.exists()  # the group DB is the shared file

        # A backend Spec links a *frontend* symbol — the cross-repo link.
        await c.call_tool(
            "create_spec",
            {"workspace": str(back), "spec_id": "SPEC-001", "title": "Login feature"},
        )
        linked = (
            await c.call_tool(
                "link_spec_symbol",
                {
                    "workspace": str(back),
                    "spec_id": "SPEC-001",
                    "symbol_qname": "pkg.front.caller",
                },
            )
        ).data
        assert linked.get("linked") is True

        impl = (
            await c.call_tool(
                "get_spec_implementation",
                {"workspace": str(back), "spec_id": "SPEC-001"},
            )
        ).data
        qnames = {s["qualified_name"] for s in impl["symbols"]}
        assert "pkg.front.caller" in qnames
        # The surfaced symbol's file lives in the *front* repo.
        assert any("front" in f for f in impl["files"])


@pytest.mark.asyncio
async def test_ungrouped_repo_cannot_link_foreign_symbol(tmp_path):
    """Without group_db, resolution stays home-only — a symbol from an
    unrelated repo is not found (zero-regression guard)."""
    a = _make_repo(tmp_path / "a", "amod", "afunc", group_db=None)
    b = _make_repo(tmp_path / "b", "bmod", "bfunc", group_db=None)

    async with Client(mcp) as c:
        await c.call_tool("index_project", {"workspace": str(a)})
        await c.call_tool("index_project", {"workspace": str(b)})
        await c.call_tool(
            "create_spec",
            {"workspace": str(a), "spec_id": "SPEC-001", "title": "X"},
        )
        res = (
            await c.call_tool(
                "link_spec_symbol",
                {
                    "workspace": str(a),
                    "spec_id": "SPEC-001",
                    "symbol_qname": "pkg.bmod.bfunc",  # lives in repo b, separate DB
                },
            )
        ).data
        assert res.get("isError") is True


@pytest.mark.asyncio
async def test_find_symbol_says_which_db_and_where_the_repo_lives(tmp_path):
    """`grouped: true` on its own is unactionable.

    A cross-repo hit's `file_path` is relative to the repo that owns it, not
    to the workspace the agent passed, so without `project_root` the match
    can't be opened; `group_db` names the database that answered.
    """
    shared = tmp_path / "grp" / "shared.db"
    back = _make_repo(tmp_path / "back", "back", "handler", group_db=shared)
    front = _make_repo(tmp_path / "front", "front", "caller", group_db=shared)
    solo = _make_repo(tmp_path / "solo", "solo", "handler", group_db=None)

    async with Client(mcp) as c:
        for repo in (back, front, solo):
            await c.call_tool("index_project", {"workspace": str(repo)})

        out = (
            await c.call_tool("find_symbol", {"workspace": str(back), "query": "caller"})
        ).data
        assert out["grouped"] is True
        assert out["group_db"] == str(shared)
        foreign = next(m for m in out["matches"] if m["qualified_name"].endswith(".caller"))
        assert foreign["project_root"] == str(front)

        # An ungrouped workspace keeps the leaner payload.
        solo_out = (
            await c.call_tool("find_symbol", {"workspace": str(solo), "query": "handler"})
        ).data
        assert "group_db" not in solo_out
        assert "grouped" not in solo_out
        assert all("project_root" not in m for m in solo_out["matches"])


def test_group_project_ids_ungrouped_is_home_only(tmp_path):
    """Unit guard: an ungrouped AppState resolves to exactly [home]."""
    repo = _make_repo(tmp_path / "solo", "m", "f", group_db=None)
    st = state_module.get_state(str(repo), create=True)
    assert st.group_project_ids() == [st.project_id]
    assert st.settings.grouped is False


def test_config_rejects_unknown_workspace_key(tmp_path):
    (tmp_path / ".livespec.toml").write_text('[workspace]\nbogus = "x"\n')
    with pytest.raises(ValueError, match="unknown \\[workspace\\] keys"):
        load_repo_config(tmp_path)


def test_config_parses_group_db(tmp_path):
    (tmp_path / ".livespec.toml").write_text('[workspace]\ngroup_db = "../shared.db"\n')
    cfg = load_repo_config(tmp_path)
    assert cfg.group_db == "../shared.db"
