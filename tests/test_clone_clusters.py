"""find_clone_clusters — read one copy of a pasted function instead of N.

`search_similar` answers "does this snippet already exist?" before a write.
These tests pin the reading-side question: which bodies exist several times,
which copy is the one to read, and what the others change — without ever
handing back a copy's body.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from livespec_mcp.domain import clones as clones_domain
from livespec_mcp.server import mcp

# Long enough to clear the 40-token floor that keeps guard clauses quiet.
ORIGINAL = """\
def summarize_orders(orders, tax_rate):
    total = 0
    count = 0
    for order in orders:
        if order.cancelled:
            continue
        amount = order.price * order.quantity
        total += amount + amount * tax_rate
        count += 1
    for order in orders:
        if order.refunded and order.price > tax_rate:
            total -= order.price * order.quantity
            count -= 1
    if count == 0:
        return {"total": 0, "average": 0}
    return {"total": total, "average": total / count}
"""

# Same structure, every identifier renamed.
RENAMED = """\
def tally_invoices(invoices, vat):
    acc = 0
    seen = 0
    for inv in invoices:
        if inv.cancelled:
            continue
        value = inv.price * inv.quantity
        acc += value + value * vat
        seen += 1
    for inv in invoices:
        if inv.refunded and inv.price > vat:
            acc -= inv.price * inv.quantity
            seen -= 1
    if seen == 0:
        return {"total": 0, "average": 0}
    return {"total": acc, "average": acc / seen}
"""

# The original with one statement added — a near-copy, not an identical one.
EDITED = ORIGINAL.replace(
    '    return {"total": total,', '    total = round(total, 2)\n    return {"total": total,'
)

UNRELATED = """\
def parse_config(path, defaults):
    result = dict(defaults)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result
