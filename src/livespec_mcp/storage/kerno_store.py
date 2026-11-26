"""EXPERIMENTAL: a Kerno (kernodb) mirror of the code index, for the versus.

Branch-only experiment -- see docs/experiments/kernodb-versus.md. SQLite stays
the source of truth: the indexer computes everything in SQL (ref resolution is
`INSERT ... SELECT` over joins) and ~700 call sites read it with raw SQL, so a
drop-in swap is not possible. What this module does instead is the part a
units/relations store *can* take over:

    symbol  -> unit (typed document, `sid` unique = the SQLite symbol.id)
    edge    -> relation symbol -> symbol (type encodes edge_type/weight/origin,
               because a Kerno relation carries no payload)
    chunk   -> unit with the keyword (BM25) and substring (trigram) indexes on

`sync()` brings the mirror to the SQLite state incrementally (upsert by `sid`,
delete what is gone, diff the relations). Readers then answer the hot read
paths -- graph load, symbol lookup, callers, search -- from Kerno alone.

Opt-in, never on by default: `LIVESPEC_EXPERIMENTAL_KERNO=1` makes
`index_project` sync the mirror and `load_graph` read from it (falling back to
SQL whenever the mirror is missing, stale or locked). `kernodb` is an optional
dependency (`pip install --pre kernodb`); without it everything here is a
no-op.
"""

from __future__ import annotations

import atexit
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

try:  # optional dependency
    import kerno
except ImportError:  # pragma: no cover - exercised only without the extra
    kerno = None  # type: ignore[assignment]

ENV_FLAG = "LIVESPEC_EXPERIMENTAL_KERNO"

# An indexed text value holds at most 254 bytes in Kerno. Qualified names and
# paths can exceed that, so the indexed copy is truncated and the full value
# rides in a second, unindexed field.
_KEY_MAX = 240
_BATCH = 5000
_EDGE_PREFIX = "e:"

_SYMBOL_SCHEMA = [
    {"name": "sid", "type": "int", "unique": True},
    {"name": "name", "type": "text", "indexed": True},
    {"name": "qkey", "type": "text", "indexed": True},
    {"name": "fkey", "type": "text", "indexed": True},
    {"name": "qname", "type": "text"},
    {"name": "file_path", "type": "text"},
    {"name": "kind", "type": "text"},
    {"name": "start_line", "type": "int"},
    {"name": "end_line", "type": "int"},
    {"name": "body_hash", "type": "text"},
]
_META_SCHEMA = [
    {"name": "key", "type": "text", "unique": True},
    {"name": "run_id", "type": "int"},
    {"name": "fp", "type": "text"},
]


def _doc(db: Any, unit: Any) -> dict[str, Any]:
    """A unit's document. `Unit.document()` decodes the payload the unit already
    carries; older kernodb builds need a second read of the database."""
    if hasattr(unit, "document"):
        return unit.document()
    return db.read_document(unit.id)


def enabled() -> bool:
    return kerno is not None and os.environ.get(ENV_FLAG, "") not in ("", "0")


def mirror_path(db_path: str | Path, project_id: int) -> Path:
    return Path(f"{db_path}.kerno") / f"p{project_id}"


def _key(value: str) -> str:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= _KEY_MAX:
        return value
    return raw[:_KEY_MAX].decode("utf-8", errors="ignore")


def _edge_type(edge_type: str, weight: float, origin: str | None) -> str:
    """Relation type for an edge. Kerno relations have no payload, so anything
    beyond the plain `(type, weight=1.0, origin=livespec)` case is encoded in
    the type name (64-byte limit: a long origin tag is truncated)."""
    origin = origin or "livespec"
    if weight == 1.0 and origin == "livespec":
        return _EDGE_PREFIX + edge_type
    return f"{_EDGE_PREFIX}{edge_type}~{weight!r}~{origin}"[:64]


def _parse_edge_type(rel_type: str) -> tuple[str, float, str]:
    parts = rel_type[len(_EDGE_PREFIX) :].split("~", 2)
    if len(parts) == 3:
        return parts[0], float(parts[1]), parts[2]
    return parts[0], 1.0, "livespec"


