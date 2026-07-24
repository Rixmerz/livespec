"""Spec markdown sync helpers: duplicate-spec scan + post-index import."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from livespec_mcp.domain.md_specs import (
    detect_spec_format,
    parse_openspec_markdown,
    parse_specs_markdown,
)

# spec_symbol.relation is a free-text column, but the query surface only knows
# these three — a typo like 'implement' would store silently and be invisible
# to every relation='implements'/'tests'/'references' filter.
_VALID_RELATIONS = frozenset({"implements", "tests", "references"})


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


def _sync_spec_scenarios(
    conn: Any, spec_pk: int, scenarios: list[tuple[str, str]]
) -> None:
    """Reconcile a spec's ``spec_scenario`` rows against a parsed scenario list.

    No-op when ``scenarios`` is empty so a native ``## SPEC-NNN:`` re-import
    (which never carries scenarios) does not wipe scenarios that an OpenSpec
    import populated for the same spec_id.

    **Upsert, not replace:** scenarios are matched by ``(spec_id, name)`` and
    updated in place; scenarios no longer present are deleted. This preserves
    each scenario's ``id`` across re-imports so ``scenario_symbol`` traceability
    links survive a re-sync (a delete+reinsert would cascade them away)."""
    if not scenarios:
        return
    keep: set[str] = set()
    for ordinal, (name, body) in enumerate(scenarios):
        keep.add(name)
        row = conn.execute(
            "SELECT id FROM spec_scenario WHERE spec_id=? AND name=?", (spec_pk, name)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE spec_scenario SET body=?, ordinal=? WHERE id=?",
                (body, ordinal, int(row["id"])),
            )
        else:
            conn.execute(
                "INSERT INTO spec_scenario(spec_id, name, body, ordinal) VALUES(?,?,?,?)",
                (spec_pk, name, body, ordinal),
            )
    existing = conn.execute(
        "SELECT id, name FROM spec_scenario WHERE spec_id=?", (spec_pk,)
    ).fetchall()
    for r in existing:
        if r["name"] not in keep:
            conn.execute("DELETE FROM spec_scenario WHERE id=?", (int(r["id"]),))


def _parse_openspec_tree(root: Path) -> list:
    """Walk an OpenSpec directory (``openspec/`` or a ``specs/`` subtree) and
    parse every markdown file as OpenSpec format.

    Capability = the file's parent directory name (OpenSpec lays specs out as
    ``specs/<capability>/spec.md``). Later files win on spec_id collision so a
    re-import is deterministic. ``changes/`` deltas and ``archive/`` are
    included — both describe requirements that exist in the codebase.
    """
    parsed: list = []
    seen: dict[str, int] = {}  # spec_id -> index into parsed (last wins)
    for md in sorted(root.rglob("*.md")):
        if any(part in {".git", ".venv", "venv", "node_modules"} for part in md.parts):
            continue
        parent = md.parent.name
        capability = None if parent in {"specs", "openspec", root.name} else parent
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec in parse_openspec_markdown(text, capability=capability):
            if spec.spec_id in seen:
                parsed[seen[spec.spec_id]] = spec
            else:
                seen[spec.spec_id] = len(parsed)
                parsed.append(spec)
    return parsed


def import_specs_from_markdown_file(
    st: Any,
    path: str | Path,
    *,
    fmt: str = "auto",
    check_duplicates: bool = True,
) -> dict[str, Any]:
    """Shared import logic for MCP tool and index post-hook.

    ``fmt`` selects the source dialect: ``"livespec"`` (native
    ``## SPEC-NNN:`` headers), ``"openspec"`` (Fission-AI OpenSpec
    ``### Requirement:`` anchors), or ``"auto"`` (default — sniff per file,
    and treat a directory ``path`` as an OpenSpec tree).
    """
    pid = st.project_id
    p = Path(path)
    if not p.is_absolute():
        p = st.settings.workspace / p
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.is_dir():
        # A directory is only meaningful as an OpenSpec tree — the native
        # format is single-file.
        parsed = _parse_openspec_tree(p)
    else:
        text = p.read_text(encoding="utf-8", errors="replace")
        chosen = detect_spec_format(text) if fmt == "auto" else fmt
        parsed = (
            parse_openspec_markdown(text)
            if chosen == "openspec"
            else parse_specs_markdown(text)
        )
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
            # Preserve the capability's ## Purpose on the module so export can
            # re-emit it verbatim (round-trip fidelity) instead of synthesizing.
            if pspec.capability_purpose:
                st.conn.execute(
                    "UPDATE module SET description=? WHERE id=?",
                    (pspec.capability_purpose, module_id),
                )
        existing = st.conn.execute(
            "SELECT id FROM spec WHERE project_id=? AND spec_id=?", (pid, pspec.spec_id)
        ).fetchone()
        if existing:
            spec_pk = int(existing["id"])
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
                    spec_pk,
                ),
            )
            updated += 1
        else:
            cur = st.conn.execute(
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
            spec_pk = int(cur.lastrowid)
            created += 1
        _sync_spec_scenarios(st.conn, spec_pk, pspec.scenarios)
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
        if relation not in _VALID_RELATIONS:
            results.append(
                {
                    "spec_id": spec_id,
                    "symbol_qname": symbol_qname,
                    "ok": False,
                    "linked": False,
                    "error": (
                        f"invalid relation '{relation}' — must be one of "
                        f"{sorted(_VALID_RELATIONS)}"
                    ),
                }
            )
            n_failed += 1
            continue
        try:
            confidence = float(m.get("confidence", 1.0))
        except (TypeError, ValueError):
            results.append(
                {
                    "spec_id": spec_id,
                    "symbol_qname": symbol_qname,
                    "ok": False,
                    "linked": False,
                    "error": f"confidence must be a number, got {m.get('confidence')!r}",
                }
            )
            n_failed += 1
            continue
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
        # Group-aware resolution: home project first, then the rest of a
        # shared group DB (ungrouped → home only, unchanged).
        sym = st.resolve_symbol(symbol_qname)
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
    if (
        not cfg.specs_sync_from
        and not cfg.specs_links_seed
        and not cfg.specs_openspec_dir
    ):
        return None
    result: dict[str, Any] = {"imports": [], "links": None}
    for rel in cfg.specs_sync_from:
        try:
            result["imports"].append(import_specs_from_markdown_file(st, rel))
        except FileNotFoundError as e:
            result["imports"].append({"path": rel, "error": str(e)})
    if cfg.specs_openspec_dir:
        from livespec_mcp.domain.openspec_discover import (
            discover_openspec_root,
            sync_openspec_tree,
        )

        root = discover_openspec_root(st.settings.workspace, cfg.specs_openspec_dir)
        if root is None:
            result["openspec"] = {
                "path": cfg.specs_openspec_dir,
                "error": "OpenSpec directory not found",
            }
        else:
            result["openspec"] = sync_openspec_tree(st, root)
    if cfg.specs_links_seed:
        try:
            result["links"] = apply_links_seed(st, cfg.specs_links_seed)
        except (FileNotFoundError, ValueError) as e:
            result["links"] = {"error": str(e)}
    return result
