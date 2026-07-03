"""v0.7 B2: propose_specs_from_codebase — heuristic Spec discovery.

The killer brownfield feature. For an existing project with no Specs, this
proposes ~30 Spec candidates grouped by module + ranked by PageRank-weighted
group importance. The agent reviews and accepts via bulk_link_spec_symbols.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _make_layered_repo(workspace):
    """Three modules with distinct concerns: auth, payments, util."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    auth = pkg / "auth"
    auth.mkdir()
    (auth / "__init__.py").write_text("")
    (auth / "login.py").write_text(
        '"""Auth login flow."""\n'
        "def login(u, p):\n"
        '    """Validates user credentials."""\n'
        "    return verify(u, p)\n"
        "\n"
        "def verify(u, p):\n"
        "    return True\n"
        "\n"
        "def logout(token):\n"
        "    return True\n"
    )

    payments = pkg / "payments"
    payments.mkdir()
    (payments / "__init__.py").write_text("")
    (payments / "charge.py").write_text(
        '"""Payment processing."""\n'
        "def charge(amount):\n"
        '    """Charges a card and returns a receipt."""\n'
        "    return validate_card() and submit(amount)\n"
        "\n"
        "def validate_card():\n"
        "    return True\n"
        "\n"
        "def submit(amount):\n"
        "    return {'ok': True}\n"
        "\n"
        "def refund(receipt_id):\n"
        "    return True\n"
    )


@pytest.mark.asyncio
async def test_propose_requirements_basic(workspace):
    _make_layered_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {"module_depth": 2, "min_symbols_per_group": 2},
            )
        ).data

    proposals = out["proposals"]
    assert len(proposals) >= 2, f"expected at least 2 proposals, got {proposals}"
    # Title humanization
    titles = {p["title"].lower() for p in proposals}
    assert "auth" in titles or "payments" in titles, f"titles: {titles}"

    # Each proposal has the expected shape
    for p in proposals:
        assert p["proposed_spec_id"].startswith("SPEC-")
        assert p["module_key"]
        assert p["symbol_count"] > 0
        assert isinstance(p["suggested_symbols"], list)
        assert p["suggested_symbols"], "suggested_symbols must be non-empty"
        for s in p["suggested_symbols"]:
            assert "qualified_name" in s
            assert "pagerank" in s


@pytest.mark.asyncio
async def test_propose_requirements_spec_ids_unique_and_continuous(workspace):
    """Proposed Spec ids continue from the highest existing Spec id."""
    _make_layered_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Seed two existing Specs so proposals start at SPEC-003
        await c.call_tool("create_spec", {"spec_id": "SPEC-001", "title": "x"})
        await c.call_tool("create_spec", {"spec_id": "SPEC-002", "title": "y"})
        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {"module_depth": 2, "min_symbols_per_group": 2},
            )
        ).data
    spec_ids = [p["proposed_spec_id"] for p in out["proposals"]]
    assert len(spec_ids) == len(set(spec_ids)), "proposed_spec_id must be unique"
    # First proposal should be SPEC-003
    if spec_ids:
        assert spec_ids[0] == "SPEC-003"


@pytest.mark.asyncio
async def test_propose_skips_already_covered(workspace):
    """A module that's already >50% covered shouldn't appear by default."""
    _make_layered_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_spec", {"spec_id": "Spec-AUTH", "title": "Auth"})
        # Cover 2/3 auth symbols (>50%)
        await c.call_tool(
            "bulk_link_spec_symbols",
            {
                "mappings": [
                    {"spec_id": "Spec-AUTH", "symbol_qname": "pkg.auth.login.login"},
                    {"spec_id": "Spec-AUTH", "symbol_qname": "pkg.auth.login.verify"},
                ]
            },
        )

        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {"module_depth": 2, "min_symbols_per_group": 2},
            )
        ).data
        keys = {p["module_key"] for p in out["proposals"]}
        # auth was 2/3 covered -> skipped
        assert "pkg.auth" not in keys, f"covered auth must be skipped: {keys}"

        # With skip_already_covered=False, auth resurfaces
        out2 = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {
                    "module_depth": 2,
                    "min_symbols_per_group": 2,
                    "skip_already_covered": False,
                },
            )
        ).data
        keys2 = {p["module_key"] for p in out2["proposals"]}
        assert "pkg.auth" in keys2 or "pkg.payments" in keys2


