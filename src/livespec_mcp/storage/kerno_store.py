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
import json
import os
import sqlite3
import time
from operator import attrgetter, itemgetter
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
_BATCH = 500_000
_EDGE_PREFIX = "e:"

# Units layer 13: a covering field keeps each symbol's inline payload in its
# index entry, so a by-name lookup reads the index alone and never the units.
_COVERING: dict[str, bool] = (
    {"covering": True}
    if kerno is not None
    and "covering" in (getattr(kerno.Database.define_schema, "__doc__", "") or "")
    and os.environ.get("LIVESPEC_KERNO_COVERING", "1") != "0"
    else {}
)

_SYMBOL_SCHEMA = [
    {"name": "sid", "type": "int", "unique": True},
    {"name": "name", "type": "text", "indexed": True, **_COVERING},
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


# The symbol fields a lookup returns, and the key each has in a result.
# `fields=` on kernodb's `*_documents` calls skips building the other values and,
# as a mapping, names the keys. It costs about 0.4 us a call, so only the lookups
# that can return many symbols pass it.
_META_MAP = {
    "sid": "id",
    "name": "name",
    "qname": "qualified_name",
    "kind": "kind",
    "file_path": "file_path",
    "start_line": "start_line",
    "end_line": "end_line",
}
_DOC_FIELDS: bool | None = None  # does `find_documents` take `fields` as a mapping?
_NEIGHBORS_PREFIX: bool | None = None  # does `Database.neighbors` take `type_prefix`?


def _detect_neighbors_prefix(db: Any) -> None:
    global _NEIGHBORS_PREFIX
    fn = getattr(db, "neighbors", None)
    try:
        import inspect

        _NEIGHBORS_PREFIX = fn is not None and "type_prefix" in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # builtin without a text signature
        _NEIGHBORS_PREFIX = False


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


_EDGE_NAMES: dict[str, str] = {}


def _parse_edge_type(rel_type: str) -> tuple[str, float, str]:
    parts = rel_type[len(_EDGE_PREFIX) :].split("~", 2)
    if len(parts) == 3:
        return parts[0], float(parts[1]), parts[2]
    return parts[0], 1.0, "livespec"


def _symbol_doc(r: tuple) -> dict[str, Any]:
    """`r` is `(id, name, qualified_name, kind, start_line, end_line, body_hash, path)`."""
    doc: dict[str, Any] = {
        "sid": int(r[0]),
        "name": _key(r[1]),
        "qkey": _key(r[2]),
        "fkey": _key(r[7]),
        "qname": r[2],
        "file_path": r[7],
        "kind": r[3],
        "start_line": int(r[4]),
        "end_line": int(r[5]),
    }
    if r[6]:
        doc["body_hash"] = r[6]
    return doc


def _chunk_payload(r: tuple) -> str:
    """A chunk as a RAW text payload: one tab-separated header line, then the text.

    Not a typed document on purpose. In kernodb 1.0.0a4 the keyword index
    silently skips a payload that is not valid UTF-8, and a document's binary
    encoding stops being valid UTF-8 as soon as an int field is >= 128 or a
    text field is >= 128 bytes long -- measured here: 5% of the chunks were
    searchable as documents. A raw `str` payload is indexed in full. The price
    is no schema, so no `upsert` by natural key: sync keeps its own
    `cid -> unit` map from the header line.

    `r` is `(id, source_type, source_id, text_kind, file_path, start_line, end_line, text)`."""
    head = "\t".join(
        str(v if v is not None else "")
        for v in (
            r[0],
            r[1],
            r[2],
            r[3],
            r[5],
            r[6],
            (r[4] or "").replace("\t", " ").replace("\n", " "),
        )
    )
    return head + "\n" + r[7]


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
    return _fp(run_id, _edge_row(conn, project_id))


def _edge_row(conn: sqlite3.Connection, project_id: int) -> tuple:
    """`(count, max id, sum of weights, count of origin 'livespec')` of the project's edges."""
    cur = conn.cursor()
    cur.row_factory = None
    return cur.execute(
        """SELECT COUNT(*), COALESCE(MAX(e.id), 0), COALESCE(SUM(e.weight), 0),
                  COALESCE(SUM(e.origin = 'livespec'), 0)
           FROM symbol_edge e
           JOIN symbol s ON s.id = e.src_symbol_id
           JOIN file f ON f.id = s.file_id
           WHERE f.project_id = ?""",
        (project_id,),
    ).fetchone()


def _fp(run_id: int, r: tuple) -> str:
    return f"{run_id}:{r[0]}:{r[1]}:{r[2]!r}:{r[3]}"


def _chunk_row(conn: sqlite3.Connection, project_id: int) -> tuple:
    """`(count, max id, total text length)` of the project's chunks."""
    cur = conn.cursor()
    cur.row_factory = None
    return cur.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(LENGTH(text)), 0) FROM chunk WHERE project_id = ?",
        (project_id,),
    ).fetchone()


