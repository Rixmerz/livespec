# livespec — Claude Code plugin

Packages the **livespec** MCP server as a Claude Code plugin, plus a specialized
subagent and a preloaded Skill so agents make better decisions about *how* to use
the tools. (The MCP server is distributed as the `livespec` package /
`uvx livespec@…` command; the plugin — and everything an agent sees — is
`livespec`.)

## What's inside

```
plugin/
├── .claude-plugin/plugin.json   # plugin manifest
├── .mcp.json                    # runs uvx livespec@<pinned version>
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

### Local checkout (dev / testing unreleased fixes)

Keep the published `plugin/.mcp.json` on `uvx livespec@…`. For a local checkout,
register the editable install in **`~/.cursor/mcp.json`** (server name `livespec`
→ shows as `user-livespec`) so the agent picks up Unreleased code even when the
marketplace plugin still pins PyPI:

```json
{
  "mcpServers": {
    "livespec": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABS/PATH/TO/livespec-mcp",
        "run",
        "livespec"
      ]
    }
  }
}
```

After edits, restart the MCP server (toggle in Cursor Settings → MCP, or kill
the `uv … run livespec` process so the host respawns it). `mcp_auth` alone
may leave a long-lived stdio process on the old code.

### Marketplace / PyPI

The published plugin runs:

```text
uvx livespec@<version>
```

Ensure `uv` is installed. Register the **owner** marketplace and install the
**product** plugin (`plugin@marketplace`):

```text
claude plugin marketplace add Rixmerz/claude-plugins
claude plugin install livespec@rixmerz
claude plugin update livespec
```

(`rixmerz` is the owner-namespace marketplace, published at
[Rixmerz/claude-plugins](https://github.com/Rixmerz/claude-plugins), which
indexes both `livespec` and `vise`. It lives in its own repo rather than in a
plugin's: Claude Code keys marketplaces by **name** across all sources, so two
repos declaring the same name displace each other and the loser's plugins stop
resolving.)

Or point Claude Code at this directory as a local plugin.

> **Version pin (published):** keep `plugin/.mcp.json` and
> `plugin/.claude-plugin/plugin.json` on the same version. Local checkout mode
> above always runs the working tree (including Unreleased nomenclature changes).

`search` is FTS5-only (no model download). See the repo `README.md`,
`docs/AGENT_PLAYBOOK.md`, and `CHANGELOG.md` for the full surface.
