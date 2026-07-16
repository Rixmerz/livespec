"""Spec markdown sync helpers: duplicate-spec scan + post-index import."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from livespec_mcp.domain.md_specs import parse_specs_markdown


def scan_duplicate_spec_markdown_specs(
    workspace: Path,
    *,
    exclude: Path | None = None,
    max_files: int = 200,
) -> list[dict[str, Any]]:
    """Find spec ids declared in more than one markdown file under ``workspace``."""
    by_spec: dict[str, list[str]] = {}
    scanned = 0
    for path in sorted(workspace.rglob("*.md")):
        if scanned >= max_files:
            break
        if any(part in {".git", ".venv", "venv", "node_modules"} for part in path.parts):
            continue
        try:
            rel = str(path.relative_to(workspace))
        except ValueError:
            continue
        if exclude is not None and path.resolve() == exclude.resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        # Reuse the real parser so this heuristic agrees with it — in
        # particular it skips `## SPEC-NNN:` headers shown inside ``` fenced
        # code blocks, which would otherwise raise phantom duplicate warnings.
        for spec in parse_specs_markdown(text):
            spec_id = spec.spec_id
            by_spec.setdefault(spec_id, [])
            if rel not in by_spec[spec_id]:
                by_spec[spec_id].append(rel)
    warnings: list[dict[str, Any]] = []
    for spec_id, paths in sorted(by_spec.items()):
        if len(paths) > 1:
            warnings.append(
                {
                    "spec_id": spec_id,
                    "paths": paths,
                    "message": (
                        f"{spec_id} appears in {len(paths)} markdown files — "
                        "keep a single canonical spec to avoid drift"
                    ),
                }
            )
    return warnings


def import_specs_from_markdown_file(
    st: Any,
    path: str | Path,
    *,
    check_duplicates: bool = True,
) -> dict[str, Any]:
    """Shared import logic for MCP tool and index post-hook."""
    pid = st.project_id
    p = Path(path)
    if not p.is_absolute():
        p = st.settings.workspace / p
    if not p.exists():
        raise FileNotFoundError(str(p))
    text = p.read_text(encoding="utf-8", errors="replace")
    parsed = parse_specs_markdown(text)
    created = 0
    updated = 0
    for pspec in parsed:
        module_id = None
        if pspec.module:
            row = st.conn.execute(
                "SELECT id FROM module WHERE project_id=? AND name=?", (pid, pspec.module)
            ).fetchone()
            if row:
                module_id = int(row["id"])
            else:
                cur = st.conn.execute(
                    "INSERT INTO module(project_id, name) VALUES(?,?)", (pid, pspec.module)
                )
                module_id = int(cur.lastrowid)
        existing = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (pid, pspec.spec_id)
        ).fetchone()
        if existing:
            st.conn.execute(
                """UPDATE spec SET title=?, description=?, status=?, priority=?,
                   module_id=?, kind=?, updated_at=datetime('now') WHERE id=?""",
                (
                    pspec.title,
                    pspec.description,
                    pspec.status,
                    pspec.priority,
                    module_id,
                    pspec.kind,
                    existing["id"],
                ),
            )
            updated += 1
        else:
            st.conn.execute(
                """INSERT INTO spec(project_id, spec_id, title, description, module_id, status, priority, kind)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    pid,
                    pspec.spec_id,
                    pspec.title,
                    pspec.description,
                    module_id,
                    pspec.status,
                    pspec.priority,
                    pspec.kind,
                ),
            )
            created += 1
    st.conn.commit()
    out: dict[str, Any] = {
        "source": str(p),
        "parsed": len(parsed),
        "created": created,
        "updated": updated,
    }
    if check_duplicates:
        dupes = scan_duplicate_spec_markdown_specs(st.settings.workspace, exclude=p)
        if dupes:
            out["duplicate_spec_warnings"] = dupes
    return out


