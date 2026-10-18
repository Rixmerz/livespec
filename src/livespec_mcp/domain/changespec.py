"""``CHANGESPEC.md``: the versioned history of code ↔ spec, one entry per track.

The file is the source of truth; the ``track`` / ``track_spec`` /
``track_symbol`` tables are derived from it and rebuilt on every index run
(``ingest_changespec``). It is written only by an explicit ``record_track``
call — never by indexing — and it is the one file livespec writes into a
project: a human-owned history document, like a CHANGELOG, not a UI artifact.

Format (newest first; prose is the caller's, headings and markers are fixed)::

    # CHANGESPEC

    ## 2026-10-18 · <title> (<base7>..<head7>)
    <!-- livespec:track id=<id> base=<full sha> head=<full sha> -->
    Why: <overall reason>
    OpenSpec: <change folder>, ...

    ### <spec-id>
    - Spec: MODIFIED "<requirement>", RENAMED "<old>" → "<new>"
    - Code: + newFn (path), ~ changedFn (path), - removedFn (path)
    - Why: <per-spec reason>

    ### (no spec)
    - Code: + helper (path)

A moved symbol shows its old path after an arrow: ``~ fn (new.py ← old.py)``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from livespec_mcp.domain.tracks import TrackDraft, qualify, track_id_for

CHANGESPEC_FILE = "CHANGESPEC.md"
HEADER = "# CHANGESPEC"
PREAMBLE = (
    "Evolutionary history of code ↔ spec, newest first. Each entry is a track: a "
    "git range, why it exists, and per spec what changed. Written by livespec "
    "`record_track`; edit the prose freely, keep the headings and the "
    "`livespec:track` markers."
)
NO_SPEC = "(no spec)"
_OP_CHAR = {"added": "+", "modified": "~", "removed": "-"}
_CHAR_OP = {v: k for k, v in _OP_CHAR.items()}

_ENTRY_RE = re.compile(
    r"^## (?P<date>\d{4}-\d{2}-\d{2}) · (?P<title>.+?) "
    r"\((?P<b>[0-9a-f]{4,64})\.\.(?P<h>[0-9a-f]{4,64})\)\s*$"
)
_MARKER_RE = re.compile(
    r"^<!--\s*livespec:track\s+id=(?P<id>\S+)\s+base=(?P<base>[0-9a-f]{40,64})"
    r"\s+head=(?P<head>[0-9a-f]{40,64})\s*-->\s*$"
)
_SPEC_HEAD_RE = re.compile(r"^### (?P<sid>.+?)\s*$")
_CODE_ITEM_RE = re.compile(r"(?P<op>[+~-]) (?P<name>\S+?) \((?P<path>.*?)\)(?=, [+~-] |\s*$)")
_Q = r'"((?:[^"\\]|\\.)*)"'
_DELTA_ITEM_RE = re.compile(rf"(?P<op>ADDED|MODIFIED|REMOVED|RENAMED) {_Q}(?: → {_Q})?")


@dataclass
class CodeItem:
    change: str  # added | modified | removed
    name: str
    path: str
    old_path: str | None = None


@dataclass
class SpecSection:
    spec_id: str | None  # None = the "(no spec)" section
    deltas: list[dict[str, Any]] = field(default_factory=list)
    code: list[CodeItem] = field(default_factory=list)
    why: str | None = None


@dataclass
class TrackEntry:
    track_id: str
    base_sha: str
    head_sha: str
    date: str
    title: str
    why: str
    changes: list[str] = field(default_factory=list)
    sections: list[SpecSection] = field(default_factory=list)


# ------------------------------------------------------------------ render


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def _render_code(items: list[CodeItem]) -> str:
    parts = []
    for c in items:
        where = f"{c.path} ← {c.old_path}" if c.old_path else c.path
        parts.append(f"{_OP_CHAR[c.change]} {c.name} ({where})")
    return ", ".join(parts)


def _render_delta(d: dict[str, Any]) -> str:
    if d["operation"] == "RENAMED" and d.get("rename_from"):
        return f"RENAMED {_quote(d['rename_from'])} → {_quote(d['requirement'])}"
    return f"{d['operation']} {_quote(d['requirement'])}"


def render_entry(entry: TrackEntry) -> str:
    """One entry, from its ``##`` heading to the line before the next one."""
    title = _one_line(entry.title) or "(untitled)"
    lines = [
        f"## {entry.date} · {title} ({entry.base_sha[:7]}..{entry.head_sha[:7]})",
        f"<!-- livespec:track id={entry.track_id} base={entry.base_sha} head={entry.head_sha} -->",
    ]
    why = (entry.why or "").strip().splitlines() or [""]
    lines.append(f"Why: {why[0].strip()}".rstrip())
    lines.extend(ln.rstrip() for ln in why[1:])
    if entry.changes:
        lines.append(f"OpenSpec: {', '.join(entry.changes)}")
    for s in entry.sections:
        lines.append("")
        lines.append(f"### {s.spec_id or NO_SPEC}")
        if s.deltas:
            lines.append("- Spec: " + ", ".join(_render_delta(d) for d in s.deltas))
        if s.code:
            lines.append("- Code: " + _render_code(s.code))
        if s.why:
            lines.append(f"- Why: {_one_line(s.why)}")
    return "\n".join(lines) + "\n"


