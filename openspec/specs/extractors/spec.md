# Extractors Specification

## Purpose

The `extractors` capability of livespec (dogfood OpenSpec SSoT).

## Requirements

### Requirement: Symbol extraction (9 languages)

The livespec MCP server SHALL ensure that the system SHALL extract functions, classes and methods (with decorators/annotations/signatures) via tree-sitter for JS/TS/Go/Ruby/PHP/Rust/Java and Python `ast` for Python.

#### Scenario: Multi-language extract

- **WHEN** a supported source file is indexed
- **THEN** its top-level symbols appear in `symbol` with stable body hashes