def chunk_signature(conn: sqlite3.Connection, project_id: int) -> str:
    """Cheap signature of the project's chunk table: row count, newest id and total
    text length. Chunks are rebuilt (new ids) or edited (length moves) by indexing."""
    return ":".join(map(str, _chunk_row(conn, project_id)))


class KernoMirror:
    """One Kerno database mirroring one project of one SQLite index."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        if kerno is None:
            raise RuntimeError("kernodb is not installed (pip install --pre kernodb)")
        self.path = Path(path)
        if not read_only:
            self.path.mkdir(parents=True, exist_ok=True)
        kw: dict[str, Any] = {"read_only": read_only, "max_batch_ops": min(_BATCH * 2, 1 << 20)}
        if not read_only and os.environ.get("LIVESPEC_KERNO_DURABILITY", "buffered") == "buffered":
            # The mirror is derived from SQLite and rebuilt when its stamp does not
            # match, so a commit need not wait for stable storage: SQLite's own
            # `synchronous=NORMAL`. close() still makes everything durable.
            # close() only flushes: the compaction it would do on the newest
            # tables is a third of a full sync's close and the next writer
            # session does it in the background.
            # bulk_without_wal: a full sync's three large commits skip the log
            # and are durable once their tables are written; a crash before
            # that loses them, and the stamp check rebuilds the mirror.
            for extra in (
                {"durability": "buffered", "close_compaction": False, "bulk_without_wal": True},
                {"durability": "buffered", "close_compaction": False},
                {"durability": "buffered"},
                {},  # a kernodb without the keywords
            ):
                try:
                    self.db = kerno.open(str(self.path), **extra, **kw)
                    break
                except TypeError:
                    if not extra:
                        raise
        else:
            self.db = kerno.open(str(self.path), **kw)
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

        # -- no-op shortcut: the stored fingerprint covers run + edge set; the
        # chunk signature (own meta unit, written only by a chunks=True sync)
        # covers the chunk table.
        er = _edge_row(conn, project_id)
        edge_fp = _fp(run_id, er)
        crow = _chunk_row(conn, project_id) if chunks else None
        csig = ":".join(map(str, crow)) if crow else None
        if self.fingerprint() == edge_fp and (not chunks or self._chunk_sig() == csig):
            zero = {"created": 0, "updated": 0, "unchanged": None, "deleted": 0}
            stats = {
                "skipped": "unchanged",
                "symbols": dict(zero),
                "edges": {"added": 0, "removed": 0, "total": None, "cross_project_skipped": 0},
            }
            if chunks:
                stats["chunks"] = dict(zero)
            stats["seconds"] = round(time.perf_counter() - t0, 3)
            return stats

        # Tuple cursors: index access, no per-row sqlite3.Row name lookups.
        cur = conn.cursor()
        cur.row_factory = None

        fast = self._sync_fast(cur, project_id, reason, chunks, er, crow)
        if fast is not None:
            self._finish(cur, project_id, run_id, edge_fp, csig, er, crow, cross=0)
            fast["fast_path"] = True
            fast["seconds"] = round(time.perf_counter() - t0, 3)
            return fast

        # -- chunks (keyword + substring indexes). First: theirs is the largest table,
        # and kernodb writes it out in the background while the symbols and edges
        # below are prepared and committed, instead of all of it at close.
        chunk_stats: dict[str, Any] | None = None
        if chunks:
            self._ensure_chunk_indexes()
            crows = cur.execute(
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
                cid = int(r[0])
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
            chunk_stats = {**counts, "deleted": len(cgone)}

        # -- symbols
        rows = cur.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.start_line, s.end_line,
                      s.body_hash, f.path
               FROM symbol s JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (project_id,),
        ).fetchall()
        before = self._existing("symbol", "sid")
        docs = list(map(_symbol_doc, rows))
        sym_ids, outcomes = self._upsert_all("symbol", "sid", docs, reason)
        gone = [uid for sid, uid in before.items() if sid not in sym_ids]
        self._delete_all(gone, reason)
        stats["symbols"] = {**outcomes, "deleted": len(gone)}

        # -- edges: diff the live relations against the SQL edge set
        erows = cur.execute(
            """SELECT e.src_symbol_id, e.dst_symbol_id, e.edge_type, e.weight, e.origin
               FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ?""",
            (project_id,),
        ).fetchall()
        get = sym_ids.get
        srcs = list(map(get, map(itemgetter(0), erows)))
        dsts = list(map(get, map(itemgetter(1), erows)))
        triples = list(map(itemgetter(2, 3, 4), erows))
        tcache = {t: _edge_type(t[0], float(t[1]), t[2]) for t in set(triples)}
        types = list(map(tcache.__getitem__, triples))
        if None in srcs or None in dsts:  # dst lives in another project of a group DB: not mirrored
            keep = [i for i, (a, b) in enumerate(zip(srcs, dsts, strict=True)) if a and b]
            cross = len(erows) - len(keep)
            srcs = [srcs[i] for i in keep]
            dsts = [dsts[i] for i in keep]
            types = [types[i] for i in keep]
        else:
            cross = 0
        want = set(zip(srcs, dsts, types, strict=True))
        rels = self.db.relations()
        keys = list(map(attrgetter("from_id", "to_id", "relation_type"), rels))
        if not all(t.startswith(_EDGE_PREFIX) for t in set(map(attrgetter("relation_type"), rels))):
            mine = [i for i, rel in enumerate(rels) if rel.relation_type.startswith(_EDGE_PREFIX)]
            rels = [rels[i] for i in mine]
            keys = [keys[i] for i in mine]
        have = set(keys)
        stale = have - want
        ops: list[tuple] = []
        if stale:
            ops = [("unrelate", rel.id) for key, rel in zip(keys, rels, strict=True) if key in stale]
        removed = len(ops)
        ops += [("relate", *key) for key in want - have]
        for i in range(0, len(ops), _BATCH):
            self.db.commit(ops[i : i + _BATCH], reason=reason, actor="livespec-indexer")
        stats["edges"] = {
            "added": len(ops) - removed,
            "removed": removed,
            "total": len(want),
            "cross_project_skipped": cross,
        }

        if chunk_stats is not None:
            stats["chunks"] = chunk_stats

        # A mirror that had to skip cross-project edges is not the graph SQL
        # would build, so it never claims the state: readers fall back to SQL.
        self._finish(cur, project_id, run_id, edge_fp, csig, er, crow, cross=cross)
        stats["seconds"] = round(time.perf_counter() - t0, 3)
        return stats

    def _ensure_chunk_indexes(self) -> None:
        if not self.db.fulltext("chunk"):
            self.db.set_fulltext("chunk", True)
        # Trigram index is opt-in: without it grep() scans in parallel.
        if os.environ.get("LIVESPEC_EXPERIMENTAL_KERNO_TRIGRAM") == "1" and not self.db.substring("chunk"):
            self.db.set_substring("chunk", True)

    def _finish(self, cur, project_id, run_id, edge_fp, csig, er, crow, *, cross: int) -> None:
        """Stamp the mirror: the state for the next incremental sync, then the chunk
        signature, then the index meta LAST (a torn sync leaves the older stamps)."""
        state: dict[str, Any] = {}
        if not cross:
            ns, smax = cur.execute(
                """SELECT COUNT(*), COALESCE(MAX(s.id), 0)
                   FROM symbol s JOIN file f ON f.id = s.file_id WHERE f.project_id = ?""",
                (project_id,),
            ).fetchone()
            state = {"s": smax, "ns": ns, "e": er[1], "ne": er[0], "w": round(float(er[2]), 6)}
            if crow:
                state.update(c=crow[1], nc=crow[0], cl=crow[2])
        self.db.upsert("meta", "key", {"key": "state", "run_id": int(run_id), "fp": json.dumps(state)})
        if crow:
            self.db.upsert("meta", "key", {"key": "chunks", "run_id": int(run_id), "fp": csig})
        fp = "incomplete" if cross else edge_fp
        self.db.upsert("meta", "key", {"key": "index", "run_id": int(run_id), "fp": fp})

    def _state(self) -> dict[str, Any] | None:
        try:
            units = self.db.find("meta", "key", "state")
            doc = self.db.read_document(units[0].id) if units else None
            st = json.loads(doc["fp"]) if doc else None
        except (kerno.KernoError, ValueError, KeyError):
            return None
        return st or None

    def _sync_fast(self, cur, project_id, reason, chunks, er, crow) -> dict[str, Any] | None:
        """Incremental sync from the ids SQLite hands out in order: a re-extracted
        file's symbols and chunks are deleted and re-inserted with new ids, so what
        changed is what lies above the stored maxima plus what disappeared. Every
        number is verified against SQL BEFORE anything is written; on any mismatch
        (an in-place edit, an edge dropped between surviving symbols) returns None
        and the caller runs the full diff. Never reads `relations()`."""
        st = self._state()
        if not st or (chunks and "c" not in st):
            return None
        before = self._existing("symbol", "sid")
        if len(before) != st["ns"]:
            return None
        sql_ids = set(
            map(
                itemgetter(0),
                cur.execute(
                    "SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id WHERE f.project_id = ?",
                    (project_id,),
                ).fetchall(),
            )
        )
        gone = before.keys() - sql_ids
        new_rows = cur.execute(
            """SELECT s.id, s.name, s.qualified_name, s.kind, s.start_line, s.end_line,
                      s.body_hash, f.path
               FROM symbol s JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ? AND s.id > ?""",
            (project_id, st["s"]),
        ).fetchall()
        new_ids = set(map(itemgetter(0), new_rows))
        if new_ids & before.keys() or len(before) - len(gone) + len(new_rows) != len(sql_ids):
            return None
        erows = cur.execute(
            """SELECT e.src_symbol_id, e.dst_symbol_id, e.edge_type, e.weight, e.origin
               FROM symbol_edge e
               JOIN symbol s ON s.id = e.src_symbol_id
               JOIN file f ON f.id = s.file_id
               WHERE f.project_id = ? AND e.id > ?""",
            (project_id, st["e"]),
        ).fetchall()
        keep = (before.keys() - gone) | new_ids
        ends = set(map(itemgetter(0), erows)) | set(map(itemgetter(1), erows))
        if not ends <= keep:  # an end outside the mirror (cross-project)
            return None
        # What the cascade of the deleted symbols takes with it.
        cascaded: dict[str, str] = {}
        for sid in gone:
            for direction in ("out", "in"):
                for rel in self.db.related(before[sid], direction):
                    if rel.relation_type.startswith(_EDGE_PREFIX):
                        cascaded[rel.id] = rel.relation_type
        removed_w = sum(_parse_edge_type(t)[1] for t in cascaded.values())
        added_w = sum(map(itemgetter(3), erows))
        if st["ne"] - len(cascaded) + len(erows) != er[0]:
            return None
        if abs(st["w"] - removed_w + added_w - float(er[2])) > 1e-4:
            return None

        cstats = None
        cops: list[tuple] = []
        if chunks:
            cbefore = {
                int(u.payload.split(b"\t", 1)[0]): (u.id, u.payload) for u in self.db.query("chunk")
            }
            if len(cbefore) != st["nc"]:
                return None
            sql_cids = set(
                map(
                    itemgetter(0),
                    cur.execute("SELECT id FROM chunk WHERE project_id = ?", (project_id,)).fetchall(),
                )
            )
            cgone = cbefore.keys() - sql_cids
            crows_new = cur.execute(
                """SELECT id, source_type, source_id, text_kind, file_path, start_line,
                          end_line, text
                   FROM chunk WHERE project_id = ? AND id > ?""",
                (project_id, st["c"]),
            ).fetchall()
            if (
                {r[0] for r in crows_new} & cbefore.keys()
                or len(cbefore) - len(cgone) + len(crows_new) != crow[0]
            ):
                return None
            gone_len = sum(
                len(cbefore[c][1].split(b"\n", 1)[1].decode("utf-8", errors="replace")) for c in cgone
            )
            if st["cl"] - gone_len + sum(len(r[7]) for r in crows_new) != crow[2]:
                return None
            cops = [("delete", cbefore[c][0]) for c in cgone]
            cops += [("store", "chunk", _chunk_payload(r)) for r in crows_new]
            cstats = {
                "created": len(crows_new),
                "updated": 0,
                "unchanged": len(cbefore) - len(cgone),
                "deleted": len(cgone),
            }

        # -- verified: apply. Deletes (cascade), creates, then relates.
        self._delete_all([before[s] for s in gone], reason)
        new_units, outcomes = self._upsert_all("symbol", "sid", list(map(_symbol_doc, new_rows)), reason)
        unit_of = {**before, **new_units}.__getitem__
        tcache: dict[tuple, str] = {}
        ops: list[tuple] = []
        for src, dst, et, w, o in erows:
            t = (et, w, o)
            if t not in tcache:
                tcache[t] = _edge_type(et, float(w), o)
            ops.append(("relate", unit_of(src), unit_of(dst), tcache[t]))
        for i in range(0, len(ops), _BATCH):
            self.db.commit(ops[i : i + _BATCH], reason=reason, actor="livespec-indexer")
        stats: dict[str, Any] = {
            "symbols": {
                "created": outcomes["created"],
                "updated": outcomes["updated"],
                "unchanged": len(before) - len(gone) + outcomes["unchanged"],
                "deleted": len(gone),
            },
            "edges": {
                "added": len(ops),
                "removed": len(cascaded),
                "total": er[0],
                "cross_project_skipped": 0,
            },
        }
        if chunks:
            self._ensure_chunk_indexes()
            self._commit_split(cops, reason)
            stats["chunks"] = cstats
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

    def _chunk_sig(self) -> str | None:
        try:
            units = self.db.find("meta", "key", "chunks")
        except kerno.KernoError:
            return None
        return self.db.read_document(units[0].id).get("fp") if units else None

    @staticmethod
    def _meta(doc: dict[str, Any]) -> dict[str, Any]:
        # A dict display: three times faster than dict(zip(...)) over an itemgetter.
        return {
            "id": doc["sid"],
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

    def _find_docs(self, field: str, value: str) -> list[dict[str, Any]]:
        """Documents of the symbols whose indexed `field` equals `value` (one call
        when kernodb has `find_documents`)."""
        fd = getattr(self.db, "find_documents", None)
        if fd is not None:
            return fd("symbol", field, value)
        return [_doc(self.db, u) for u in self.db.find("symbol", field, value)]

    def _find_metas(self, field: str, value: str) -> list[dict[str, Any]]:
        """The same symbols as their result dicts. With a kernodb whose
        `find_documents` renames (`fields=` as a mapping) the binding builds each
        result dict directly: no second dict per symbol."""
        global _DOC_FIELDS
        fd = getattr(self.db, "find_documents", None)
        if fd is not None and _DOC_FIELDS is not False:
            try:
                out = fd("symbol", field, value, fields=_META_MAP)
            except (TypeError, kerno.KernoError):
                _DOC_FIELDS = False
            else:
                _DOC_FIELDS = True
                return out
        return list(map(self._meta, self._find_docs(field, value)))

    def find_symbol(self, name: str) -> list[dict[str, Any]]:
        return self._find_metas("name", _key(name))

    def find_qname(self, qname: str) -> list[dict[str, Any]]:
        out = map(self._meta, self._find_docs("qkey", _key(qname)))
        return [m for m in out if m["qualified_name"] == qname]

    def symbols_in_file(self, path: str) -> list[dict[str, Any]]:
        return [m for m in self._find_metas("fkey", _key(path)) if m["file_path"] == path]

    def _neighbours(self, sid: int, direction: str, edge_type: str) -> list[dict[str, Any]]:
        if hasattr(self.db, "neighbor_documents"):  # find + relations + documents in one call
            if _NEIGHBORS_PREFIX is None:
                _detect_neighbors_prefix(self.db)
            prefix = (_EDGE_PREFIX + edge_type) if _NEIGHBORS_PREFIX else _EDGE_PREFIX
            seen_sids: set[int] = set()
            out = []
            # No `fields=` here: a symbol has few callers, and the keyword costs more
            # per call than the three values it would skip.
            names = _EDGE_NAMES  # relation type -> edge type; few distinct strings, parsed once
            for rtype, doc in self.db.neighbor_documents("symbol", "sid", int(sid), direction, prefix):
                name = names.get(rtype)
                if name is None:
                    name = names[rtype] = _parse_edge_type(rtype)[0]
                dsid = doc["sid"]
                if name != edge_type or dsid in seen_sids:
                    continue
                seen_sids.add(dsid)
                out.append(self._meta(doc))
            return out
        units = self.db.find("symbol", "sid", int(sid))
        if not units:
            return []
        out = []
        seen: set[str] = set()
        if _NEIGHBORS_PREFIX is None:
            _detect_neighbors_prefix(self.db)
        if hasattr(self.db, "neighbors"):  # relation + other end in one call
            if _NEIGHBORS_PREFIX:  # relations of other types never load their unit
                pairs = self.db.neighbors(units[0].id, direction, type_prefix=_EDGE_PREFIX + edge_type)
            else:
                pairs = self.db.neighbors(units[0].id, direction)
            for rel, other in pairs:
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
