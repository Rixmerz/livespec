"""Experimental: a revisioned history of the index, kept in a Kerno database.

SQLite stays the source of truth for every query livespec answers. This module
mirrors the *semantic* state of the index into a Kerno database after each
index run, so that what SQLite overwrites is kept:

* one Kerno unit per symbol (keyed by project, file and qualified name). A new
  revision is written only when the symbol's ``body_hash``, ``signature_hash``
  or kind changes -- reformatting and line shifts do not add revisions, the
  same invariance ``body_hash`` already guarantees;
* a symbol that disappears from the index is deleted in Kerno with a tombstone
  revision, so its history survives the deletion;
* call edges are Kerno relations (``calls``), diffed against the index;
* one ``livespec_sync`` unit whose revisions are the sync log: each revision
  records the index run it mirrors, and its ``commit_seq`` is the point an
  as-of read uses to reconstruct a symbol "as of index run N";
* every sync is a single atomic Kerno change set, and the store can be checked
  end to end with Kerno's ``verify``.

Enabled only when ``LIVESPEC_KERNO=1`` and the optional ``kernodb`` package is
installed (``pip install 'livespec[kerno]'``). Failures are returned as data;
an index run never fails because of this mirror.

The database lives beside ``docs.db`` in ``.mcp-docs/kerno/`` (the index
directory livespec already owns), never anywhere else in the project.
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SYMBOL_TYPE = "livespec_symbol"
SYNC_TYPE = "livespec_sync"
CALLS = "calls"
#: Payload fields whose change is a semantic change (a new revision).
_SEMANTIC = ("kind", "signature", "signature_hash", "body_hash")


@dataclass(frozen=True)
class KernoProblem:
    """A failure to report, shaped into ``mcp_error`` by the tools layer."""

    message: str
    hint: str | None = None


def enabled() -> bool:
    return os.environ.get("LIVESPEC_KERNO", "").strip().lower() in {"1", "true", "yes", "on"}


def _kerno():
    try:
        import kerno  # type: ignore[import-not-found]
    except ImportError:
        return None
    return kerno


def store_path(db_path: Path) -> Path:
    return Path(db_path).parent / "kerno"


# One handle per directory: Kerno allows a single open handle per process.
_handles: dict[Path, Any] = {}
_handles_lock = threading.Lock()


def open_store(db_path: Path) -> Any | KernoProblem:
    kerno = _kerno()
    if kerno is None:
        return KernoProblem(
            "the kernodb package is not installed",
            hint="pip install --pre 'livespec[kerno]' (or pip install --pre kernodb)",
        )
    path = store_path(db_path)
    with _handles_lock:
        db = _handles.get(path)
        if db is None:
            try:
                db = kerno.open(path)
            except Exception as e:  # KernoError: locked by another process, damaged, ...
                return KernoProblem(f"could not open the Kerno store at {path}: {e}")
            _handles[path] = db
        return db


def close_all() -> None:
    with _handles_lock:
        for db in _handles.values():
            try:
                db.close()
            except Exception:
                pass
        _handles.clear()


atexit.register(close_all)


def symbol_key(project_id: int, path: str, qname: str) -> str:
    return f"{project_id}:{path}:{qname}"


def _current_symbols(conn: sqlite3.Connection, project_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT s.id, s.qualified_name, s.kind, s.signature, s.signature_hash,
                  s.body_hash, s.start_line, s.end_line, f.path
           FROM symbol s JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?
           ORDER BY f.path, s.start_line, s.id""",
        (project_id,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}
    for r in rows:
        base = symbol_key(project_id, r["path"], r["qualified_name"])
        # Same qname twice in one file (Rust `impl X` blocks, if/else shims):
        # the n-th occurrence in source order gets `#n`, so none is dropped.
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        out[key] = {
            "key": key,
            "sqlite_id": r["id"],
            "project_id": project_id,
            "qname": r["qualified_name"],
            "file": r["path"],
            "kind": r["kind"],
            "signature": r["signature"],
            "signature_hash": r["signature_hash"],
            "body_hash": r["body_hash"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
        }
    return out


def _current_edges(conn: sqlite3.Connection, project_id: int) -> set[tuple[int, int]]:
    rows = conn.execute(
        """SELECT DISTINCT e.src_symbol_id, e.dst_symbol_id
           FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ? AND e.edge_type = 'calls'""",
        (project_id,),
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def _decode(payload: bytes | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except ValueError:
        return {}


def _stored(db: Any, project_id: int) -> dict[str, tuple[str, dict[str, Any]]]:
    """key -> (unit id, payload) for the active symbol units of the project."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for u in db.query(SYMBOL_TYPE):
        p = _decode(u.payload)
        if p.get("project_id") == project_id and "key" in p:
            out[p["key"]] = (u.id, p)
    return out


def _sync_unit(db: Any, project_id: int) -> tuple[str | None, dict[str, Any]]:
    for u in db.query(SYNC_TYPE):
        p = _decode(u.payload)
        if p.get("project_id") == project_id:
            return u.id, p
    return None, {}


def _dump(d: dict[str, Any]) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def sync(conn: sqlite3.Connection, project_id: int, db_path: Path) -> dict[str, Any] | KernoProblem:
    """Mirror the index of ``project_id`` into the Kerno store, atomically."""
    db = open_store(db_path)
    if isinstance(db, KernoProblem):
        return db
    current = _current_symbols(conn, project_id)
    stored = _stored(db, project_id)
    run = conn.execute(
        "SELECT id FROM index_run WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
    ).fetchone()
    run_id = int(run[0]) if run else None

    batched = hasattr(db, "last_commit_seq")  # kernodb with relations inside a batch
    ops: list[tuple[Any, ...]] = []
    new_keys: list[str] = []
    changed = 0
    for key, sym in current.items():
        payload = {k: v for k, v in sym.items() if k != "sqlite_id"}
        payload["index_run"] = run_id
        if key not in stored:
            ops.append(("store", SYMBOL_TYPE, _dump(payload)))
            new_keys.append(key)
        else:
            uid, old = stored[key]
            if any(old.get(f) != payload.get(f) for f in _SEMANTIC):
                ops.append(("update", uid, _dump(payload)))
                changed += 1
    removed_ids = {uid for key, (uid, _) in stored.items() if key not in current}

    # Units new in this sync are addressed by the index of their "store" op.
    store_index = {key: i for i, key in enumerate(new_keys)}

    def ref(key: str) -> Any:
        return store_index[key] if key in store_index else stored[key][0]

    key_of = {sym["sqlite_id"]: key for key, sym in current.items()}
    wanted_keys = {
        (key_of[a], key_of[b])
        for a, b in _current_edges(conn, project_id)
        if a in key_of and b in key_of
    }
    have: dict[tuple[str, str], str] = {}
    if hasattr(db, "relations"):  # one scan instead of one call per unit
        project_units = {uid for uid, _ in stored.values()} - removed_ids
        for rel in db.relations(CALLS):
            if rel.from_id in project_units:
                have[(rel.from_id, rel.to_id)] = rel.id
    else:
        for uid, _ in stored.values():
            if uid in removed_ids:
                continue
            for rel in db.related(uid, "out", CALLS):
                have[(rel.from_id, rel.to_id)] = rel.id
    wanted_existing = {
        (stored[a][0], stored[b][0])
        for a, b in wanted_keys
        if a not in store_index and b not in store_index
    }
    stale = [rid for pair, rid in have.items() if pair not in wanted_existing]
    to_add = [
        (a, b)
        for a, b in wanted_keys
        if a in store_index or b in store_index or (stored[a][0], stored[b][0]) not in have
    ]

    if batched:
        ops.extend(("unrelate", rid) for rid in stale)
        ops.extend(("delete_cascade", uid) for uid in removed_ids)
        ops.extend(("relate", ref(a), ref(b), CALLS) for a, b in to_add)
    else:
        # Older bindings: unrelate (once) before delete, relations after the commit.
        doomed: dict[str, None] = {}
        for uid in removed_ids:
            for rel in db.related(uid, "out", CALLS) + db.related(uid, "in", CALLS):
                doomed[rel.id] = None
        ops.extend(("unrelate", rid) for rid in doomed)
        ops.extend(("unrelate", rid) for rid in stale if rid not in doomed)
        ops.extend(("delete", uid) for uid in removed_ids)

    sync_id, sync_state = _sync_unit(db, project_id)
    # index_run ids restart when docs.db is rebuilt: a run id that does not
    # grow starts a new generation, and as-of reads stay inside one.
    generation = int(sync_state.get("generation", 1))
    last_run = sync_state.get("index_run")
    if last_run is not None and run_id is not None and run_id <= last_run:
        generation += 1
    log = {
        "project_id": project_id,
        "index_run": run_id,
        "generation": generation,
        "symbols": len(current),
    }
    # The sync log goes last, so the indexes of the symbol "store" ops stay 0..n-1.
    if sync_id is None:
        ops.append(("store", SYNC_TYPE, _dump(log)))
    else:
        ops.append(("update", sync_id, _dump(log)))

    if batched and len(ops) > MAX_BATCH_OPS:
        stored_ids, change_sets = _commit_chunked(db, ops)
    else:
        stored_ids, change_sets = db.commit(ops), 1
    if not batched:
        new_ids = dict(zip(new_keys, stored_ids, strict=False))
        for a, b in to_add:
            src = new_ids.get(a) or stored[a][0]
            dst = new_ids.get(b) or stored[b][0]
            db.relate(src, dst, CALLS)
    added = len(to_add)
    removed = len(removed_ids)

    return {
        "store": str(store_path(db_path)),
        "index_run": run_id,
        "symbols": len(current),
        "symbols_added": len(new_keys),
        "symbols_changed": changed,
        "symbols_removed": removed,
        "edges_added": added,
        "edges_removed": len(stale),
        "first_sync": not sync_state,
        "generation": generation,
        "atomic": batched and change_sets == 1,
        "change_sets": change_sets,
    }


#: Kerno's default limit of operations per change set.
MAX_BATCH_OPS = 10_000


def _commit_chunked(db: Any, ops: list[tuple[Any, ...]]) -> tuple[list[str], int]:
    """Commit more ops than one Kerno change set holds.

    Stores go first (their ids resolve the batch-local indexes of "relate"
    ops), then everything else; the last op (the sync log) is in the final
    change set, so a sync interrupted in between does not record its run.
    """
    stores = [op for op in ops if op[0] == "store" and op[1] == SYMBOL_TYPE]
    rest = [op for op in ops if not (op[0] == "store" and op[1] == SYMBOL_TYPE)]
    ids: list[str] = []
    change_sets = 0
    for i in range(0, len(stores), MAX_BATCH_OPS):
        ids.extend(db.commit(stores[i : i + MAX_BATCH_OPS]))
        change_sets += 1

    def resolve(end: Any) -> Any:
        return ids[end] if isinstance(end, int) else end

    rest = [
        (op[0], resolve(op[1]), resolve(op[2]), op[3])
        if op[0] in ("relate", "relate_unique")
        else op
        for op in rest
    ]
    tail: list[str] = []
    for i in range(0, len(rest), MAX_BATCH_OPS):
        tail = db.commit(rest[i : i + MAX_BATCH_OPS])
        change_sets += 1
    return ids + tail, change_sets


def _find_units(db: Any, project_id: int, qname: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (uid, p) for key, (uid, p) in _stored(db, project_id).items() if p.get("qname") == qname
    ]


def symbol_revisions(
    project_id: int, db_path: Path, qname: str, limit: int = 20
) -> dict[str, Any] | KernoProblem:
    db = open_store(db_path)
    if isinstance(db, KernoProblem):
        return db
    units = _find_units(db, project_id, qname)
    if not units:
        return KernoProblem(
            f"no symbol {qname!r} in the Kerno history store",
            hint="run index_project with LIVESPEC_KERNO=1 first; removed symbols are not "
            "looked up by name",
        )
    out = []
    for uid, current in units:
        revs = []
        for r in db.history(uid, limit=limit, with_payload=True):
            p = _decode(r.payload)
            revs.append(
                {
                    "revision": r.revision,
                    "kind": r.kind,
                    "commit_seq": r.commit_seq,
                    "index_run": p.get("index_run"),
                    "signature": p.get("signature"),
                    "body_hash": p.get("body_hash"),
                    "lines": [p.get("start_line"), p.get("end_line")],
                    "content_hash": r.content_hash,
                }
            )
        out.append({"unit_id": uid, "file": current.get("file"), "revisions": revs})
    return {"qname": qname, "units": out}


def _seq_for_run(db: Any, project_id: int, run_id: int) -> int | None:
    sync_id, _ = _sync_unit(db, project_id)
    if sync_id is None:
        return None
    best: int | None = None
    revs = db.history(sync_id, limit=10_000, with_payload=True)
    if not revs:
        return None
    current_gen = _decode(revs[0].payload).get("generation", 1)
    for r in revs:
        p = _decode(r.payload)
        run = p.get("index_run")
        if p.get("generation", 1) == current_gen and run is not None and run <= run_id:
            best = max(best or 0, r.commit_seq)
    return best


def symbol_as_of(
    project_id: int, db_path: Path, qname: str, index_run: int
) -> dict[str, Any] | KernoProblem:
    db = open_store(db_path)
    if isinstance(db, KernoProblem):
        return db
    seq = _seq_for_run(db, project_id, index_run)
    if seq is None:
        return KernoProblem(
            f"no Kerno sync at or before index run {index_run}",
            hint="the history starts at the first index_project run with LIVESPEC_KERNO=1",
        )
    units = _find_units(db, project_id, qname)
    if not units:
        return KernoProblem(f"no symbol {qname!r} in the Kerno history store")
    states = []
    for uid, p in units:
        r = db.read_as_of(uid, seq)
        if r is None:
            states.append({"file": p.get("file"), "state": "absent"})
            continue
        old = _decode(r.payload)
        states.append(
            {
                "file": p.get("file"),
                "state": "deleted" if r.kind == "delete" else "present",
                "revision": r.revision,
                "index_run": old.get("index_run"),
                "signature": old.get("signature"),
                "body_hash": old.get("body_hash"),
                "lines": [old.get("start_line"), old.get("end_line")],
                "same_as_now": old.get("body_hash") == p.get("body_hash")
                and old.get("signature_hash") == p.get("signature_hash"),
            }
        )
    return {"qname": qname, "index_run": index_run, "commit_seq": seq, "states": states}


def verify(db_path: Path) -> dict[str, Any] | KernoProblem:
    db = open_store(db_path)
    if isinstance(db, KernoProblem):
        return db
    report = dict(db.verify())
    report["store"] = str(store_path(db_path))
    return report
