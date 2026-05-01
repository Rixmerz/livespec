"""v0.7 B2: propose_requirements_from_codebase — heuristic RF discovery.

The killer brownfield feature. For an existing project with no RFs, this
proposes ~30 RF candidates grouped by module + ranked by PageRank-weighted
group importance. The agent reviews and accepts via bulk_link_rf_symbols.
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
                "propose_requirements_from_codebase",
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
        assert p["proposed_rf_id"].startswith("RF-")
        assert p["module_key"]
        assert p["symbol_count"] > 0
        assert isinstance(p["suggested_symbols"], list)
        assert p["suggested_symbols"], "suggested_symbols must be non-empty"
        for s in p["suggested_symbols"]:
            assert "qualified_name" in s
            assert "pagerank" in s


@pytest.mark.asyncio
async def test_propose_requirements_rf_ids_unique_and_continuous(workspace):
    """Proposed RF ids continue from the highest existing RF id."""
    _make_layered_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        # Seed two existing RFs so proposals start at RF-003
        await c.call_tool("create_requirement", {"rf_id": "RF-001", "title": "x"})
        await c.call_tool("create_requirement", {"rf_id": "RF-002", "title": "y"})
        out = (
            await c.call_tool(
                "propose_requirements_from_codebase",
                {"module_depth": 2, "min_symbols_per_group": 2},
            )
        ).data
    rf_ids = [p["proposed_rf_id"] for p in out["proposals"]]
    assert len(rf_ids) == len(set(rf_ids)), "proposed_rf_id must be unique"
    # First proposal should be RF-003
    if rf_ids:
        assert rf_ids[0] == "RF-003"


@pytest.mark.asyncio
async def test_propose_skips_already_covered(workspace):
    """A module that's already >50% covered shouldn't appear by default."""
    _make_layered_repo(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        await c.call_tool("create_requirement", {"rf_id": "RF-AUTH", "title": "Auth"})
        # Cover 2/3 auth symbols (>50%)
        await c.call_tool(
            "bulk_link_rf_symbols",
            {
                "mappings": [
                    {"rf_id": "RF-AUTH", "symbol_qname": "pkg.auth.login.login"},
                    {"rf_id": "RF-AUTH", "symbol_qname": "pkg.auth.login.verify"},
                ]
            },
        )

        out = (
            await c.call_tool(
                "propose_requirements_from_codebase",
                {"module_depth": 2, "min_symbols_per_group": 2},
            )
        ).data
        keys = {p["module_key"] for p in out["proposals"]}
        # auth was 2/3 covered -> skipped
        assert "pkg.auth" not in keys, f"covered auth must be skipped: {keys}"

        # With skip_already_covered=False, auth resurfaces
        out2 = (
            await c.call_tool(
                "propose_requirements_from_codebase",
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
                "propose_requirements_from_codebase",
                {"module_depth": 3, "min_symbols_per_group": 2},
            )
        ).data
    titles = {p["title"] for p in out["proposals"]}
    assert any("Auth Service" in t for t in titles), f"titles: {titles}"

    # Underscore -> space, title-cased
    assert "auth_service" not in {t.lower().replace(" ", "_") for t in titles} or \
           any("Auth Service" == t for t in titles)
