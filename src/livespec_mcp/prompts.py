"""User-facing slash-command prompts."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from fastmcp import FastMCP

# Checkout/editable installs read docs/; wheel installs ship a packaged copy
# under livespec_mcp/templates/ (pyproject force-include) because parents[2]
# resolves to site-packages' parent there, where docs/ does not exist.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_PLAYBOOK = _REPO_ROOT / "docs" / "AGENT_PLAYBOOK.md"


def _load_agent_playbook() -> str:
    try:
        res = resources.files("livespec_mcp") / "templates" / "AGENT_PLAYBOOK.md"
        if res.is_file():
            return res.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        pass
    if _AGENT_PLAYBOOK.is_file():
        return _AGENT_PLAYBOOK.read_text(encoding="utf-8")
    return (
        "Agent playbook file missing. See docs/AGENT_QUICKSTART.md and README.md "
        "in the livespec repository."
    )


def register(mcp: FastMCP) -> None:
    @mcp.prompt
    def agent_playbook() -> str:
        """How to use livespec tools; OpenSpec authoring first; @spec: linking.

        Invoke at the start of a session on any livespec-indexed repo. Covers cold-open
        tool patterns, OpenSpec as preferred Spec authoring SSoT (livespec is the engine
        beneath), OpenSpec Markdown import, anti-patterns, and
        brownfield onboarding.
        """
        return _load_agent_playbook()

    @mcp.prompt
    def onboard_project() -> str:
        """Walk a new project: index, list languages, surface top symbols, draft Specs."""
        return (
            "You're onboarding to a new repo through livespec. Steps:\n"
            "1) Call `index_project()` and report counts.\n"
            "2) Call `get_project_overview()` and summarize languages and top symbols.\n"
            "3) Call `list_specs()` — if empty, prefer drafting OpenSpec under\n"
            "   `openspec/specs/<capability>/spec.md` then `sync_openspec()` (not\n"
            "   create_spec-first). Spec ids are OpenSpec slugs only.\n"
            "4) If `openspec/` already exists, run `sync_openspec()` + `validate_openspec`.\n"
            "5) Report Spec totals and suggest `@spec:` / bulk links for top symbols."
        )

    @mcp.prompt
    def openspec_workflow() -> str:
        """OpenSpec (Fission-AI) interop loop: sync -> trace -> validate -> export.

        Invoke when the repo has an `openspec/` directory. livespec is the
        code-graph/traceability layer BENEATH OpenSpec — it reads and writes the
        OpenSpec format and does not compete with OpenSpec authoring.
        """
        return (
            "This repo uses OpenSpec (Fission-AI). livespec is the code-graph +\n"
            "traceability layer beneath it — it reads AND writes the OpenSpec\n"
            "format. Run the loop:\n\n"
            "1) Ingest the whole tree in one call:\n"
            "   `sync_openspec()` — canonical requirements from openspec/specs/ PLUS\n"
            "   every change under openspec/changes/ (proposed) and openspec/archive/\n"
            "   (archived). Reads openspec.json. (Single file: import_specs_from_markdown.)\n"
            "2) Understand: `list_specs()` (rows carry scenario_count) and\n"
            "   `get_spec_implementation(spec_id)` — returns the requirement, its\n"
            "   #### Scenario: blocks with per-scenario linked symbols + a `verified`\n"
            "   flag, and coverage.scenarios_verified.\n"
            "3) Trace at scenario granularity (OpenSpec's atomic unit):\n"
            "   `link_scenario_symbol(spec_id, scenario_name, symbol_qname,\n"
            "   relation='tests')` — link a test/impl to a single WHEN/THEN scenario.\n"
            "4) Change lifecycle: `list_spec_changes()` / `get_spec_change(name)` to\n"
            "   inspect; `apply_spec_change(name)` folds deltas into the canonical spec\n"
            "   set (ADDED/MODIFIED/RENAMED activate, REMOVED deprecate);\n"
            "   `archive_spec_change(name)` closes it.\n"
            "5) Validate: `validate_openspec(strict=True)` — mirrors\n"
            "   `openspec validate --strict`; every requirement MUST have >=1 scenario.\n"
            "6) Write back: `export_openspec(out_dir='openspec')` re-emits the canonical\n"
            "   tree + changes/ + archive/ — closing the round-trip.\n\n"
            "Mapping: OpenSpec capability == livespec module (pass `capability=` or\n"
            "`module=`); requirement name slugs to spec_id (auth + 'User Login' ->\n"
            "auth-user-login). All idempotent — re-sync freely. Pass workspace= on\n"
            "every call."
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
    def audit_spec_coverage() -> str:
        """List Specs without code links, and code modules without Spec links."""
        return (
            "Audit traceability:\n"
            "1) `list_specs(has_implementation=False)` — orphan Specs.\n"
            "2) For each top module, check if any Spec maps via `get_spec_implementation`.\n"
            "3) Output two tables: orphan Specs and uncovered modules."
        )

    @mcp.prompt
    def extract_specs_from_module(module_or_path: str) -> str:
        """Infer candidate Specs by reading the public surface of a module."""
        return (
            f"Infer Specs from `{module_or_path}`. Steps:\n"
            f"1) `propose_specs_from_codebase(scope='{module_or_path}')` —\n"
            f"   heuristic groups + suggested symbols. If nothing is returned,\n"
            f"   fall back to `find_symbol(query='*')` to enumerate by hand.\n"
            f"2) Group by behavioral intent (auth, billing, ingestion, ...).\n"
            f"3) Draft 3-7 Specs (id, title, 1-line description, suggested module).\n"
            f"4) Ask the user which to persist as OpenSpec under\n"
            f"   `openspec/specs/<capability>/spec.md`, then `sync_openspec()`\n"
            f"   (or `import_specs_from_markdown(..., fmt='auto')`). Prefer that\n"
            f"   over `create_spec` for new work — OpenSpec markdown is the\n"
            f"   authoring SSoT; livespec is the engine beneath."
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
