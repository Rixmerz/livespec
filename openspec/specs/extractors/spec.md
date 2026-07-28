# Extractors Specification

## Purpose

The `extractors` capability of livespec (dogfood).

## Requirements

### Requirement: Symbol extraction (9 languages)

<!-- livespec:id=SPEC-002 -->

Extract functions, classes and methods — together with their decorators,
annotations and signatures — using tree-sitter for JS/TS/Go/Ruby/PHP/
Rust/Java and the Python `ast` module for Python precision. Supports the
9 languages with passing extractor tests.
