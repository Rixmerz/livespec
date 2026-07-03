---
name: livespec-fastapi
description: >-
  Onboard and operate Livespec MCP on a FastAPI repo: index, mount Spec Explorer
  at /explorer, HTTP route discovery, Try-it, Spec traceability. Use when the
  project uses FastAPI, mentions /explorer, livespec-mcp, or Spec Explorer.
---

# Livespec + FastAPI

## When to trigger

- Repo has `FastAPI()` in `main.py` / `app.py`
- User wants `/explorer`, code intelligence, or Spec traceability on a Python API
- After `livespec-mcp fastapi init` was run in this repo

## Required: `workspace`

Pass `workspace="<absolute repo root>"` on **every** livespec tool. No env fallback.

## Setup (once per repo)

Prefer the installer:

```bash
livespec-mcp fastapi init /path/to/repo
```

Manual equivalent:

1. Add `.livespec.toml` with `[explorer] auto_mount = true` and `mount_path = "/explorer"`.
2. `index_project(workspace=..., explorer=True)`.
3. Ensure mount: autowire block in entry module **or** `enable_explorer(app)`.
4. `uvicorn main:app --reload` → open `/explorer/`.

## Agent session checklist

1. **Orient** — `get_project_overview(workspace=...)`; index if needed.
2. **HTTP surface** — `find_endpoints(framework="fastapi")`; confirm GET/POST paths.
3. **Refresh UI** — `export_explorer(workspace=...)` before demoing Explorer.
4. **Task work** — `find_symbol` → `quick_orient` → `analyze_impact` / `who_calls`.
5. **Specs** (if adopted) — `list_specs`, `get_spec_implementation(spec_id)`, `audit_coverage(summary_only=True)`.
6. **After edits** — `git_diff_impact(summary_only=True)`; use `payload_warning` if present.

## Explorer Try-it

API tab → set Base URL → **Execute** on a route. Requires app running; fix CORS in dev if blocked.

## Mount patterns

```python
from livespec_mcp.explorer import enable_explorer

app = FastAPI()
enable_explorer(app)  # reads [explorer].mount_path from .livespec.toml
```

Alternatives: `explorer_lifespan`, `LivespecExplorerMiddleware` — see `livespec_mcp.explorer.fastapi`.

## Do not

- Index `/Users/.../my` (parent of multiple repos).
- Assume PyPI — livespec-mcp is installed from source or internal package in this org.
