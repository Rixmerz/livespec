# livespec-mcp — RF↔symbol link seed

The `implements` / `tests` links between each self-RF and the symbols
that satisfy it are committed as a deterministic data seed:

- **`livespec-rf-links.json`** — sorted list of
  `{"rf_id": "RF-NNN", "qname": "<symbol qname>", "relation": "implements" | "tests"}`.

## Reproduce the links

From a fresh clone, after `index_project` + `import_requirements_from_markdown`
(see the regeneration flow in [`livespec-rfs.md`](./livespec-rfs.md)):

```
python scripts/apply_rf_links.py --workspace /abs/path/to/livespec-mcp
```

The script reads `livespec-rf-links.json` and replays every link through
`bulk_link_rf_symbols` in a single transaction. It is idempotent
(`INSERT OR IGNORE`), so re-running it only fills in missing links.

## Re-export the seed (when links change)

After adding/removing links in the live `.mcp-docs/docs.db`, regenerate
the seed so it stays the source of truth:

```python
import sqlite3, json
c = sqlite3.connect(".mcp-docs/docs.db")
rows = c.execute(
    """SELECT rf.rf_id, s.qualified_name, rs.relation
       FROM rf_symbol rs
       JOIN rf ON rf.id = rs.rf_id
       JOIN symbol s ON s.id = rs.symbol_id
       WHERE rf.project_id = 1"""
).fetchall()
data = sorted(
    ({"rf_id": r[0], "qname": r[1], "relation": r[2]} for r in rows),
    key=lambda d: (d["rf_id"], d["relation"], d["qname"]),
)
with open("docs/requirements/livespec-rf-links.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
```

Sorted output keeps the diff small and review-friendly.
