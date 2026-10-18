"""`livespec ui` — one local, read-only web view over every indexed project.

The projects come from the user-level registry (`livespec_mcp.registry`), and
each project's data from its own index opened read-only
(`livespec_mcp.storage.readonly`). Nothing is generated into, or served from,
any repository: the page is static package data (`ui/static/`), the numbers are
queried on request.
"""