"""

TRIVIAL = "def get_name(self):\n    return self.name\n"


def _write(workspace, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


async def _clusters(c: Client, **kw):
    return (await c.call_tool("find_clone_clusters", kw)).data


def _qnames(cluster) -> set[str]:
    return {cluster["representative"]["qualified_name"]} | {
        o["qualified_name"] for o in cluster["others"]
    }


@pytest.mark.asyncio
async def test_renamed_copy_clusters_as_identical(workspace):
    _write(
        workspace,
        {"shop/orders.py": ORIGINAL, "shop/billing/invoices.py": RENAMED, "shop/cfg.py": UNRELATED},
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _clusters(c)

    assert out["totals"] == {
        "clusters": 1,
        "members": 2,
        "duplicated_lines": 32,
        "lines_saved_if_read_once": 16,
    }
    (cluster,) = out["clusters"]
    assert _qnames(cluster) == {
        "shop.orders.summarize_orders",
        "shop.billing.invoices.tally_invoices",
    }
    (other,) = cluster["others"]
    assert other["relation"] == "identical"
    # What was renamed, not the body.
    assert ["summarize_orders", "tally_invoices"] in other["renamed"]
    assert ["orders", "invoices"] in other["renamed"]
    assert "diff" not in other
    assert "source" not in json.dumps(out)
    assert "parse_config" not in json.dumps(out), "unrelated functions must not cluster"


@pytest.mark.asyncio
async def test_edited_copy_clusters_as_similar_with_a_bounded_diff(workspace):
    _write(workspace, {"shop/orders.py": ORIGINAL, "shop/audit/orders_logged.py": EDITED})
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _clusters(c)

    (cluster,) = out["clusters"]
    assert cluster["representative"]["qualified_name"] == "shop.orders.summarize_orders"
    (other,) = cluster["others"]
    assert other["relation"] == "similar"
    assert 0.8 <= other["similarity"] < 1.0
    assert other["lines"] == {"added": 1, "removed": 0, "changed": 0}
    # Hunk headers address real file lines on both sides.
    assert other["diff"] == ["@@ -16,0 +16,1 @@", "+total = round(total, 2)"]
    assert len(other["diff"]) <= clones_domain.MAX_DIFF_LINES_PER_MEMBER
    # Unchanged lines never travel.
    assert not any("amount = order.price" in line for line in other["diff"])


@pytest.mark.asyncio
async def test_threshold_decides_what_counts_as_a_near_copy(workspace):
    _write(workspace, {"shop/orders.py": ORIGINAL, "shop/audit/orders_logged.py": EDITED})
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        strict = await _clusters(c, threshold=1.0)
    assert strict["totals"]["clusters"] == 0


@pytest.mark.asyncio
async def test_representative_is_the_copy_with_the_most_callers(workspace):
    _write(
        workspace,
        {
            # Lexically first and on the shortest path — would win every tie-break.
            "a.py": ORIGINAL,
            "pkg/deep/invoices.py": RENAMED
            + "\n\ndef monthly(invoices):\n    return tally_invoices(invoices, 0.2)\n"
            + "\n\ndef yearly(invoices):\n    return tally_invoices(invoices, 0.1)\n",
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _clusters(c)

    rep = out["clusters"][0]["representative"]
    assert rep["qualified_name"] == "pkg.deep.invoices.tally_invoices"
    assert rep["callers"] == 2


@pytest.mark.asyncio
async def test_representative_ties_break_on_path_then_name(workspace):
    _write(
        workspace,
        {
            "pkg/deep/nested/orders.py": ORIGINAL,
            "pkg/orders.py": ORIGINAL,
            "pkg/zz_orders.py": ORIGINAL,
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _clusters(c)

    (cluster,) = out["clusters"]
    assert cluster["representative"]["file_path"] == "pkg/orders.py"
    assert cluster["members"] == 3
    assert [o["relation"] for o in cluster["others"]] == ["identical", "identical"]
    # Byte-identical copies have nothing to report beyond where they are.
    assert all("renamed" not in o and "diff" not in o for o in cluster["others"])


@pytest.mark.asyncio
async def test_production_copy_is_the_representative_even_when_tests_call_theirs(workspace):
    _write(
        workspace,
        {
            "pkg/zz/orders.py": ORIGINAL,
            "tests/helpers.py": RENAMED
            + "\n\ndef test_a():\n    return tally_invoices([], 0)\n"
            + "\n\ndef test_b():\n    return tally_invoices([], 1)\n",
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        default = await _clusters(c)
        with_tests = await _clusters(c, include_tests=True)

    # By default the test copy is not a member at all, so nothing clusters.
    assert default["totals"]["clusters"] == 0
    assert default["filtered_out"]["tests"] >= 1
    (cluster,) = with_tests["clusters"]
    assert cluster["representative"]["file_path"] == "pkg/zz/orders.py"
    assert cluster["others"][0]["file_path"] == "tests/helpers.py"


@pytest.mark.asyncio
async def test_trivial_bodies_and_generated_paths_stay_out(workspace):
    _write(
        workspace,
        {
            "pkg/a.py": "class A:\n    " + TRIVIAL.replace("\n", "\n    "),
            "pkg/b.py": "class B:\n    " + TRIVIAL.replace("\n", "\n    "),
            "pkg/orders.py": ORIGINAL,
            "vendor/lib/orders.py": ORIGINAL,
            "build/orders.py": ORIGINAL,
        },
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {"include_generated": True})
        out = await _clusters(c)

    assert out["totals"]["clusters"] == 0
    assert out["filtered_out"]["trivial"] >= 2
    assert out["filtered_out"]["generated"] >= 1


@pytest.mark.asyncio
async def test_min_size_and_path_filter(workspace):
    files = {f"pkg/copy_{i}.py": ORIGINAL for i in range(3)}
    files["other/first.py"] = UNRELATED
    files["other/second.py"] = UNRELATED
    _write(workspace, files)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        everything = await _clusters(c)
        big = await _clusters(c, min_size=3)
        scoped = await _clusters(c, path="other")
        one_file = await _clusters(c, path="other/first.py")
        nowhere = await _clusters(c, path="nope")

    assert everything["totals"]["clusters"] == 2
    assert [cl["members"] for cl in big["clusters"]] == [3]
    assert [cl["representative"]["file_path"] for cl in scoped["clusters"]] == ["other/first.py"]
    assert scoped["totals"]["clusters"] == 1
    # A file filter still shows the copies that live outside it.
    assert one_file["clusters"][0]["others"][0]["file_path"] == "other/second.py"
    assert nowhere["totals"]["clusters"] == 0


_STATEMENTS = [
    "    for item in items:\n        if item.ready:\n            acc.append(item.value * scale)\n",
    "    with open(path, encoding='utf-8') as handle:\n        text = handle.read().strip()\n",
    "    try:\n        limit = int(scale)\n    except ValueError:\n        limit = len(items)\n",
    "    lookup = {item.key: item.value for item in items if item.key is not None}\n",
    "    while acc and acc[-1] is None:\n        acc.pop()\n",
    "    names = sorted(str(item.name).lower() for item in items)\n",
    "    if not acc:\n        raise RuntimeError('nothing to do for ' + str(path))\n",
    "    total = sum(value for value in acc if value > 0) / (len(acc) or 1)\n",
    "    assert isinstance(items, list), type(items)\n",
    "    del acc[::2]\n    acc.extend(reversed(items))\n",
    "    flag = any(item.ready for item in items) and not all(item.ready for item in items)\n",
]


def _body(i: int) -> str:
    """A function unlike the other `_body(j)`: its own statements, its own length."""
    picked = [_STATEMENTS[(i * 3 + k * (i + 1)) % len(_STATEMENTS)] for k in range(4 + i)]
    return (
        f"def work_{i}(items, path, scale):\n    acc = []\n" + "".join(picked) + "    return acc\n"
    )


def _many_clusters(workspace, n: int = 6) -> None:
    """Cluster i has i+2 byte-identical copies of `_body(i)`."""
    files = {}
    for i in range(n):
        for copy in range(i + 2):
            files[f"gen{i}/copy_{copy}.py"] = _body(i)
    _write(workspace, files)


@pytest.mark.asyncio
async def test_ordering_and_exact_totals_under_pagination(workspace):
    _many_clusters(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        full = await _clusters(c)
        summary = await _clusters(c, summary_only=True)
        page1 = await _clusters(c, limit=4)
        page2 = await _clusters(c, limit=4, cursor=page1["next_cursor"])

    assert full["totals"]["clusters"] == 6
    assert full["totals"]["members"] == sum(i + 2 for i in range(6))
    saved = [cl["lines_saved"] for cl in full["clusters"]]
    assert saved == sorted(saved, reverse=True)
    assert sum(saved) == full["totals"]["lines_saved_if_read_once"]
    assert (
        sum(cl["duplicated_lines"] for cl in full["clusters"])
        == (full["totals"]["duplicated_lines"])
    )
    assert [cl["cluster"] for cl in full["clusters"]] == [1, 2, 3, 4, 5, 6]

    # Totals do not move with the page, and summary_only carries no clusters.
    assert summary["totals"] == page1["totals"] == page2["totals"] == full["totals"]
    assert "clusters" not in summary
    assert summary["next_cursor"] is None
    assert page1["next_cursor"] == 4 and page2["next_cursor"] is None
    assert page1["clusters"] + page2["clusters"] == full["clusters"]


@pytest.mark.asyncio
async def test_two_runs_give_the_same_answer(workspace):
    _many_clusters(workspace, 4)
    _write(workspace, {"x/edited.py": EDITED, "x/orig.py": ORIGINAL, "x/renamed.py": RENAMED})
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        first = await _clusters(c)
        clones_domain._REPORT_CACHE.clear()  # the second run recomputes from the index
        second = await _clusters(c)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.asyncio
async def test_payload_stays_bounded_on_a_large_cluster(workspace):
    files = {}
    for i in range(30):
        # Each copy gets its own trailing statements, so each is a distinct
        # shape with its own diff — the worst case for payload size.
        forms = (
            "    total = round(total, {j}) + count\n",
            "    count = abs(count) - {j} * total\n",
        )
        extra = "".join(forms[(i >> j) & 1].format(j=j) for j in range(5))
        files[f"svc/handler_{i:02d}.py"] = ORIGINAL.replace(
            '    return {"total": total,', extra + '    return {"total": total,'
        )
    _write(workspace, files)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _clusters(c, threshold=0.6)

    (cluster,) = out["clusters"]
    assert cluster["members"] == 30
    assert len(cluster["others"]) == clones_domain.MAX_OTHERS_LISTED
    assert cluster["others_omitted"] == 29 - clones_domain.MAX_OTHERS_LISTED
    diff_lines = sum(len(o.get("diff", [])) for o in cluster["others"])
    assert diff_lines <= clones_domain.MAX_DIFF_LINES_PER_CLUSTER
    assert all(
        len(o.get("diff", [])) <= clones_domain.MAX_DIFF_LINES_PER_MEMBER for o in cluster["others"]
    )
    assert any(o.get("diff_truncated") for o in cluster["others"])
    # 30 copies x 21 lines is ~20 KB of source; the answer is a fraction of it.
    source_chars = sum(len(t) for t in files.values())
    assert len(json.dumps(out)) < source_chars / 3


@pytest.mark.asyncio
async def test_a_closure_is_not_a_clone_of_its_wrapper(workspace):
    inner = ORIGINAL.replace("def summarize_orders", "def inner").replace("\n", "\n    ")
    # Enough of its own code that the wrapper is not just "the closure plus a
    # return" — the two are different bodies, one inside the other.
    own = UNRELATED.split("\n", 1)[1].rsplit("    return result", 1)[0]
    wrapper = f"def make(path, defaults):\n{own}    {inner}\n    return inner, result\n"
    _write(workspace, {"pkg/a.py": wrapper, "pkg/b.py": wrapper.replace("make", "build")})
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = await _clusters(c)

    # One cluster of the two wrappers; the two pasted closures inside them are
    # the same duplicated text and must not be counted a second time.
    assert out["totals"]["clusters"] == 1
    assert _qnames(out["clusters"][0]) == {"pkg.a.make", "pkg.b.build"}
    assert out["nested_clusters_dropped"] == 1


@pytest.mark.asyncio
async def test_baseline_flags_frozen_copies_but_never_hides_them(workspace):
    _write(workspace, {"shop/orders.py": ORIGINAL, "shop/billing/invoices.py": RENAMED})
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        before = await _clusters(c)
        await c.call_tool("debt_baseline_capture", {})
        after = await _clusters(c)

    assert "frozen_members" not in before["clusters"][0]
    assert after["totals"] == before["totals"]
    assert after["clusters"][0]["frozen_members"] == 2


@pytest.mark.asyncio
async def test_opening_a_clone_says_it_has_copies(workspace):
    _write(
        workspace,
        {"shop/orders.py": ORIGINAL, "shop/billing/invoices.py": RENAMED, "shop/cfg.py": UNRELATED},
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        cold = (await c.call_tool("get_symbol_source", {"qname": "summarize_orders"})).data
        await _clusters(c)  # warms the fingerprint cache
        src = (await c.call_tool("get_symbol_source", {"qname": "summarize_orders"})).data
        unit = (await c.call_tool("read_unit", {"qname": "tally_invoices"})).data
        alone = (await c.call_tool("read_unit", {"qname": "parse_config"})).data

    # Never a repo-wide pass on a one-symbol read: silent until the cache exists.
    assert "clones" not in cold
    assert src["clones"]["identical_copies"] == 1
    assert src["clones"]["also_at"] == [
        "shop.billing.invoices.tally_invoices (shop/billing/invoices.py:1)"
    ]
    assert unit["clones"]["also_at"] == ["shop.orders.summarize_orders (shop/orders.py:1)"]
    assert "clones" not in alone


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [{"min_size": 1}, {"threshold": 0}, {"threshold": 1.5}, {"limit": 0}, {"cursor": -1}],
)
async def test_bad_arguments_use_the_error_shape(workspace, args):
    _write(workspace, {"shop/orders.py": ORIGINAL})
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        res = await c.call_tool("find_clone_clusters", args, raise_on_error=False)
    out = res.data if res.data is not None else res.structured_content
    assert out["isError"] is True
    assert set(out) <= {"error", "isError", "did_you_mean", "hint"}


# ---------------------------------------------------------------------------
# the join itself
# ---------------------------------------------------------------------------


def _brute_force(shapes, threshold):
    pairs = set()
    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            a, b = shapes[i][1], shapes[j][1]
            if a and b and len(a & b) / len(a | b) >= threshold:
                pairs.add(frozenset((i, j)))
    return pairs


@pytest.mark.parametrize("threshold", [0.5, 0.8, 0.95, 1.0])
def test_prefix_filter_join_finds_exactly_what_a_full_scan_finds(threshold):
    import random

    rng = random.Random(7)
    shapes = []
    for i in range(120):
        base = frozenset(rng.sample(range(400), rng.randint(5, 30)))
        shapes.append((f"s{i:03d}", base))
        if i % 3 == 0:  # a near-copy: drop one hash, add one
            mutated = set(base)
            mutated.discard(next(iter(sorted(mutated))))
            mutated.add(1000 + i)
            shapes.append((f"s{i:03d}m", frozenset(mutated)))
        if i % 10 == 0:  # same hashes under another shape key
            shapes.append((f"s{i:03d}d", base))

    pairs, candidates = clones_domain.similar_shape_pairs(shapes, threshold)
    assert {frozenset(p) for p in pairs} == _brute_force(shapes, threshold)
    assert candidates < len(shapes) * (len(shapes) - 1) // 2
