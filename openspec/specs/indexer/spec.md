# Indexer Specification

## Purpose

The `indexer` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Scoped reference resolution

The livespec MCP server SHALL ensure that the system SHALL resolve call/usage references into `symbol_edge` rows with import-scoped precision and write edges with `INSERT OR IGNORE`.

#### Scenario: Partial reindex keeps edges

- **WHEN** a callee file changes while callers are unchanged
- **THEN** edges from unchanged callers to the updated callee remain after re-index

#### Scenario: An attribute call needs evidence of its receiver

- **WHEN** a call `X.f()` has a receiver that is a literal (`"".join(x)`), a name the file does not import, or any other object, and the project defines a module-level function `f`
- **THEN** no edge links that call to the function `f`
- **AND** a receiver the file imports (`import pkg.mod as m; m.f()`, `from pkg import mod; mod.f()`, an import inside the function) links to `f` in that module
- **AND** `self.f()` and a call on another object reach only methods, and `self.f()` in a class with no declared base only that class's own methods

#### Scenario: A call never crosses languages

- **WHEN** a TypeScript file calls `parts.join(",")` and the only `join` in the project is a Python function
- **THEN** no edge links the two