def bulk_link_spec_symbols_impl(st: Any, mappings: list[dict[str, Any]]) -> dict[str, Any]:
    """Core bulk-link loop (shared with MCP tool)."""
    pid = st.project_id
    results: list[dict[str, Any]] = []
    n_linked = 0
    n_skipped = 0
    n_failed = 0
    for m in mappings:
        spec_id = m.get("spec_id")
        symbol_qname = m.get("symbol_qname")
        if not spec_id or not symbol_qname:
            results.append(
                {
                    "spec_id": spec_id,
                    "symbol_qname": symbol_qname,
                    "ok": False,
                    "linked": False,
                    "error": "spec_id and symbol_qname are required",
                }
            )
            n_failed += 1
            continue
        relation = m.get("relation", "implements")
        confidence = float(m.get("confidence", 1.0))
        source = m.get("source", "manual")
        spec = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (pid, spec_id)
        ).fetchone()
        if not spec:
            results.append(
                {
                    "spec_id": spec_id,
                    "symbol_qname": symbol_qname,
                    "ok": False,
                    "linked": False,
                    "error": f"Spec '{spec_id}' not found",
                }
            )
            n_failed += 1
            continue
        sym = st.conn.execute(
            """SELECT s.id, s.kind FROM symbol s JOIN file f ON f.id=s.file_id
               WHERE f.project_id=? AND s.qualified_name=? LIMIT 1""",
            (pid, symbol_qname),
        ).fetchone()
        if not sym:
            hint = None
            if symbol_qname.count(".") == 2 and symbol_qname.startswith("tests."):
                hint = (
                    "bulk_link expects a function/method qname, not a test module — "
                    "use tests.pkg.test_mod.test_fn (the test function symbol)"
                )
            results.append(
                {
                    "spec_id": spec_id,
                    "symbol_qname": symbol_qname,
                    "ok": False,
                    "linked": False,
                    "error": f"Symbol '{symbol_qname}' not found",
                    "hint": hint,
                }
            )
            n_failed += 1
            continue
        cur = st.conn.execute(
            """INSERT OR IGNORE INTO spec_symbol(spec_id, symbol_id, relation, confidence, source)
               VALUES(?,?,?,?,?)""",
            (int(spec["id"]), int(sym["id"]), relation, confidence, source),
        )
        linked = cur.rowcount > 0
        if linked:
            n_linked += 1
        else:
            n_skipped += 1
        results.append(
            {
                "spec_id": spec_id,
                "symbol_qname": symbol_qname,
                "ok": True,
                "linked": linked,
                "error": None,
            }
        )
    st.conn.commit()
    return {
        "linked": n_linked,
        "skipped": n_skipped,
        "failed": n_failed,
        "total": len(mappings),
        "results": results,
    }


def apply_links_seed(st: Any, seed_path: str | Path) -> dict[str, Any]:
    """Replay bulk_link mappings from a JSON seed file."""
    p = Path(seed_path)
    if not p.is_absolute():
        p = st.settings.workspace / p
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{p}: expected JSON list")
    mappings: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        spec_id = entry.get("spec_id")
        qname = entry.get("qname") or entry.get("symbol_qname")
        if not spec_id or not qname:
            continue
        mapping = {
            "spec_id": spec_id,
            "symbol_qname": qname,
            "relation": entry.get("relation", "implements"),
            "source": entry.get("source", "manual"),
        }
        # Preserve a per-entry confidence if the seed carries one — dropping it
        # forced every seeded link to 1.0 regardless of what was recorded.
        if "confidence" in entry:
            mapping["confidence"] = entry["confidence"]
        mappings.append(mapping)
    return bulk_link_spec_symbols_impl(st, mappings)


def sync_specs_from_config(st: Any) -> dict[str, Any] | None:
    """Run ``[specs].sync_from`` (+ optional ``links_seed``) from TOML."""
    from livespec_mcp.config import load_repo_config

    cfg = load_repo_config(st.settings.workspace)
    if not cfg.specs_sync_from and not cfg.specs_links_seed:
        return None
    result: dict[str, Any] = {"imports": [], "links": None}
    for rel in cfg.specs_sync_from:
        try:
            result["imports"].append(import_specs_from_markdown_file(st, rel))
        except FileNotFoundError as e:
            result["imports"].append({"path": rel, "error": str(e)})
    if cfg.specs_links_seed:
        try:
            result["links"] = apply_links_seed(st, cfg.specs_links_seed)
        except (FileNotFoundError, ValueError) as e:
            result["links"] = {"error": str(e)}
    return result
