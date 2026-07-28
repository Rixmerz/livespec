# livespec — Spec↔symbol link seed

The `implements` / `tests` links between each self-Spec and the symbols
that satisfy it are committed as a deterministic data seed:

- **`livespec-spec-links.json`** — sorted list of
  `{"spec_id": "SPEC-NNN", "qname": "<symbol qname>", "relation": "implements" | "tests"}`.

## Reproduce the links

From a fresh clone, after `index_project` + `sync_openspec` (or
`import_specs_from_markdown` on `openspec/` — see the regeneration flow in
[`livespec-specs.md`](./livespec-specs.md)):

```
python scripts/apply_spec_links.py --workspace /abs/path/to/livespec-mcp
```

The script reads `livespec-spec-links.json` and replays every link through
`bulk_link_spec_symbols` in a single transaction. It is idempotent
(`INSERT OR IGNORE`), so re-running it only fills in missing links.

## Re-export the seed (when links change)

After adding/removing links in the live `.mcp-docs/docs.db`, regenerate
the seed so it stays the source of truth:

```python
import sqlite3, json
c = sqlite3.connect(".mcp-docs/docs.db")
rows = c.execute(
    """SELECT spec.spec_id, s.qualified_name, rs.relation
       FROM spec_symbol rs
       JOIN spec ON spec.id = rs.spec_id
       JOIN symbol s ON s.id = rs.symbol_id
       WHERE spec.project_id = 1"""
).fetchall()
data = sorted(
    ({"spec_id": r[0], "qname": r[1], "relation": r[2]} for r in rows),
    key=lambda d: (d["spec_id"], d["relation"], d["qname"]),
)
with open("docs/requirements/livespec-spec-links.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
```

Sorted output keeps the diff small and review-friendly.
