Real FlowTrace output, not a hand-written approximation of it.

`flowtrace.jsonl` was captured by running `main.py` under FlowTrace's Python
layer with `FLOWTRACE_PACKAGE_PREFIX=shop`; only `ts` and `duration_ns` were
flattened so the file does not churn. `tests/test_trace_ingest_real_capture.py`
ingests it. If FlowTrace changes how it writes `file` / `line`, regenerate this
file and that test is where the change shows.