def _symbol_doc(r: sqlite3.Row) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "sid": int(r["id"]),
        "name": _key(r["name"]),
        "qkey": _key(r["qualified_name"]),
        "fkey": _key(r["path"]),
        "qname": r["qualified_name"],
        "file_path": r["path"],
        "kind": r["kind"],
        "start_line": int(r["start_line"]),
        "end_line": int(r["end_line"]),
    }
    if r["body_hash"]:
        doc["body_hash"] = r["body_hash"]
    return doc


def _chunk_payload(r: sqlite3.Row) -> str:
    """A chunk as a RAW text payload: one tab-separated header line, then the text.

    Not a typed document on purpose. In kernodb 1.0.0a4 the keyword index
    silently skips a payload that is not valid UTF-8, and a document's binary
    encoding stops being valid UTF-8 as soon as an int field is >= 128 or a
    text field is >= 128 bytes long -- measured here: 5% of the chunks were
    searchable as documents. A raw `str` payload is indexed in full. The price
    is no schema, so no `upsert` by natural key: sync keeps its own
    `cid -> unit` map from the header line."""
    head = "\t".join(
        str(v if v is not None else "")
        for v in (
            r["id"],
            r["source_type"],
            r["source_id"],
            r["text_kind"],
            r["start_line"],
            r["end_line"],
            (r["file_path"] or "").replace("\t", " ").replace("\n", " "),
        )
    )
    return head + "\n" + r["text"]


def _parse_chunk(payload: bytes) -> dict[str, Any]:
    head, _, text = payload.decode("utf-8", errors="replace").partition("\n")
    cid, source_type, source_id, text_kind, start, end, file_path = head.split("\t", 6)
    return {
        "chunk_id": int(cid),
        "source_type": source_type,
        "source_id": int(source_id) if source_id else None,
        "text_kind": text_kind,
        "start_line": int(start) if start else None,
        "end_line": int(end) if end else None,
        "file_path": file_path or None,
        "text": text,
    }


def fingerprint(conn: sqlite3.Connection, project_id: int, run_id: int) -> str:
    """What the mirror must match to be served: the run AND the edge set.

    `run_id` alone is not enough. livespec mutates `symbol_edge` outside index
    runs (external-graph ingest, route edges resolved when a sibling project of
    a group DB is indexed, origin reclaims), and the SQL loader sees those at
    once. Found by running the test suite under the flag: 9 tests read a stale
    mirror when freshness was keyed on the run id only."""
    r = conn.execute(
        """SELECT COUNT(*) n, COALESCE(MAX(e.id), 0) mx, COALESCE(SUM(e.weight), 0) w,
                  COALESCE(SUM(e.origin = 'livespec'), 0) own
           FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?""",
        (project_id,),
    ).fetchone()
    return f"{run_id}:{r['n']}:{r['mx']}:{r['w']!r}:{r['own']}"


