# Analysis Specification

## Purpose

The `analysis` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Dead-code & coverage analysis

The livespec MCP server SHALL ensure that the system SHALL detect unreachable symbols (`find_dead_code`), audit Spec coverage (`audit_coverage`), and surface orphan tests (`find_orphan_tests`).

#### Scenario: Dead-code sweep

- **WHEN** an indexed project has unused private helpers
- **THEN** `find_dead_code` returns those helpers as candidates without claiming production traffic proof

#### Scenario: Test symbols excluded from dead-code by default

- **WHEN** symbols under a test path (`tests/`, `src/test/`, `*.test.ts`, `*Test.java`, …) have zero production callers
- **THEN** `find_dead_code` SHALL NOT list them as dead candidates by default
- **AND** `find_dead_code(include_tests=True)` SHALL include them

#### Scenario: Only tests are orphan-test candidates

- **WHEN** a test file defines helpers, fakes, fixtures or nested functions beside its tests
- **THEN** `find_orphan_tests` SHALL consider only what the runner collects (pytest `test*` functions and `test*` methods of a class, Go `TestX`, `@Test`, a function passed to `test(`/`it(` by name)
- **AND** the helpers are counted in `test_helper_symbols_skipped`, never reported as orphan tests

### Requirement: Endpoint discovery (framework-aware)

The livespec MCP server SHALL ensure that the system SHALL discover HTTP/CLI entry points across supported frameworks via `find_endpoints`, including Express/Hono in the default sweep.

#### Scenario: Default sweep

- **WHEN** a repo defines Express or Hono `router.get` routes
- **THEN** `find_endpoints()` without framework filter includes those routes

#### Scenario: Spring DI stereotypes are not listed as HTTP endpoints

- **WHEN** a Java file has `@Bean`, `@Configuration`, `@Service`, `@SpringBootApplication`, or `@Component` without HTTP mapping annotations
- **THEN** `find_endpoints()` and `find_endpoints(framework="spring")` SHALL NOT list those symbols as endpoints
- **AND** `@RestController` / `@GetMapping` (and other HTTP mappings) SHALL still be listed
- **AND** Spring DI stereotypes SHALL remain protected from `find_dead_code` (zero in-project callers is expected)

#### Scenario: Angular UI is opt-in

- **WHEN** a TypeScript file has `@Component` / `@Injectable` classes
- **THEN** `find_endpoints()` without framework SHALL NOT list them
- **AND** `find_endpoints(framework="angular")` SHALL list them

#### Scenario: Click and FastMCP are opt-in

- **WHEN** a Python module defines `@click.command` and `@mcp.tool` alongside a FastAPI route
- **THEN** `find_endpoints()` without framework SHALL list the FastAPI route and SHALL NOT list the Click/MCP symbols
- **AND** `framework="click"` / `framework="fastmcp"` SHALL list the respective symbols

#### Scenario: Go call-style HTTP routes

- **WHEN** a Go file registers `r.GET("/x", h)` (gin) or `http.HandleFunc("/y", h)`
- **THEN** `find_endpoints()` SHALL include those routes with `http_method` / `http_path`
- **AND** `framework="gin"` / `framework="nethttp"` SHALL filter accordingly

### Requirement: Impact analysis

The livespec MCP server SHALL ensure that the system SHALL answer blast-radius questions via `analyze_impact`, `git_diff_impact`, `who_calls`, and `who_does_this_call`; `git_diff_impact` SHALL report as changed only the symbols the range added, modified or removed, not every symbol of a changed file.

#### Scenario: Who calls

- **WHEN** symbol A calls symbol B in the index
- **THEN** `who_calls(B)` includes A in the caller set

#### Scenario: Untouched function in a changed file

- **WHEN** a range changes one function of a file and leaves another untouched
- **THEN** `git_diff_impact` lists the changed function in `changed_symbols` and not the untouched one

#### Scenario: Editing a spec's own definition

- **WHEN** a range adds a scenario to a requirement in `openspec/specs/<capability>/spec.md`
- **THEN** `git_diff_impact` lists that requirement in `spec_definitions_changed` as `modified`, apart from the `affected_specs` reached through code
- **AND** the PR comment counts it as a definition change, even when the index never imported the spec
