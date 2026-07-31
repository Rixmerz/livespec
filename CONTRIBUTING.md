# Contributing to livespec

Thanks for wanting to push this forward. This guide is short on ceremony and
long on one thing: **what counts as proof that a change is good**.

## The rule that matters

> A green test suite is necessary, not sufficient. Every change ships with
> evidence gathered by running livespec against a **real application
> surface** — an actual repository, through the MCP wire — not only against
> fixtures.

Fixtures prove the code does what you wrote. Real repos prove the code does
what an agent needs. Most of the bugs that shaped this project (resolver
fan-out, Django class-based views, bundler output polluting `top_symbols`,
payload overflow at 40K symbols) were invisible in the test suite and obvious
after ten minutes on a real codebase.

## Three legitimate outcomes

Running the evidence loop is not a formality you perform to get a "keep"
verdict. All three of these are successful contributions:

1. **Keep / justify** — the tool answers a question an agent actually asks,
   and now you have the numbers to say so.
2. **Improve** — the signal is real but noisy, slow, or overflowing. You fix
   the specific failure the evidence surfaced.
3. **Delete** — the tool was silent across every profile, or another tool
   answers the same question in fewer calls. **Removing a tool is a
   first-class contribution**, not a failure. This project has dropped more
   tools than most projects ship.

Do not curate on intuition. The v0.8 tier split was rewritten from scratch
after five logged sessions contradicted the opinion-based list.

## The evidence loop

1. **Baseline.** Pick at least two real repositories (see profiles below).
   Index them and record the numbers your change should move: counts,
   latency, payload size, false positives you can name.
2. **Change the code.**
3. **Re-run the same repositories.** Record the after-numbers and the delta.
   A delta you cannot explain is a bug you have not found yet.
4. **Write the regression test.** It must fail without your change. A fix
   without a failing-first test is not finished.
5. **Write down what you learned.** Small change: a `CHANGELOG.md` bullet
   with the delta. Multi-repo sweep: a section in
   [`docs/AGENT_USAGE_DATA.md`](docs/AGENT_USAGE_DATA.md), which is the
   field log this methodology comes from.

### Profiles worth covering

A single repository is one data point, and usually the flattering one. Aim
to spread across profiles rather than pile up repos of the same shape.

| Profile | What it stresses |
|---|---|
| Exploration | first-contact tools: `quick_orient`, `find_symbol`, `who_does_this_call` |
| Refactor | backward cone: `who_calls`, `analyze_impact`, `find_dead_code` |
| Bugfix at scale (30K+ symbols) | pagination, payload size, graph cache, latency |
| Spec-active repo | `list_specs`, `get_spec_implementation`, `audit_coverage`, OpenSpec sync |
| Non-Python | tree-sitter extractors, framework detection outside the Python path |
| Polyrepo (`group_db`) | cross-repo route joins, `find_legacy_flows` |

### What good evidence looks like

Real examples from this repository's history — this is the bar:

| Change | Evidence that justified it |
|---|---|
| Resolver same-file disambiguation | edges on livespec itself 969 → 742 (−23%), call cones visibly clean |
| Entry-point detection batch | `find_dead_code` on livespec 18 → 1 candidate (−94%) |
| Django class-based view support | `find_endpoints(framework="django")` 20 → 162; `find_dead_code` 824 → 514 |
| Pagination contract | 4M–7M-character payloads on a large Rust monorepo, reproduced and bounded |

Note the shape: a named repository profile, a metric, a before, an after.
"Feels better" and "should be faster" are not evidence.

## Evidence bar by change type

| Change | Minimum evidence |
|---|---|
| **New tool** | Exercised in ≥2 profiles, plus the question it answers that no existing tool answers in ≤2 calls. Cite the actual result. |
| **Tool behaviour change** | Before/after counts on ≥2 real repos. |
| **Tool removal** | Silent across ≥4 profiles, or demonstrably subsumed by another tool in ≤2 calls. |
| **New language / extractor** | Fixture tests **and** one real repo in that language, with symbol counts you have sanity-checked by hand. |
| **Resolver / graph change** | Edge-count delta plus a spot check of one known call cone. |
| **Performance** | `uv run python bench/run.py` before and after, same machine. |
| **Bug fix** | The failing test, written first. |
| **Docs only** | None. |