class KernoMirror:
    """One Kerno database mirroring one project of one SQLite index."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        if kerno is None:
            raise RuntimeError("kernodb is not installed (pip install --pre kernodb)")
        self.path = Path(path)
        if not read_only:
            self.path.mkdir(parents=True, exist_ok=True)
        self.db = kerno.open(str(self.path), read_only=read_only, max_batch_ops=_BATCH * 2)
        if not read_only:
            self.db.define_schema("symbol", _SYMBOL_SCHEMA)
            self.db.define_schema("meta", _META_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> KernoMirror:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- write: bring the mirror to the SQLite state ----------

    def _existing(self, unit_type: str, field: str) -> dict[int, str]:
        """`{natural key: unit id}` of every active unit of a type.

        `Unit.document()` decodes the payload `query()` already returned; without
        it (kernodb <= 1.0.0a4) this is one extra `read_document` per unit."""
        return {int(_doc(self.db, u)[field]): u.id for u in self.db.query(unit_type)}

    def _upsert_all(
        self, unit_type: str, field: str, docs: list[dict[str, Any]], reason: str
    ) -> tuple[dict[int, str], dict[str, int]]:
        ids: dict[int, str] = {}
        outcomes = {"created": 0, "updated": 0, "unchanged": 0}
        # A change set is bounded in ops AND in bytes, and the keyword and
        # trigram index entries of a unit count against both (5,000 Django
        # chunks became 90,997 engine ops / 64 MiB). The caller cannot know
        # the expansion up front, so halve the batch until it fits.
        pending = [docs[i : i + _BATCH] for i in range(0, len(docs), _BATCH)]
        pending.reverse()
        while pending:
            part = pending.pop()
            try:
                out = self.db.commit(
                    [("upsert", unit_type, field, d) for d in part],
                    reason=reason,
                    actor="livespec-indexer",
                    detailed=True,
                )
            except kerno.ChangeSetTooLargeError:
                if len(part) == 1:
                    raise
                mid = len(part) // 2
                pending += [part[mid:], part[:mid]]
                continue
            for d, res in zip(part, out["upserts"], strict=True):
                ids[int(d[field])] = res["id"]
                outcomes[res["outcome"]] += 1
        return ids, outcomes

    def _commit_split(self, ops: list[tuple], reason: str) -> None:
        """Commit `ops` in change sets that fit (see `_upsert_all` on why halving)."""
        pending = [ops[i : i + _BATCH] for i in range(0, len(ops), _BATCH)]
        pending.reverse()
        while pending:
            part = pending.pop()
            try:
                self.db.commit(part, reason=reason, actor="livespec-indexer")
            except kerno.ChangeSetTooLargeError:
                if len(part) == 1:
                    raise
                mid = len(part) // 2
                pending += [part[mid:], part[:mid]]

    def _delete_all(self, unit_ids: list[str], reason: str) -> None:
        for i in range(0, len(unit_ids), _BATCH):
            self.db.commit(
                [("delete_cascade", u) for u in unit_ids[i : i + _BATCH]],
                reason=reason,
                actor="livespec-indexer",
            )

    def sync(
        self, conn: sqlite3.Connection, project_id: int, *, run_id: int, chunks: bool = True
    ) -> dict[str, Any]:
        """Make the mirror equal to the SQLite index of `project_id`.

        Not atomic as a whole (a Kerno change set holds a bounded number of
        ops), so the `meta` unit carrying `run_id` is written LAST: a reader
        that sees a `run_id` other than SQLite's latest falls back to SQL.
        """
        t0 = time.perf_counter()
        reason = f"index run {run_id}"
        stats: dict[str, Any] = {}

        # -- symbols
        rows = conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.start_line, s.end_line,
                      s.body_hash, f.path
               FROM symbol s JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (project_id,),
        ).fetchall()
        before = self._existing("symbol", "sid")
        docs = [_symbol_doc(r) for r in rows]
        sym_ids, outcomes = self._upsert_all("symbol", "sid", docs, reason)
        gone = [uid for sid, uid in before.items() if sid not in sym_ids]
        self._delete_all(gone, reason)
        stats["symbols"] = {**outcomes, "deleted": len(gone)}

        # -- edges: diff the live relations against the SQL edge set
        want: set[tuple[str, str, str]] = set()
        cross = 0
        for r in conn.execute(
            """SELECT e.src_symbol_id, e.dst_symbol_id, e.edge_type, e.weight, e.origin
               FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (project_id,),
        ):
            src = sym_ids.get(int(r["src_symbol_id"]))
            dst = sym_ids.get(int(r["dst_symbol_id"]))
            if src and dst:
                want.add((src, dst, _edge_type(r["edge_type"], float(r["weight"]), r["origin"])))
            else:  # dst lives in another project of a group DB: not mirrored
                cross += 1
        have = {
            (rel.from_id, rel.to_id, rel.relation_type): rel.id
            for rel in self.db.relations()
            if rel.relation_type.startswith(_EDGE_PREFIX)
        }
        ops: list[tuple] = [("unrelate", rid) for key, rid in have.items() if key not in want]
        removed = len(ops)
        ops += [("relate", *key) for key in want if key not in have]
        for i in range(0, len(ops), _BATCH):
            self.db.commit(ops[i : i + _BATCH], reason=reason, actor="livespec-indexer")
        stats["edges"] = {
            "added": len(ops) - removed,
            "removed": removed,
            "total": len(want),
            "cross_project_skipped": cross,
        }

        # -- chunks (keyword + substring indexes)
        if chunks:
            if not self.db.fulltext("chunk"):
                self.db.set_fulltext("chunk", True)
            if not self.db.substring("chunk"):
                self.db.set_substring("chunk", True)
            crows = conn.execute(
                """SELECT id, source_type, source_id, text_kind, file_path, start_line,
                          end_line, text
                   FROM chunk WHERE project_id = ?""",
                (project_id,),
            )
            cbefore = {
                int(u.payload.split(b"\t", 1)[0]): (u.id, u.payload) for u in self.db.query("chunk")
            }
            cops: list[tuple] = []
            counts = {"created": 0, "updated": 0, "unchanged": 0}
            seen: set[int] = set()
            for r in crows:
                cid = int(r["id"])
                seen.add(cid)
                payload = _chunk_payload(r)
                old = cbefore.get(cid)
                if old is None:
                    cops.append(("store", "chunk", payload))
                    counts["created"] += 1
                elif old[1] != payload.encode("utf-8"):
                    cops.append(("update", old[0], payload))
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
            cgone = [uid for cid, (uid, _) in cbefore.items() if cid not in seen]
            cops += [("delete", uid) for uid in cgone]
            self._commit_split(cops, reason)
            stats["chunks"] = {**counts, "deleted": len(cgone)}

        # A mirror that had to skip cross-project edges is not the graph SQL
        # would build, so it never claims the state: readers fall back to SQL.
        fp = "incomplete" if cross else fingerprint(conn, project_id, run_id)
        self.db.upsert("meta", "key", {"key": "index", "run_id": int(run_id), "fp": fp})
        stats["seconds"] = round(time.perf_counter() - t0, 3)
        return stats

    # ---------- read ----------

    def _index_meta(self) -> dict[str, Any] | None:
        try:
            units = self.db.find("meta", "key", "index")
        except kerno.KernoError:
            return None
        return self.db.read_document(units[0].id) if units else None

    def run_id(self) -> int | None:
        meta = self._index_meta()
        return int(meta["run_id"]) if meta else None

    def fingerprint(self) -> str | None:
        meta = self._index_meta()
        return meta.get("fp") if meta else None

    @staticmethod
    def _meta(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(doc["sid"]),
            "name": doc["name"],
            "qualified_name": doc["qname"],
            "kind": doc["kind"],
            "file_path": doc["file_path"],
            "start_line": doc["start_line"],
            "end_line": doc["end_line"],
        }

    def graph(self) -> tuple[dict[int, dict], list[tuple[int, int, str, float, str]]]:
        """Every symbol's meta and every edge as `(src, dst, type, weight, origin)`,
        keyed by the SQLite symbol ids -- the inputs `load_graph` needs."""
        by_unit: dict[str, int] = {}
        sym_meta: dict[int, dict] = {}
        for u in self.db.query("symbol"):
            m = self._meta(_doc(self.db, u))
            by_unit[u.id] = m["id"]
            sym_meta[m["id"]] = m
        edges = []
        for rel in self.db.relations():
            if not rel.relation_type.startswith(_EDGE_PREFIX):
                continue
            et, weight, origin = _parse_edge_type(rel.relation_type)
            edges.append((by_unit[rel.from_id], by_unit[rel.to_id], et, weight, origin))
        return sym_meta, edges

    def find_symbol(self, name: str) -> list[dict[str, Any]]:
        return [self._meta(_doc(self.db, u)) for u in self.db.find("symbol", "name", _key(name))]

    def find_qname(self, qname: str) -> list[dict[str, Any]]:
        out = [self._meta(_doc(self.db, u)) for u in self.db.find("symbol", "qkey", _key(qname))]
        return [m for m in out if m["qualified_name"] == qname]

    def symbols_in_file(self, path: str) -> list[dict[str, Any]]:
        out = [self._meta(_doc(self.db, u)) for u in self.db.find("symbol", "fkey", _key(path))]
        return [m for m in out if m["file_path"] == path]

    def _neighbours(self, sid: int, direction: str, edge_type: str) -> list[dict[str, Any]]:
        units = self.db.find("symbol", "sid", int(sid))
        if not units:
            return []
        out = []
        seen: set[str] = set()
        if hasattr(self.db, "neighbors"):  # relation + other end in one call
            for rel, other in self.db.neighbors(units[0].id, direction):
                rtype = rel.relation_type
                if not rtype.startswith(_EDGE_PREFIX) or _parse_edge_type(rtype)[0] != edge_type:
                    continue
                if other.id not in seen:
                    seen.add(other.id)
                    out.append(self._meta(other.document()))
            return out
        read = self.db.read_document
        for rel in self.db.related(units[0].id, direction):
            if not rel.relation_type.startswith(_EDGE_PREFIX):
                continue
            if _parse_edge_type(rel.relation_type)[0] != edge_type:
                continue
            other = rel.from_id if direction == "in" else rel.to_id
            if other not in seen:
                seen.add(other)
                out.append(self._meta(read(other)))
        return out

    def callers(self, sid: int, edge_type: str = "calls") -> list[dict[str, Any]]:
        return self._neighbours(sid, "in", edge_type)

    def callees(self, sid: int, edge_type: str = "calls") -> list[dict[str, Any]]:
        return self._neighbours(sid, "out", edge_type)

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """BM25 keyword search over chunks. `[]` when no word of the query can match."""
        try:
            hits = self.db.search_text(query, limit=limit, types=["chunk"])
        except kerno.InvalidArgumentError:
            return []
        out = []
        for h in hits:
            d = _parse_chunk(self.db.read(h.id).payload)
            d["score"] = h.score
            d["snippet"] = d.pop("text")[:240]
            out.append(d)
        return out

    def grep(self, needle: str, limit: int | None = None) -> list[int]:
        """Chunk ids whose payload holds `needle` (ASCII case-insensitive)."""
        try:
            ids = self.db.search_substring(needle, limit=limit, types=["chunk"])
        except kerno.InvalidArgumentError:
            return []
        read = self.db.read
        return [int(read(i).payload.split(b"\t", 1)[0]) for i in ids]

    def symbol_history(self, sid: int) -> list[dict[str, Any]]:
        """Every revision of a symbol the mirror has seen -- the thing SQLite's
        overwrite-in-place index cannot answer. Oldest first."""
        units = self.db.find("symbol", "sid", int(sid))
        if not units:
            return []
        return [
            {"revision": r.revision, "kind": r.kind, "commit_seq": r.commit_seq}
            for r in self.db.history(units[0].id, order="asc")
        ]


