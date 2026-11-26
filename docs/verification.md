# Spec verification and staleness

A spec links to the code that implements it. An edit to that code may break the spec, so livespec
records, per requirement and implementing symbol, what checked it and on which code (the symbol's
body and signature hashes), and says when an edit since made it stale.

```bash
livespec index .
livespec verify . --spec RF-12 --by test --evidence "tests/test_totals.py::test_half_up"
livespec stale .                          # exit 1 while a verified requirement's code changed
livespec stale . --strict                 # a requirement never verified is stale too
livespec stale . --require-test critical  # a critical one counts only when a test verified it
```

The record is `openspec/verifications.json` (else `.livespec/verifications.json`), committed with
the code, so CI can hold a merge to it:

```yaml
name: specs
on: [pull_request]
jobs:
  stale:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uvx livespec index . && uvx livespec stale . --require-test critical
```

`by` is `test` (a test that passed), `reviewer` (a person) or `verifier` (an agent's independent
check). An agent's word is weaker than a test's; `--require-test` makes a priority demand the latter.
