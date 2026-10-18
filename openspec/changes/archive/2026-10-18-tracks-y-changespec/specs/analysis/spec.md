## MODIFIED Requirements

### Requirement: Impact analysis

The livespec MCP server SHALL ensure that the system SHALL answer blast-radius questions via `analyze_impact`, `git_diff_impact`, `who_calls`, and `who_does_this_call`; `git_diff_impact` SHALL report as changed only the symbols the range added, modified or removed, not every symbol of a changed file.

#### Scenario: Who calls

- **WHEN** symbol A calls symbol B in the index
- **THEN** `who_calls(B)` includes A in the caller set

#### Scenario: Untouched function in a changed file

- **WHEN** a range changes one function of a file and leaves another untouched
- **THEN** `git_diff_impact` lists the changed function in `changed_symbols` and not the untouched one
