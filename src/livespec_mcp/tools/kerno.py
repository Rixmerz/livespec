"""Experimental Kerno-backed history tools (``LIVESPEC_KERNO=1``).

Registered only when the experiment is on, so the default tool surface and its
tool-count / agent-docs contracts are unchanged. The domain lives in
``domain/kerno_history.py``; this module only shapes results and errors.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from livespec_mcp.domain import kerno_history as kh
from livespec_mcp.state import get_state
from livespec_mcp.tools._errors import mcp_error
from livespec_mcp.workspace_param import Workspace

KERNO_TOOL_NAMES = ("kerno_symbol_revisions", "kerno_symbol_as_of", "kerno_verify_history")

_RO = {"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}


def _shape(result: dict[str, Any] | kh.KernoProblem) -> dict[str, Any]:
    if isinstance(result, kh.KernoProblem):
        return mcp_error(result.message, hint=result.hint)
    return result


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=_RO)
    def kerno_symbol_revisions(
        qname: str, limit: int = 20, workspace: Workspace | None = None
    ) -> dict[str, Any]:
        """[experimental, Kerno] Every semantic revision of a symbol across index runs.

        One revision per change of body, signature or kind seen by
        index_project (reformatting does not count), newest first, with the
        index run that observed it and Kerno's verified content hash. Unlike
        get_symbol_history (git range), this needs no git: it is what the
        indexer itself saw. Use it to answer "when did this function last
        really change?" between commits that were never recorded.
        """
        st = get_state(workspace)
        return _shape(kh.symbol_revisions(st.project_id, st.settings.db_path, qname, limit))

    @mcp.tool(annotations=_RO)
    def kerno_symbol_as_of(
        qname: str, index_run: int, workspace: Workspace | None = None
    ) -> dict[str, Any]:
        """[experimental, Kerno] A symbol as it was at an earlier index run.

        Reconstructs the symbol's signature, body hash and lines as of
        ``index_run`` (an id from the index_run table / index_project's
        last_run) with a Kerno as-of read, and says whether it is the same as
        now. "absent" means it did not exist yet; "deleted" that it was removed.
        """
        st = get_state(workspace)
        return _shape(kh.symbol_as_of(st.project_id, st.settings.db_path, qname, index_run))

    @mcp.tool(annotations=_RO)
    def kerno_verify_history(workspace: Workspace | None = None) -> dict[str, Any]:
        """[experimental, Kerno] Verify the integrity of the Kerno history store.

        Runs Kerno's units-layer verification (checksums, revisions, relations)
        and returns its counts and any findings. clean=True means every record
        checked out.
        """
        st = get_state(workspace)
        return _shape(kh.verify(st.settings.db_path))
