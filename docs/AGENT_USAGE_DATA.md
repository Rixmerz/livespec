# Agent Usage Data — v0.8 P2 battle-test

This file is the field log behind the v0.8 curation pass. It replaces
the opinion-based tier list (ROADMAP §6) with what real agent sessions
actually do across multiple unfamiliar codebases.

## How this gets filled

1. Pick a target codebase (Django subset, Next.js boilerplate, a large Rust monorepo
   subset, etc.). Clone it, set `LIVESPEC_WORKSPACE` to its path.
2. Run an agent session: a real bug-fix, feature, refactor, or
   exploration task — not a synthetic benchmark. Let the agent call
   tools naturally.
3. The middleware (`src/livespec_mcp/instrumentation.py`) writes one
   JSONL line per call into `<workspace>/.mcp-docs/agent_log.jsonl`.
4. After 3-5 sessions per codebase, run:
   ```bash
   uv run python bench/agent_log_analyze.py \
       /path/to/codebase1 /path/to/codebase2 ... \
       --json bench/agent_usage.json
   ```
5. Update the **Findings** section below with the data.

The redaction step in the middleware rewrites absolute paths to
`<workspace>` so the aggregated JSON can leave the user's machine
without leaking home-directory layout.

## Target codebases (planned)

| Codebase | Language | Lines | Tasks |
|---|---|---:|---|
| Django subset | Python | ~250K | feature, bugfix, refactor |
| Next.js boilerplate | TypeScript | ~30K | feature, bugfix, refactor |
| a large Rust monorepo subset | Rust | ~500K | feature, bugfix, refactor |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |

Pick TBD slots in-session. One should be a JS/TS app of moderate
size; another should be a language under-represented in the others
(Go or Java).

## Findings

_To be filled after the battle-test sessions complete. Suggested
sections:_

### Tool ranking (data-driven)

_Insert the top of the `bench/agent_log_analyze.py` Markdown table
here. Note any tool with **calls = 0** across all sessions —
that's a drop candidate per ROADMAP §4 Pillar A._

### Common follow-up patterns

_Top 5-10 `A -> B` pairs from the analyzer. If `find_symbol ->
get_symbol_info` is ubiquitous, that's evidence `quick_orient`
should be the default first call. If `index_project ->
propose_requirements_from_codebase` is the typical brownfield
opener, document it as the canonical `AGENT_QUICKSTART.md` flow._

### Tier classification (post-data)

_Match each tool against the v0.8 plan in CLAUDE.md `## Tool tier
vision`:_

- _Tools that data-validates as tier-1 (called in ≥X% of sessions):_
- _Tools that should remain in tier-1 despite low call count
  (RF tools that answer the README's lead question — `get_requirement_implementation`, `list_requirements`):_
- _Tools to move to plugin (called only in ≥1 session AND when
  matching DB state condition):_
- _Tools to drop (never called, or always paired with a different
  tool that subsumes them):_

### Latency / payload outliers

_Anything with p95 latency > 2s or max result_chars > 100K is a
candidate for further pagination work or graph cache investigation._

### Surprises

_What did the data say that contradicted prior intuition? ROADMAP
§6 already identified two biases (recency, agentic survivor) — list
any new ones surfaced here so v0.9 doesn't repeat them._

## Methodology notes

- **`result_cited_in_final_answer`** is NOT recorded by the
  middleware. It's a post-hoc annotation: read the agent's final
  text output, look for qnames or RF ids that match the tool result,
  flag those calls as cited. A tool whose results never appear in
  the final answer is suspect — it's noise, not signal.
- **Sessions** are bucketed by `session_id` (FastMCP-assigned).
  The follow-up pair analysis only counts pairs where both calls
  share a session — cross-session sequences are noise.
- **Counts are agent calls, not unique calls.** A tool called 50
  times in one session by a flailing agent looks the same as a tool
  called 50 times across 50 different sessions. Sample size matters;
  prefer raw count + sessions-with-call.
