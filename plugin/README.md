# livespec — Claude Code plugin

Packages the **livespec-mcp** server as a Claude Code plugin, plus a specialized
subagent and a preloaded Skill so agents make better decisions about *how* to use
the tools.

## What's inside

```
plugin/
├── .claude-plugin/plugin.json   # plugin manifest
├── .mcp.json                    # runs the livespec-mcp server (uvx livespec-mcp)
├── agents/livespec.md           # specialized subagent; preloads the livespec Skill
├── skills/livespec/SKILL.md     # operating manual: tool map, workspace rule, contracts
└── commands/livespec-onboard.md # /livespec-onboard [repo] — cold-open + orientation
```

### The subagent + preloaded Skill

`agents/livespec.md` declares `skills: [livespec]` in its frontmatter. Per the
[Claude Code subagents docs](https://code.claude.com/docs/en/sub-agents), listed
skills are **preloaded** — the *full* Skill content is injected into the subagent's
context at startup, not just its description. The subagent can still invoke other
project/user/plugin skills on demand via the `Skill` tool.

This gives the subagent livespec's tool map, the required-`workspace` rule, the
pagination contract, and the cold-open workflow up front, so it proceeds correctly
without rediscovering conventions each time.

## Install

The `.mcp.json` runs `uvx livespec-mcp` (the server is on PyPI). Ensure `uv` is
installed. Then add this plugin to a marketplace and install it, or point Claude
Code at this directory as a local plugin.

The MCP server exposes 43 tools (29 core + 11 Spec + 3 docs). See the repo
`README.md`, `docs/AGENT_PLAYBOOK.md`, and `CHANGELOG.md` for the full surface.
