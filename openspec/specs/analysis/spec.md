# Analysis Specification

## Purpose

The `analysis` capability of livespec (dogfood).

## Requirements

### Requirement: Dead-code & coverage analysis

<!-- livespec:id=SPEC-007 -->

Detect unreachable / unused symbols (`find_dead_code`), audit Spec
coverage of the codebase (`audit_coverage`), and surface tests that
exercise no linked symbol (`find_orphan_tests`).

### Requirement: Endpoint discovery (framework-aware)

<!-- livespec:id=SPEC-008 -->

Discover HTTP/route endpoints across 14 frameworks (Flask, FastAPI,
Click, Django, Next.js, Deno Fresh, SvelteKit, Remix, Spring Boot,
Angular, Hono, etc.) via `find_endpoints`, including decorator and alias
detection.

### Requirement: Impact analysis

<!-- livespec:id=SPEC-009 -->

Answer "what breaks if I change this?" via `analyze_impact`,
`git_diff_impact`, and the `who_calls` / `who_does_this_call` traversals
over the call graph.
