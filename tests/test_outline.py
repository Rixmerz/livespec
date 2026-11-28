"""quick_orient outlines — "what does this module / class offer?" in one call.

Measured before this existed: answering that for a module took either the
whole file or one `quick_orient` per symbol, and the second was *more*
characters than the first. Signature, docstring and span are already columns
of the index, so the outline is one query and needs no cache.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from livespec_mcp.domain import outline as outline_domain
from livespec_mcp.server import mcp

MODULE = '''\
"""Billing."""


def charge(customer, amount: int) -> bool:
    """Charge a customer.

    Long explanation that an outline must not carry.
    """
    ledger = Ledger()
    return ledger.record(customer, amount)


class Ledger:
    """Append-only record of charges."""

    def record(self, customer, amount: int) -> bool:
        """Write one entry."""
        self._entries.append((customer, amount))
        return True

    def total(self) -> int:
        return sum(a for _, a in self._entries)
'''


async def _orient(c: Client, qname: str):
    res = await c.call_tool("quick_orient", {"qname": qname}, raise_on_error=False)
    return res.data if res.data is not None else res.structured_content


@pytest.mark.asyncio
async def test_a_file_path_returns_its_outline(workspace):
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "billing.py").write_text(MODULE, encoding="utf-8")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _orient(c, "pkg/billing.py")
        dotted = await _orient(c, "./pkg/billing.py")

    assert out["file_path"] == "pkg/billing.py"
    assert out["symbols"] == 4
    assert out["outline"] == [
        "4-10 charge(customer, amount: int) -> bool — Charge a customer.",
        "13-22 class Ledger() — Append-only record of charges.",
        "  16-19 record(self, customer, amount: int) -> bool — Write one entry.",
        "  21-22 total(self) -> int",
    ]
    assert dotted["outline"] == out["outline"]
    # Signatures and leads only — no body, no second docstring line.
    blob = json.dumps(out)
    assert "ledger.record" not in blob and "Long explanation" not in blob
    assert len(blob) < len(MODULE)


@pytest.mark.asyncio
async def test_a_class_carries_the_outline_of_its_members(workspace):
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "billing.py").write_text(MODULE, encoding="utf-8")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        cls = await _orient(c, "pkg.billing.Ledger")
        fn = await _orient(c, "pkg.billing.charge")

    assert cls["kind"] == "class"
    assert cls["members"] == 2
    assert cls["outline"] == [
        "16-19 record(self, customer, amount: int) -> bool — Write one entry.",
        "21-22 total(self) -> int",
    ]
    # A plain function keeps the payload it always had.
    assert "outline" not in fn and "members" not in fn
    assert fn["qualified_name"] == "pkg.billing.charge"


@pytest.mark.asyncio
async def test_an_unknown_name_is_still_a_shaped_error(workspace):
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "billing.py").write_text(MODULE, encoding="utf-8")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _orient(c, "pkg/missing.py")

    assert out["isError"] is True
    assert set(out) <= {"error", "isError", "did_you_mean", "hint"}


@pytest.mark.asyncio
async def test_outline_is_bounded(workspace):
    params = ", ".join(f"argument_number_{i}: dict[str, list[int]]" for i in range(40))
    body = f"def wide({params}):\n    return 1\n\n\n"
    body += "".join(f"def f_{i}():\n    return {i}\n\n\n" for i in range(200))
    (workspace / "big.py").write_text(body, encoding="utf-8")
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _orient(c, "big.py")

    assert out["symbols"] == 201
    assert len(out["outline"]) == outline_domain.MAX_OUTLINE_LINES
    assert out["outline_truncated"] == 201 - outline_domain.MAX_OUTLINE_LINES
    # A 1,500-character signature does not get to be one outline line.
    assert len(out["outline"][0]) <= outline_domain.MAX_SIGNATURE + 12
    assert out["outline"][0].endswith("…")
