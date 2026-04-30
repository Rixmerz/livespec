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
