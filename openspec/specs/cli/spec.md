# Cli Specification

## Purpose

The `cli` capability of livespec (dogfood).

## Requirements

### Requirement: Headless CLI

<!-- livespec:id=SPEC-012 -->

Provide a headless `livespec-mcp index` / `livespec-mcp status` entry
point that shares the same indexing pipeline as the MCP server, for use
in CI or scripted environments without an MCP host.