# ---------- shadow-mode hooks (used only under LIVESPEC_EXPERIMENTAL_KERNO) ----------


# How often the shadow actually served. Dumped at exit to the JSON file named
# by LIVESPEC_EXPERIMENTAL_KERNO_STATS, so a test-suite run under the flag can
# prove the Kerno path ran instead of silently falling back to SQL.
COUNTERS = {"syncs": 0, "sync_errors": 0, "graph_served": 0, "graph_fallback": 0}


def _dump_counters() -> None:
    target = os.environ.get(ENV_FLAG + "_STATS")
    if target and any(COUNTERS.values()):
        import json

        with open(f"{target}.{os.getpid()}", "w") as fh:
            json.dump(COUNTERS, fh)


atexit.register(_dump_counters)


def sync_after_index(conn: sqlite3.Connection, db_path: str, project_id: int) -> dict | None:
    """Sync the mirror after an index run. Never raises: the mirror is a shadow."""
    if not enabled() or not db_path or db_path.startswith("conn:"):
        return None
    row = conn.execute(
        """SELECT id FROM index_run
           WHERE project_id=? AND finished_at IS NOT NULL AND files_changed > 0
           ORDER BY id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        with KernoMirror(mirror_path(db_path, project_id)) as m:
            if m.fingerprint() == fingerprint(conn, project_id, int(row["id"])):
                return {"skipped": "fresh"}
            COUNTERS["syncs"] += 1
            return m.sync(conn, project_id, run_id=int(row["id"]), chunks=False)
    except kerno.KernoError as e:
        COUNTERS["sync_errors"] += 1
        return {"error": f"{type(e).__name__}: {e}"}


def load_graph_inputs(conn: sqlite3.Connection, db_path: str, project_id: int, run_id: int):
    """`(sym_meta, edges)` from the mirror, or None when it cannot serve this state."""
    if not enabled() or not db_path or db_path.startswith("conn:"):
        return None
    path = mirror_path(db_path, project_id)
    try:
        if path.exists():
            with KernoMirror(path, read_only=True) as m:
                if m.fingerprint() == fingerprint(conn, project_id, run_id):
                    out = m.graph()
                    COUNTERS["graph_served"] += 1
                    return out
    except kerno.KernoError:
        pass
    COUNTERS["graph_fallback"] += 1
    return None
