"""Flask routes in find_endpoints: every method, blueprint prefix, real framework."""

from __future__ import annotations

import pytest
from fastmcp import Client

from livespec_mcp.server import mcp


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


async def _endpoints(workspace):
    async with Client(mcp) as c:
        await c.call_tool("index_project", {})
        out = (await c.call_tool("find_endpoints", {})).data
    return {e["qualified_name"]: e for e in out["endpoints"]}


def _flask_app(workspace, *, register="app.register_blueprint(auth_bp, url_prefix='/auth')"):
    _write(workspace, "app/__init__.py", "")
    _write(
        workspace,
        "app/auth/__init__.py",
        "from flask import Blueprint\nbp = Blueprint('auth', __name__)\nfrom app.auth import routes\n",
    )
    _write(
        workspace,
        "app/auth/routes.py",
        "from app.auth import bp\n\n"
        "@bp.route('/login', methods=['GET', 'POST'])\n"
        "def login():\n    return 'x'\n\n"
        "@bp.route('/logout')\n"
        "def logout():\n    return 'x'\n",
    )
    _write(
        workspace,
        "app/factory.py",
        "from flask import Flask\nfrom app.auth import bp as auth_bp\n\n"
        f"def create_app():\n    app = Flask(__name__)\n    {register}\n    return app\n",
    )


@pytest.mark.asyncio
async def test_all_methods_prefix_and_framework_from_registration(workspace):
    _flask_app(workspace)
    eps = await _endpoints(workspace)
    login = eps["app.auth.routes.login"]
    assert login["http_methods"] == ["GET", "POST"]
    assert login["http_path"] == "/auth/login"
    assert login["http_path_prefix"] == "/auth"
    # the file imports nothing from flask: the framework comes from `bp`'s origin
    assert login["http_framework"] == "flask"
    assert eps["app.auth.routes.logout"]["http_methods"] == ["GET"]
    assert eps["app.auth.routes.logout"]["http_path"] == "/auth/logout"


@pytest.mark.asyncio
async def test_blueprint_own_url_prefix_is_used_without_registration_prefix(workspace):
    _flask_app(workspace, register="app.register_blueprint(auth_bp)")
    (workspace / "app/auth/__init__.py").write_text(
        "from flask import Blueprint\nbp = Blueprint('auth', __name__, url_prefix='/own')\n"
    )
    eps = await _endpoints(workspace)
    assert eps["app.auth.routes.login"]["http_path"] == "/own/login"


@pytest.mark.asyncio
async def test_registration_prefix_overrides_blueprint_prefix(workspace):
    _flask_app(workspace)
    (workspace / "app/auth/__init__.py").write_text(
        "from flask import Blueprint\nbp = Blueprint('auth', __name__, url_prefix='/own')\n"
    )
    eps = await _endpoints(workspace)
    assert eps["app.auth.routes.login"]["http_path"] == "/auth/login"


@pytest.mark.asyncio
async def test_ambiguous_or_missing_prefix_leaves_path_untouched(workspace):
    # registered twice under different prefixes -> not statically one path
    _flask_app(
        workspace,
        register=(
            "app.register_blueprint(auth_bp, url_prefix='/a')\n"
            "    app.register_blueprint(auth_bp, url_prefix='/b')"
        ),
    )
    eps = await _endpoints(workspace)
    login = eps["app.auth.routes.login"]
    assert login["http_path"] == "/login"
    assert "http_path_prefix" not in login


@pytest.mark.asyncio
async def test_non_literal_prefix_and_unregistered_blueprint_add_nothing(workspace):
    _flask_app(workspace, register="app.register_blueprint(auth_bp, url_prefix=PREFIX)")
    eps = await _endpoints(workspace)
    assert eps["app.auth.routes.login"]["http_path"] == "/login"
    assert "http_path_prefix" not in eps["app.auth.routes.login"]


@pytest.mark.asyncio
async def test_fastapi_label_follows_the_imported_framework(workspace):
    _write(
        workspace,
        "svc/api.py",
        "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
        "@router.get('/ping')\ndef ping():\n    return 1\n",
    )
    _write(
        workspace,
        "svc/flask_side.py",
        "import flask\napp = flask.Flask(__name__)\n\n@app.route('/x')\ndef x():\n    return 1\n",
    )
    eps = await _endpoints(workspace)
    assert eps["svc.api.ping"]["http_framework"] == "fastapi"
    assert eps["svc.flask_side.x"]["http_framework"] == "flask"


@pytest.mark.asyncio
async def test_unknown_framework_is_not_guessed(workspace):
    _write(workspace, "m.py", "from lib import bp\n\n@bp.route('/z')\ndef z():\n    return 1\n")
    eps = await _endpoints(workspace)
    assert eps["m.z"]["http_path"] == "/z"
    assert "http_framework" not in eps["m.z"]
