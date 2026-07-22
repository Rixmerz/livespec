---
description: Index the current repo with livespec and report an orientation summary
argument-hint: "[absolute repo path — defaults to cwd]"
---

Onboard this repository with livespec code intelligence.

Delegate to the **livespec** subagent with this task:

> Workspace: `$1` (if empty, use the current working directory's git root).
> Do a cold open: `index_project` → `get_project_overview` → `list_specs`.
> Report file/symbol/edge counts, languages detected, the top entry-point
> symbols, and Spec totals (with coverage gaps if any). Keep it to a tight
> orientation briefing I can act on.