def entry_from_draft(
    draft: TrackDraft, *, title: str, why: str, spec_reasons: dict[str, str] | None
) -> TrackEntry:
    reasons = spec_reasons or {}
    sections: list[SpecSection] = []

    def code_of(items) -> list[CodeItem]:
        order = {"added": 0, "modified": 1, "removed": 2}
        return [
            CodeItem(c.change, c.name, c.path, c.old_path)
            for c in sorted(items, key=lambda c: (order[c.change], c.path, c.name))
        ]

    for sid, t in draft.specs.items():
        sections.append(
            SpecSection(
                spec_id=sid,
                deltas=[
                    {
                        "operation": d.operation,
                        "requirement": d.requirement,
                        **({"rename_from": d.rename_from} if d.rename_from else {}),
                    }
                    for d in t.deltas
                ],
                code=code_of(t.symbols),
                why=reasons.get(sid),
            )
        )
    if draft.unlinked:
        sections.append(SpecSection(spec_id=None, code=code_of(draft.unlinked)))
    return TrackEntry(
        track_id=draft.track_id,
        base_sha=draft.base_sha,
        head_sha=draft.head_sha,
        date=draft.date,
        title=title,
        why=why,
        changes=draft.changes,
        sections=sections,
    )


# ------------------------------------------------------------------- parse


@dataclass
class Block:
    """A ``##`` block of the file, kept verbatim so a rewrite touches only the
    entry it updates. ``entry`` is None when the block does not parse."""

    text: str
    line: int
    entry: TrackEntry | None = None
    problem: str | None = None


def _parse_block(lines: list[str]) -> tuple[TrackEntry | None, str | None]:
    head = _ENTRY_RE.match(lines[0])
    if head is None:
        return None, "heading is not '## YYYY-MM-DD · <title> (<base>..<head>)'"
    marker = next((m for ln in lines[1:4] if (m := _MARKER_RE.match(ln.strip()))), None)
    if marker is None:
        return None, "no '<!-- livespec:track id=... base=... head=... -->' marker"
    entry = TrackEntry(
        track_id=marker.group("id"),
        base_sha=marker.group("base"),
        head_sha=marker.group("head"),
        date=head.group("date"),
        title=head.group("title").strip(),
        why="",
    )
    why_lines: list[str] = []
    in_why = False
    section: SpecSection | None = None
    for raw in lines[1:]:
        ln = raw.rstrip()
        if _MARKER_RE.match(ln.strip()):
            continue
        sh = _SPEC_HEAD_RE.match(ln)
        if sh:
            in_why = False
            sid = sh.group("sid").strip()
            section = SpecSection(spec_id=None if sid == NO_SPEC else sid)
            entry.sections.append(section)
            continue
        if section is None:
            if ln.startswith("Why:"):
                in_why = True
                why_lines.append(ln[4:].strip())
            elif ln.startswith("OpenSpec:"):
                in_why = False
                entry.changes = [c.strip() for c in ln[9:].split(",") if c.strip()]
            elif in_why:
                why_lines.append(ln)
            continue
        body = ln.strip()
        if body.startswith("- Spec:"):
            for m in _DELTA_ITEM_RE.finditer(body[7:]):
                first, second = _unquote(m.group(2)), m.group(3)
                if m.group("op") == "RENAMED" and second is not None:
                    section.deltas.append(
                        {
                            "operation": "RENAMED",
                            "requirement": _unquote(second),
                            "rename_from": first,
                        }
                    )
                else:
                    section.deltas.append({"operation": m.group("op"), "requirement": first})
        elif body.startswith("- Code:"):
            for m in _CODE_ITEM_RE.finditer(body[7:].strip()):
                where = m.group("path")
                path, _, old = where.partition(" ← ")
                section.code.append(
                    CodeItem(_CHAR_OP[m.group("op")], m.group("name"), path, old or None)
                )
        elif body.startswith("- Why:"):
            section.why = body[6:].strip() or None
    entry.why = "\n".join(why_lines).strip()
    return entry, None