@pytest.mark.asyncio
async def test_humanize_title_avoids_generic_segments(workspace):
    """A module like `app.src.auth_service.*` should yield title 'Auth Service',
    not 'src'."""
    pkg = workspace / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    src = pkg / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    auth = src / "auth_service"
    auth.mkdir()
    (auth / "__init__.py").write_text("")
    (auth / "login.py").write_text(
        '"""Auth service login flow."""\n'
        "def login(u, p):\n    return verify(u, p)\n"
        "\n"
        "def verify(u, p):\n    return True\n"
        "\n"
        "def logout(token):\n    return True\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # depth=3 -> group_key 'app.src.auth_service' -> title 'Auth Service'
        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {"module_depth": 3, "min_symbols_per_group": 2},
            )
        ).data
    titles = {p["title"] for p in out["proposals"]}
    assert any("Auth Service" in t for t in titles), f"titles: {titles}"

    # Underscore -> space, title-cased
    assert "auth_service" not in {t.lower().replace(" ", "_") for t in titles} or \
           any("Auth Service" == t for t in titles)


@pytest.mark.asyncio
async def test_propose_requirements_skips_test_modules(workspace):
    """v0.8 P2 fix #10: tests/ should not generate Spec proposals.

    Earlier behavior: a tests/ folder with N test functions produced
    an 'Spec-N Test Shortener'-style proposal grouping test fns as a
    'feature'. Tests exercise features; they are not features themselves.
    """
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "feature.py").write_text(
        "def widget():\n    return 1\n"
        "def helper_a():\n    return 1\n"
        "def helper_b():\n    return 1\n"
    )
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "test_widget.py").write_text(
        "from pkg.feature import widget\n"
        "def test_one():\n    assert widget() == 1\n"
        "def test_two():\n    assert widget() == 1\n"
        "def test_three():\n    assert widget() == 1\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {"module_depth": 1, "min_symbols_per_group": 2},
            )
        ).data
    keys = {p["module_key"] for p in out["proposals"]}
    titles = {p["title"] for p in out["proposals"]}
    assert not any("test" in k.lower() for k in keys), (
        f"tests/ should not generate proposals: keys={keys}, titles={titles}"
    )


@pytest.mark.asyncio
async def test_default_module_depth_does_not_collapse_deep_tree(workspace):
    """F5: default module_depth=3 must not collapse a deep `src.pkg.*`
    subtree into a single useless Spec. The default call (no module_depth)
    yields more than one proposal with sub-module granularity.

    With the old default of 2, `src.pkg.auth` and `src.pkg.payments` both
    collapsed to group "src.pkg" -> one Spec absorbing the whole tree.
    """
    src = workspace / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    pkg = src / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    auth = pkg / "auth"
    auth.mkdir()
    (auth / "__init__.py").write_text("")
    (auth / "login.py").write_text(
        '"""Auth login flow."""\n'
        "def login(u, p):\n    return verify(u, p)\n"
        "\n"
        "def verify(u, p):\n    return True\n"
        "\n"
        "def logout(token):\n    return True\n"
    )

    payments = pkg / "payments"
    payments.mkdir()
    (payments / "__init__.py").write_text("")
    (payments / "charge.py").write_text(
        '"""Payment processing."""\n'
        "def charge(amount):\n    return submit(amount)\n"
        "\n"
        "def submit(amount):\n    return {'ok': True}\n"
        "\n"
        "def refund(receipt_id):\n    return True\n"
    )

    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # No module_depth -> uses the new default (3).
        out = (
            await c.call_tool(
                "propose_specs_from_codebase",
                {"min_symbols_per_group": 2},
            )
        ).data

    assert out["module_depth"] == 3, "default module_depth must be 3"
    proposals = out["proposals"]
    assert len(proposals) > 1, (
        f"deep tree collapsed into a single Spec: {proposals}"
    )
    keys = {p["module_key"] for p in proposals}
    # No proposal should be the shallow `src.pkg` that absorbs the whole tree.
    assert "src.pkg" not in keys, f"deep tree absorbed by src.pkg: {keys}"
    assert "src.pkg.auth" in keys and "src.pkg.payments" in keys, (
        f"expected sub-module granularity: {keys}"
    )
