"""v0.9 P5: Django class-based view detection in find_endpoints (#15).

Decorator-based detection misses Django's most common pattern: class
inheriting from `LoginRequiredMixin` / `View` / `FormView` / etc.
Surfaced by Django session-04 where 20 endpoints were returned but all
were decorator-based (no class-based views).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _write_views(workspace: Path) -> None:
    pkg = workspace / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "views.py").write_text(
        "from django.contrib.auth.mixins import LoginRequiredMixin\n"
        "from django.views.generic import FormView, ListView\n"
        "\n"
        "class ProfileView(LoginRequiredMixin, FormView):\n"
        "    template_name = 'profile.html'\n"
        "\n"
        "class ArticleList(ListView):\n"
        "    model = 'Article'\n"
        "\n"
        "class JustAClass:\n"
        "    pass\n"
    )


@pytest.mark.asyncio
async def test_find_endpoints_django_includes_cbv(workspace):
    _write_views(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_endpoints", {"framework": "django"})
        ).data
        qnames = {e["qualified_name"] for e in out["endpoints"]}
        assert "myapp.views.ProfileView" in qnames, (
            f"LoginRequiredMixin-protected CBV must be detected: {qnames}"
        )
        assert "myapp.views.ArticleList" in qnames
        # Plain class is not an endpoint
        assert "myapp.views.JustAClass" not in qnames


@pytest.mark.asyncio
async def test_find_endpoints_django_cbv_carries_base_label(workspace):
    """Each CBV-detected endpoint reports which base classified it."""
    _write_views(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_endpoints", {"framework": "django"})
        ).data
        by_qname = {e["qualified_name"]: e for e in out["endpoints"]}
        profile = by_qname.get("myapp.views.ProfileView")
        assert profile is not None
        # Either of the bases is acceptable — first match wins
        assert profile.get("django_cbv_base") in (
            "LoginRequiredMixin",
            "FormView",
        )


@pytest.mark.asyncio
async def test_find_endpoints_no_framework_includes_django_cbv(workspace):
    """Default `framework=None` should also surface Django CBVs."""
    _write_views(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {})).data
        qnames = {e["qualified_name"] for e in out["endpoints"]}
        assert "myapp.views.ProfileView" in qnames


@pytest.mark.asyncio
async def test_find_endpoints_flask_does_not_surface_django_cbv(workspace):
    """Filtering to a non-Django framework keeps the result clean."""
    _write_views(workspace)
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (
            await c.call_tool("find_endpoints", {"framework": "flask"})
        ).data
        qnames = {e["qualified_name"] for e in out["endpoints"]}
        assert "myapp.views.ProfileView" not in qnames