def parse_changespec(text: str) -> tuple[str, list[Block]]:
    """``(preamble, blocks)``. The preamble is everything before the first
    ``## `` heading; each block runs to the next one."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    preamble = "\n".join(lines[: starts[0]] if starts else lines)
    blocks: list[Block] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = lines[start:end]
        entry, problem = _parse_block(chunk)
        blocks.append(
            Block(
                text="\n".join(chunk).rstrip("\n") + "\n",
                line=start + 1,
                entry=entry,
                problem=problem,
            )
        )
    return preamble, blocks


def upsert_entry(text: str | None, entry: TrackEntry) -> tuple[str, bool]:
    """``(new file text, created)``. An entry with the same base and head is
    replaced in place (keeping per-spec whys the update does not restate); a
    new one goes above the first entry that is not newer."""
    if not text or not text.strip():
        return f"{HEADER}\n\n{PREAMBLE}\n\n{render_entry(entry)}", True
    preamble, blocks = parse_changespec(text)
    created = True
    for b in blocks:
        if b.entry and (b.entry.base_sha, b.entry.head_sha) == (entry.base_sha, entry.head_sha):
            # A per-spec why the person already wrote survives an update that
            # does not restate it.
            old = {s.spec_id: s.why for s in b.entry.sections if s.why}
            for sec in entry.sections:
                if not sec.why and sec.spec_id in old:
                    sec.why = old[sec.spec_id]
            b.text = render_entry(entry)
            created = False
            break
    rendered = render_entry(entry)
    if created:
        at = len(blocks)
        for i, b in enumerate(blocks):
            if b.entry and b.entry.date <= entry.date:
                at = i
                break
        blocks.insert(at, Block(text=rendered, line=0, entry=entry))
    pre = preamble.rstrip()
    if not pre:
        pre = f"{HEADER}\n\n{PREAMBLE}"
    body = "\n".join(b.text.rstrip("\n") + "\n" for b in blocks)
    return f"{pre}\n\n{body}", created


def write_entry(workspace: Path, entry: TrackEntry) -> dict[str, Any]:
    """@spec:tracks-record-a-track-in-changespec-md

    Add or update ``entry`` in ``<workspace>/CHANGESPEC.md``."""
    path = Path(workspace) / CHANGESPEC_FILE
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    new_text, created = upsert_entry(existing, entry)
    path.write_text(new_text, encoding="utf-8")
    return {"path": CHANGESPEC_FILE, "created": created, "entry_markdown": render_entry(entry)}


# ------------------------------------------------------------------ ingest


def ingest_changespec(conn: sqlite3.Connection, project_id: int, workspace: Path) -> dict[str, Any]:
    """@spec:tracks-index-rebuilt-from-changespec-md

    Replace this project's track rows with what ``CHANGESPEC.md`` holds.

    A missing file means no tracks. A block that does not parse is reported in
    ``problems`` and skipped; an unreadable file is reported and the existing
    rows are kept. Never raises for the file's content."""
    path = Path(workspace) / CHANGESPEC_FILE
    if not path.is_file():
        n = conn.execute("SELECT COUNT(*) FROM track WHERE project_id=?", (project_id,)).fetchone()
        if n[0]:
            conn.execute("DELETE FROM track WHERE project_id=?", (project_id,))
        return {"present": False, "tracks": 0}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {
            "present": True,
            "tracks": None,
            "problems": [{"line": 0, "error": f"unreadable: {e}"}],
        }
    _, blocks = parse_changespec(text)
    problems: list[dict[str, Any]] = []
    entries: list[TrackEntry] = []
    seen: set[tuple[str, str]] = set()
    for b in blocks:
        if b.entry is None:
            problems.append({"line": b.line, "error": b.problem})
            continue
        key = (b.entry.base_sha, b.entry.head_sha)
        if key in seen:
            problems.append({"line": b.line, "error": "duplicate entry for the same range"})
            continue
        seen.add(key)
        entries.append(b.entry)
    conn.execute("DELETE FROM track WHERE project_id=?", (project_id,))
    symbols = 0
    for ordinal, e in enumerate(entries):
        cur = conn.execute(
            """INSERT INTO track(project_id, track_key, title, why, base_sha, head_sha,
                                 recorded_on, changes, ordinal)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                project_id,
                e.track_id or track_id_for(e.base_sha, e.head_sha),
                e.title,
                e.why,
                e.base_sha,
                e.head_sha,
                e.date,
                json.dumps(e.changes) if e.changes else None,
                ordinal,
            ),
        )
        tid = int(cur.lastrowid)
        for s in e.sections:
            if s.spec_id is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO track_spec(track_id, spec_id, why, deltas)
                       VALUES(?,?,?,?)""",
                    (tid, s.spec_id, s.why, json.dumps(s.deltas) if s.deltas else None),
                )
            for c in s.code:
                conn.execute(
                    """INSERT INTO track_symbol(track_id, spec_id, change, name,
                                                qualified_name, path, old_path)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tid, s.spec_id, c.change, c.name, qualify(c.name, c.path), c.path, c.old_path),
                )
                symbols += 1
    out: dict[str, Any] = {"present": True, "tracks": len(entries), "symbols": symbols}
    if problems:
        out["problems"] = problems
    return out


# ----------------------------------------------------------------- history


def spec_aliases(conn: sqlite3.Connection, project_id: int, spec_id: str) -> set[str]:
    """``spec_id`` plus every legacy id that points at it: track rows keep the
    id CHANGESPEC.md was written with."""
    return {spec_id} | {
        r["alias"]
        for r in conn.execute(
            """SELECT a.alias FROM spec_alias a JOIN spec sp ON sp.id=a.spec_id
               WHERE a.project_id=? AND sp.spec_id=?""",
            (project_id, spec_id),
        )
    }


def spec_history(conn: sqlite3.Connection, project_id: int, ids: set[str]) -> list[dict[str, Any]]:
    """@spec:tracks-spec-and-symbol-history

    Tracks that touched any of ``ids`` (one Spec and its aliases), newest
    first, with the per-Spec why, deltas and symbols."""
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT t.* FROM track t WHERE t.project_id=? AND (
                EXISTS (SELECT 1 FROM track_spec ts
                        WHERE ts.track_id=t.id AND ts.spec_id IN ({marks}))
             OR EXISTS (SELECT 1 FROM track_symbol x
                        WHERE x.track_id=t.id AND x.spec_id IN ({marks})))
            ORDER BY t.ordinal, t.id""",
        (project_id, *ids, *ids),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        ts = conn.execute(
            f"SELECT why, deltas FROM track_spec WHERE track_id=? AND spec_id IN ({marks})",
            (int(r["id"]), *ids),
        ).fetchone()
        by: dict[str, list[dict[str, Any]]] = {"added": [], "modified": [], "removed": []}
        for x in conn.execute(
            f"""SELECT change, name, qualified_name, path, old_path FROM track_symbol
                WHERE track_id=? AND spec_id IN ({marks}) ORDER BY id""",
            (int(r["id"]), *ids),
        ):
            item = {"name": x["name"], "qualified_name": x["qualified_name"], "path": x["path"]}
            if x["old_path"]:
                item["old_path"] = x["old_path"]
            by.setdefault(x["change"], []).append(item)
        items.append(
            {
                "track_id": r["track_key"],
                "date": r["recorded_on"],
                "title": r["title"],
                "why": r["why"],
                "base_sha": r["base_sha"],
                "head_sha": r["head_sha"],
                "spec_why": ts["why"] if ts else None,
                "deltas": json.loads(ts["deltas"]) if ts and ts["deltas"] else [],
                "symbols": by,
            }
        )
    return items


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def symbol_history(
    conn: sqlite3.Connection, project_id: int, query: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """@spec:tracks-spec-and-symbol-history

    ``(tracks, matched qualified names)`` for a symbol named by its qualified
    name, its module-local name, or a qualified-name suffix. Newest first; each
    track carries the changes it made to the symbol and the Specs involved."""
    rows = conn.execute(
        """SELECT x.*, t.track_key, t.title, t.why AS track_why, t.recorded_on,
                  t.base_sha, t.head_sha
           FROM track_symbol x JOIN track t ON t.id=x.track_id
           WHERE t.project_id=? AND (x.qualified_name=? OR x.name=?
                 OR x.qualified_name LIKE ? ESCAPE '\\')
           ORDER BY t.ordinal, t.id, x.id""",
        (project_id, query, query, "%." + _like_escape(query)),
    ).fetchall()
    grouped: dict[int, dict[str, Any]] = {}
    for r in rows:
        tid = int(r["track_id"])
        g = grouped.get(tid)
        if g is None:
            g = grouped[tid] = {
                "track_id": r["track_key"],
                "date": r["recorded_on"],
                "title": r["title"],
                "why": r["track_why"],
                "base_sha": r["base_sha"],
                "head_sha": r["head_sha"],
                "changes": [],
                "specs": [],
            }
        change = {"change": r["change"], "qualified_name": r["qualified_name"], "path": r["path"]}
        if r["old_path"]:
            change["old_path"] = r["old_path"]
        if change not in g["changes"]:
            g["changes"].append(change)
        if r["spec_id"] and r["spec_id"] not in [s["spec_id"] for s in g["specs"]]:
            ts = conn.execute(
                "SELECT why FROM track_spec WHERE track_id=? AND spec_id=?", (tid, r["spec_id"])
            ).fetchone()
            g["specs"].append({"spec_id": r["spec_id"], "why": ts["why"] if ts else None})
    return list(grouped.values()), sorted({r["qualified_name"] for r in rows})
