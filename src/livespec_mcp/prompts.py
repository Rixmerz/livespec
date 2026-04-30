"""User-facing slash-command prompts."""

from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.prompt
    def onboard_project() -> str:
        """Walk a new project: index, list languages, surface top symbols, draft RFs."""
        return (
            "You're onboarding to a new repo through livespec-mcp. Steps:\n"
            "1) Call `index_project()` and report counts.\n"
            "2) Call `get_project_overview()` and summarize languages and top symbols.\n"
            "3) Call `list_requirements()` — if empty, suggest 3-5 candidate RFs based on top symbols.\n"
            "4) Ask the user which module they want to focus on next."
        )

    @mcp.prompt
    def analyze_change_impact(target: str) -> str:
        """Run impact analysis for a symbol/file/RF and explain blast radius."""
        return (
            f"Analyze the impact of changing `{target}`. Steps:\n"
            f"1) Detect target type (symbol qname, file path, or RF id).\n"
            f"2) Call `analyze_impact(target_type=..., target='{target}')`.\n"
            f"3) Summarize: who calls this, what RFs are affected, suggested test scope."
        )

    @mcp.prompt
    def audit_requirement_coverage() -> str:
        """List RFs without code links, and code modules without RF links."""
        return (
            "Audit traceability:\n"
            "1) `list_requirements(has_implementation=False)` — orphan RFs.\n"
            "2) For each top module, check if any RF maps via `get_requirement_implementation`.\n"
            "3) Output two tables: orphan RFs and uncovered modules."
        )

    @mcp.prompt
    def extract_requirements_from_module(module_or_path: str) -> str:
        """Infer candidate RFs by reading the public surface of a module."""
        return (
            f"Infer Functional Requirements from `{module_or_path}`. Steps:\n"
            f"1) `list_files(path_glob='{module_or_path}*')` and read public symbols via `find_symbol`.\n"
            f"2) Group by behavioral intent (auth, billing, ingestion, ...).\n"
            f"3) Draft 3-7 RFs (id, title, 1-line description, suggested module).\n"
            f"4) Ask the user which to persist via `create_requirement`."
        )

    @mcp.prompt
    def document_undocumented_symbols(module_glob: str = "*") -> str:
        """Find symbols without a doc and generate one for each."""
        return (
            f"Document missing symbols in `{module_glob}`. Steps:\n"
            f"1) `list_docs(target_type='symbol')` -> set of already-documented qnames.\n"
            f"2) `list_files(path_glob='{module_glob}')` then `find_symbol(query='*')` to enumerate.\n"
            f"3) For each undocumented function/class above PageRank threshold, "
            f"call `generate_docs_for_symbol`.\n"
            f"4) Report counts."
        )

    @mcp.prompt
    def refresh_stale_docs() -> str:
        """Detect stale docs and regenerate them."""
        return (
            "Refresh drifted docs. Steps:\n"
            "1) `detect_stale_docs(target_type='all')` — list drift.\n"
            "2) For each, run `generate_docs_for_symbol` or `generate_docs_for_requirement`.\n"
            "3) Report a diff summary."
        )

    @mcp.prompt
    def explain_symbol(qname: str) -> str:
        """One-pass explanation: code + callers + RFs touched."""
        return (
            f"Explain `{qname}` end-to-end:\n"
            f"1) `get_symbol_info(identifier='{qname}', detail='full')`.\n"
            f"2) `find_references(identifier='{qname}')` for caller context.\n"
            f"3) `analyze_impact(target_type='symbol', target='{qname}')` for blast radius.\n"
            f"4) Synthesize: purpose, who depends on it, which RFs are affected."
        )
