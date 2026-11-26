"""Which spec requirements were checked against the code that implements them, and which an edit since made stale.

A spec links to the symbols that implement it (`spec_symbol`, relation `implements`). An edit to one of
those symbols may break the requirement, so a requirement is *verified* only for the code it was checked
on: the record keeps, per requirement and implementing symbol, the symbol's `body_hash` and signature hash
when it was checked, the commit, and what checked it (`by`: a test that passed, a reviewer, an agent's
verifier) with its evidence. Once the symbol's body or signature changes, the requirement is **stale**
there until it is checked again.

The record is committed with the code (`openspec/verifications.json` where the repository keeps
`openspec/`, else `.livespec/verifications.json`): a CI job reads it with `livespec stale` and fails a
merge while a requirement is stale, whoever edited the code and with whatever tool. The index is read,
never written, here; the record is the only file this module writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT = 1
#: What checked a requirement; a CI job may demand `test` for the critical ones.
BY = ("test", "reviewer", "verifier")


def ledger_path(workspace: Path) -> Path:
    workspace = Path(workspace)
    if (workspace / "openspec").is_dir():
        return workspace / "openspec" / "verifications.json"
    return workspace / ".livespec" / "verifications.json"


def read(workspace: Path) -> dict[str, Any]:
    """The record, or an empty one; a file of another format or shape is an error, never read past."""
    path = ledger_path(workspace)
    if not path.is_file():
        return {"format": FORMAT, "verified": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("format") != FORMAT
        or not isinstance(data.get("verified"), dict)
    ):
        raise ValueError(f"{path} is not a livespec verification record of format {FORMAT}")
    return data


def links(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """Every requirement and the symbols that implement it, with their current hashes."""
    rows = conn.execute(
        "SELECT sp.spec_id, sp.title, sp.priority, s.qualified_name, s.body_hash, s.signature_hash, f.path "
        "FROM spec_symbol ss JOIN spec sp ON sp.id = ss.spec_id JOIN symbol s ON s.id = ss.symbol_id "
        "JOIN file f ON f.id = s.file_id WHERE sp.project_id = ? AND ss.relation = 'implements' "
        "AND sp.status != 'deprecated' ORDER BY sp.spec_id, s.qualified_name",
        (project_id,),
    ).fetchall()
    keys = ("spec", "title", "priority", "symbol", "body_hash", "signature_hash", "path")
    return [dict(zip(keys, tuple(r), strict=True)) for r in rows]


def record(
    workspace: Path,
    current: Iterable[dict[str, Any]],
    specs: Iterable[str],
    *,
    by: str,
    commit: str,
    evidence: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Mark `specs` verified on the code that implements them now, and write the record. Returns the
    entries written; a spec with no implementing symbol has nothing to verify and is said so by its absence."""
    if by not in BY:
        raise ValueError(f"`by` is one of {', '.join(BY)}")
    if not commit:
        raise ValueError("a verification names the commit it was checked on")
    wanted = set(specs)
    data = read(workspace)
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    written = []
    for link in current:
        if link["spec"] not in wanted:
            continue
        entry = {
            "body_hash": link["body_hash"],
            "signature_hash": link["signature_hash"],
            "path": link["path"],
            "commit": commit,
            "by": by,
            "evidence": evidence,
            "at": at,
        }
        data["verified"].setdefault(link["spec"], {})[link["symbol"]] = entry
        written.append({"spec": link["spec"], "symbol": link["symbol"], **entry})
    path = ledger_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return written


def stale(
    workspace: Path,
    current: Iterable[dict[str, Any]],
    *,
    strict: bool = False,
    require_test: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """The requirements whose implementing code changed since they were checked (`changed`), with
    `strict` the ones never checked (`never`), and those whose priority is in `require_test` but were
    checked by something other than a test (`untested`)."""
    verified = read(workspace)["verified"]
    demanding = set(require_test)
    out = []
    for link in current:
        seen = verified.get(link["spec"], {}).get(link["symbol"])
        base = {
            "spec": link["spec"],
            "title": link["title"],
            "symbol": link["symbol"],
            "path": link["path"],
        }
        if seen is None:
            if strict:
                out.append({**base, "reason": "never"})
            continue
        if (
            seen.get("body_hash") != link["body_hash"]
            or seen.get("signature_hash") != link["signature_hash"]
        ):
            out.append(
                {
                    **base,
                    "reason": "changed",
                    "verified_at": seen.get("commit"),
                    "by": seen.get("by"),
                }
            )
        elif link["priority"] in demanding and seen.get("by") != "test":
            out.append(
                {
                    **base,
                    "reason": "untested",
                    "verified_at": seen.get("commit"),
                    "by": seen.get("by"),
                }
            )
    return out
