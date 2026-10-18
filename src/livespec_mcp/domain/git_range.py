"""Git plumbing for a ``base..head`` range: name-status diff, ref resolution,
blob reads. Shared by ``git_diff_impact`` and tracks.

Failures come back as data (``GitProblem``) so ``domain/`` never imports the
MCP surface; the thin ``tools/`` wrappers shape them into ``mcp_error``.

Every caller-supplied ref goes after ``--end-of-options``: a ref beginning with
``-`` could otherwise be parsed as a git option (``--output=...`` is an
arbitrary file write).
"""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass


@dataclass
class GitProblem:
    """A git failure an MCP tool turns into ``mcp_error``."""

    error: str
    hint: str | None = None
    did_you_mean: list[str] | None = None


@dataclass
class DiffEntry:
    """One line of ``git diff --name-status -M``.

    ``status`` is the first letter (A, M, D, R, C, T). ``old_path`` is the
    base-side path (None for an add), ``new_path`` the head-side one (None for
    a delete). A rename or copy has both."""

    status: str
    old_path: str | None
    new_path: str | None

    @property
    def paths(self) -> list[str]:
        out: list[str] = []
        for p in (self.old_path, self.new_path):
            if p and p not in out:
                out.append(p)
        return out


def _git_problem_from(stderr: str, stdout: str, ws_root: str, base: str, head: str) -> GitProblem:
    stderr_lower = stderr.lower()
    if "not a git repository" in stderr_lower:
        return GitProblem(
            f"workspace is not a git repository: {ws_root}. "
            "git_diff_impact requires git history; run `git init` "
            "and at least one commit first."
        )
    if "unknown revision" in stderr_lower or "bad revision" in stderr_lower:
        return GitProblem(
            f"unknown git ref(s): base_ref='{base}', head_ref='{head}'. "
            "Check `git rev-parse` for both."
        )
    if "ambiguous argument" in stderr_lower:
        return GitProblem(
            f"ambiguous ref: '{base}..{head}'. Use full SHAs or branch names that exist locally."
        )
    first_line = next((ln for ln in (stderr or stdout).splitlines() if ln.strip()), "")
    return GitProblem(
        f"git diff failed: {first_line[:200]}"
        if first_line
        else "git diff failed (no diagnostic output)"
    )


def diff_name_status(
    ws_root: str, base_ref: str, head_ref: str
) -> tuple[list[DiffEntry] | None, GitProblem | None]:
    """``git diff --name-status -M base..head`` parsed into ``DiffEntry`` rows.

    ``-M`` detects renames so the OLD path (which holds the indexed symbols)
    is kept alongside the new one — a plain ``--name-only`` rename shows only
    the new path."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                ws_root,
                "diff",
                "--name-status",
                "-M",
                "--end-of-options",
                f"{base_ref}..{head_ref}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, GitProblem(
            "git not found on PATH",
            hint="install git and ensure it is on PATH for this MCP server process",
        )
    except subprocess.CalledProcessError as e:
        return None, _git_problem_from(
            (e.stderr or "").strip(), (e.stdout or "").strip(), ws_root, base_ref, head_ref
        )
    except subprocess.TimeoutExpired:
        return None, GitProblem(
            "git diff timed out after 10s",
            hint="narrow the ref range or check for a runaway git hook",
        )
    entries: list[DiffEntry] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("\t")]
        status = parts[0][:1]
        paths = [p for p in parts[1:] if p]
        if not paths:
            continue
        if status in ("R", "C") and len(paths) >= 2:
            entries.append(DiffEntry(status, paths[0], paths[1]))
        elif status == "A":
            entries.append(DiffEntry(status, None, paths[0]))
        elif status == "D":
            entries.append(DiffEntry(status, paths[0], None))
        else:
            entries.append(DiffEntry(status, paths[0], paths[0]))
    return entries, None


def changed_paths(entries: list[DiffEntry]) -> list[str]:
    """Every path a diff touched, old and new side, in diff order, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for e in entries:
        for p in e.paths:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _known_refs(ws_root: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", ws_root, "for-each-ref", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def resolve_commit(ws_root: str, ref: str) -> tuple[str | None, GitProblem | None]:
    """Full SHA of the commit ``ref`` names, or a problem with did_you_mean."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                ws_root,
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{ref}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, GitProblem(
            "git not found on PATH",
            hint="install git and ensure it is on PATH for this MCP server process",
        )
    except subprocess.TimeoutExpired:
        return None, GitProblem("git rev-parse timed out after 10s")
    sha = proc.stdout.strip()
    if proc.returncode == 0 and sha:
        return sha, None
    stderr = (proc.stderr or "").lower()
    if "not a git repository" in stderr:
        return None, GitProblem(
            f"workspace is not a git repository: {ws_root}",
            hint="tracks need git history; run `git init` and commit first",
        )
    refs = _known_refs(ws_root)
    close = difflib.get_close_matches(ref, refs, n=5, cutoff=0.5)
    return None, GitProblem(
        f"unknown git ref: '{ref}'",
        hint="pass a branch, tag or commit that `git rev-parse` resolves",
        did_you_mean=close,
    )


def commit_date(ws_root: str, sha: str) -> str | None:
    """Committer date of ``sha`` as ``YYYY-MM-DD``."""
    try:
        proc = subprocess.run(
            ["git", "-C", ws_root, "show", "-s", "--format=%cs", "--end-of-options", sha],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return out or None


def read_blobs(ws_root: str, specs: list[tuple[str, str]]) -> dict[tuple[str, str], str | None]:
    """Text of each ``(commit_sha, path)`` via one ``git cat-file --batch``.

    A missing blob maps to None. Paths with a newline cannot be addressed by
    the batch protocol and map to None too."""
    out: dict[tuple[str, str], str | None] = {}
    wanted = [s for s in dict.fromkeys(specs) if "\n" not in s[1]]
    for s in specs:
        out[s] = None
    if not wanted:
        return out
    payload = "".join(f"{sha}:{path}\n" for sha, path in wanted).encode("utf-8")
    try:
        proc = subprocess.run(
            ["git", "-C", ws_root, "cat-file", "--batch"],
            input=payload,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return out
    data = proc.stdout
    pos = 0
    for key in wanted:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        header = data[pos:nl].decode("utf-8", errors="replace").split()
        pos = nl + 1
        if len(header) == 3 and header[1] in ("blob", "tree", "commit", "tag"):
            size = int(header[2])
            body = data[pos : pos + size]
            pos += size + 1  # trailing LF after the content
            if header[1] == "blob":
                out[key] = body.decode("utf-8", errors="replace")
        # "<name> missing" / "ambiguous": no body follows
    return out
