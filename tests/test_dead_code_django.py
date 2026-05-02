"""v0.9 P4: Django-style dead-code false-positive fixes.

Three patterns surfaced by session 04 against Django 5.1.4 (824 dead
candidates, 801 in `django/`):

  - non-Python files (vendored xregexp.js etc.) flagged dead because the
    Python-only module-level scanner can't read JS callsites
  - string-based dotted-path references in settings (INSTALLED_APPS,
    MIDDLEWARE, PASSWORD_HASHERS, default_app_config) — the referenced
    classes look unused to AST-only analysis
  - Django `class Meta:` and `class Migration:` inner classes — read
    reflectively by ModelBase / FormMeta / MigrationLoader metaclasses
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


@pytest.mark.asyncio
async def test_non_python_files_skipped_by_default(workspace):
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # A genuine Python dead fn — must still be flagged
    (pkg / "code.py").write_text("def python_dead():\n    return 1\n")
    # A vendored JS file — its symbols must NOT appear in default output
    (pkg / "vendored.js").write_text(
        "function jsDead() {\n  return 1;\n}\n"
        "function jsAlsoDead() {\n  return 2;\n}\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        default_out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in default_out["dead_symbols"]}
        # Python dead is reported
        assert any("python_dead" in q for q in qnames)
        # JS dead is filtered out by default
        assert not any("jsDead" in q for q in qnames)
        assert not any("jsAlsoDead" in q for q in qnames)

        opted_in = (
            await c.call_tool(
                "find_dead_code", {"include_non_python": True}
            )
        ).data
        opted_qnames = {d["qualified_name"] for d in opted_in["dead_symbols"]}
        assert any("jsDead" in q for q in opted_qnames)


@pytest.mark.asyncio
async def test_dotted_path_string_protects_class(workspace):
    """Django-style settings: `INSTALLED_APPS = ['app.apps.MyConfig']`
    must keep `MyConfig` out of dead code."""
    apps = workspace / "myapp"
    apps.mkdir()
    (apps / "__init__.py").write_text("")
    (apps / "apps.py").write_text(
        "class MyConfig:\n"
        "    name = 'myapp'\n"
    )
    (workspace / "settings.py").write_text(
        "INSTALLED_APPS = [\n"
        "    'myapp.apps.MyConfig',\n"
        "]\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert "myapp.apps.MyConfig" not in qnames, (
            f"dotted-path string ref should protect MyConfig: {qnames}"
        )


@pytest.mark.asyncio
async def test_meta_inner_class_not_flagged_dead(workspace):
    """Django reflects on `class Meta:` inside model classes via the
    ModelBase metaclass — Meta has zero direct callers but is never dead."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text(
        "class Article:\n"
        "    class Meta:\n"
        "        ordering = ['title']\n"
        "    def some_method(self):\n"
        "        return 1\n"
        "\n"
        "x = Article()\n"  # Force Article to look used
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        assert "pkg.models.Article.Meta" not in qnames, (
            f"inner class Meta must not be flagged dead: {qnames}"
        )


@pytest.mark.asyncio
async def test_top_level_meta_class_still_flagged(workspace):
    """A `class Meta:` at module level (not inside another class) has no
    framework hook protecting it — it's a normal dead-code candidate."""
    pkg = workspace / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "loose.py").write_text(
        "class Meta:\n"  # standalone, no enclosing class
        "    pass\n"
    )
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_dead_code", {})).data
        qnames = {d["qualified_name"] for d in out["dead_symbols"]}
        # Top-level Meta IS dead. The inner-class hook only applies when
        # qname has at least 3 segments (module.Outer.Meta).
        assert "pkg.loose.Meta" in qnames