## Contracts you must not break

These are load-bearing. A PR that violates one gets rejected regardless of
how good the feature is. Details and rationale live in
[`CLAUDE.md`](CLAUDE.md).

- **Error shape** — every tool error goes through `tools/_errors.py:mcp_error()`.
  No custom error dictionaries.
- **Pagination** — any tool returning an unbounded collection accepts
  `limit` / `cursor` / `summary_only`, and counts stay exact regardless of
  pagination.
- **Migrations are append-only** — new tuple in `storage/db.py:MIGRATIONS`,
  never reuse or reorder a version number.
- **The ref resolver is `INSERT OR IGNORE` only** — never `DELETE` from
  `symbol_edge` during resolution, or edges from unchanged files vanish.
- **`body_hash` invariance** — reformatting must not drift the hash; a real
  semantic change must.
- **`workspace` is required** on every tool, with no environment or
  current-directory fallback.

New runtime dependencies need a justification in the PR description. The
default answer is no.

## Local gates

```bash
uv run pytest -q                      # full suite, must be green (639 tests today)
uv run ruff check src tests           # same check CI runs
uv build                              # packaging regression gate
uv run python bench/run.py --quick    # ~30s, when you touched anything hot
```

CI runs ruff, a wheel-contents check, and pytest on Python 3.10 / 3.11 /
3.12. Never bypass the pre-commit hook with `--no-verify`; fix the cause.

When you change tool code, the MCP server running inside your editor is
still executing the old module. Reconnect it (`/mcp` in Claude Code, or
restart the client) before you trust any evidence you collect.

## Privacy rules for evidence

Evidence usually comes from repositories that are not yours to publish.
Nothing that identifies them may enter this repository — not in code, tests,
docs, comments, fixtures, or commit messages.

- **No absolute local paths.** Not `/Users/you/...`, not `/home/you/...`.
  Scripts under `scripts/` read workspace paths from environment variables.
- **No employer, client, or private repository names.** Describe the shape
  instead: "a large Rust monorepo", "a Python API service", "a Deno Fresh
  app". That is how every session in the field log is written.
- **Redact paths in captured output.** The instrumentation middleware
  rewrites absolute paths to `<workspace>` for exactly this reason — keep
  that property when you add new logging.

If you are unsure whether a detail is safe, genericize it. A vaguer sentence
costs nothing; a leak is permanent.

## Documentation you must update

Same commit batch as the code, not "next session":

| Trigger | Update |
|---|---|
| Any code landing on `main` | `HANDOFF.md` section 3 (state, HEAD, test count) |
| Behaviour a user or agent would notice | `CHANGELOG.md` under `[Unreleased]` |
| Public surface changed (tool list, install, headline numbers) | `README.md` |
| Strategy moved (pillar advanced, feature added to or cut from v1.0) | `ROADMAP.md` |
| Multi-repo evidence sweep | `docs/AGENT_USAGE_DATA.md` |

Version numbers in `pyproject.toml` change only when cutting a tag.

## Commits and pull requests

Commit subject follows the repository's existing style — `v0.X PN: short
summary` for phase work — with a body that explains each subtask and why.
Test counts go at the bottom.

Your PR description should let a reviewer skip re-deriving your reasoning:

- What changed and why.
- **The evidence**: repos (genericized), metric, before, after.
- Which contract from the list above the change touches, if any.
- Which docs you updated.

## Reporting a problem without fixing it

That is genuinely useful, and the report is better than a vague issue if it
carries the same evidence shape:

- Repository profile and size (language, files, symbols, edges).
- The exact tool call, with arguments.
- What you expected, what you got, and — if you can — which results were
  false positives and why.

That is precisely the format that turned into the ten bug fixes behind v0.8
and v0.9.
