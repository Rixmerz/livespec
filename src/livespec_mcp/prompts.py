"""User-facing slash-command prompts."""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_PLAYBOOK = _REPO_ROOT / "docs" / "AGENT_PLAYBOOK.md"


def _load_agent_playbook() -> str:
    if not _AGENT_PLAYBOOK.is_file():
        return (
            "Agent playbook file missing. See docs/AGENT_QUICKSTART.md and README.md "
            "in the livespec-mcp repository."
        )
    return _AGENT_PLAYBOOK.read_text(encoding="utf-8")


def register(mcp: FastMCP) -> None:
    @mcp.prompt
    def agent_playbook() -> str:
        """How to use livespec tools and how to comment/link code (@spec: annotations).

        Invoke at the start of a session on any livespec-indexed repo. Covers cold-open
        tool patterns, Spec traceability in docstrings, Markdown Spec import, anti-patterns,
        and brownfield onboarding — the operational guide agents should follow.
        """
        return _load_agent_playbook()

    @mcp.prompt
    def onboard_project() -> str:
        """Walk a new project: index, list languages, surface top symbols, draft Specs."""
        return (
            "You're onboarding to a new repo through livespec-mcp. Steps:\n"
            "1) Call `index_project()` and report counts.\n"
            "2) Call `get_project_overview()` and summarize languages and top symbols.\n"
            "3) Call `list_specs()` — if empty, suggest 3-5 candidate Specs based on top symbols.\n"
            "4) Ask the user which module they want to focus on next."
        )

    @mcp.prompt
    def analyze_change_impact(target: str) -> str:
        """Run impact analysis for a symbol/file/Spec and explain blast radius."""
        return (
            f"Analyze the impact of changing `{target}`. Steps:\n"
            f"1) Detect target type (symbol qname, file path, or Spec id).\n"
            f"2) Call `analyze_impact(target_type=..., target='{target}')`.\n"
            f"3) Summarize: who calls this, what Specs are affected, suggested test scope."
        )

    @mcp.prompt
    def audit_requirement_coverage() -> str:
        """List Specs without code links, and code modules without Spec links."""
        return (
            "Audit traceability:\n"
            "1) `list_specs(has_implementation=False)` — orphan Specs.\n"
            "2) For each top module, check if any Spec maps via `get_spec_implementation`.\n"
            "3) Output two tables: orphan Specs and uncovered modules."
        )

    @mcp.prompt
    def extract_requirements_from_module(module_or_path: str) -> str:
        """Infer candidate Specs by reading the public surface of a module."""
        return (
            f"Infer Functional Requirements from `{module_or_path}`. Steps:\n"
            f"1) `propose_specs_from_codebase(scope='{module_or_path}')` —\n"
            f"   heuristic groups + suggested symbols. If nothing is returned,\n"
            f"   fall back to `find_symbol(query='*')` to enumerate by hand.\n"
            f"2) Group by behavioral intent (auth, billing, ingestion, ...).\n"
            f"3) Draft 3-7 Specs (id, title, 1-line description, suggested module).\n"
            f"4) Ask the user which to persist via `create_spec`."
        )

    @mcp.prompt
    def document_undocumented_symbols(module_glob: str = "*") -> str:
        """Find symbols without a doc and generate one for each."""
        return (
            f"Document missing symbols in `{module_glob}`. Steps:\n"
            f"1) `list_docs(target_type='symbol')` -> set of already-documented qnames.\n"
            f"2) `find_symbol(query='*')` to enumerate; pair with `quick_orient`\n"
            f"   for first-contact metadata.\n"
            f"3) For each undocumented function/class above PageRank threshold:\n"
            f"   a) `get_symbol_source(qname=...)` to read the body.\n"
            f"   b) Write Markdown docs yourself based on the source.\n"
            f"   c) `generate_docs(target_type='symbol', identifier=qname, content=<markdown>)`.\n"
            f"      Without `content` the tool will try MCP sampling; in Claude Code\n"
            f"      that returns the prompt for you to write and retry.\n"
            f"4) Report counts."
        )

    @mcp.prompt
    def refresh_stale_docs() -> str:
        """Detect stale docs and regenerate them."""
        return (
            "Refresh drifted docs. Steps:\n"
            "1) `list_docs(target_type='all', only_stale=True)` — list drift.\n"
            "2) For each, regenerate with `generate_docs(target_type=..., identifier=...)`.\n"
            "3) Report a diff summary."
        )

    @mcp.prompt
    def explain_symbol(qname: str) -> str:
        """One-pass explanation: code + callers + Specs touched."""
        return (
            f"Explain `{qname}` end-to-end:\n"
            f"1) `quick_orient(qname='{qname}')` — metadata, top callers/callees,\n"
            f"   linked Specs, entry-point flag.\n"
            f"2) `get_symbol_source(qname='{qname}')` to read the body.\n"
            f"3) `analyze_impact(target_type='symbol', target='{qname}')` for the full\n"
            f"   blast radius (transitive callers + Spec rollup).\n"
            f"4) Synthesize: purpose, who depends on it, which Specs are affected."
        )
