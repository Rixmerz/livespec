---
description: Start the livespec UI — one local, read-only web page over every indexed project
argument-hint: "[--port N] [--open]"
allowed-tools: Bash
---

Start `livespec ui` in the background and give me its URL.

It is one local web server (bound to 127.0.0.1) over **every project indexed
with livespec on this machine**, grouped by polyrepo group. It reads the
user-level registry and each project's index read-only — it writes nothing into
any repository — so do **not** run it from, or point it at, a project folder;
it needs no path.

Run this with the Bash tool (pass `$ARGUMENTS` through; default is a random
free port):

```bash
STATE="${TMPDIR:-/tmp}/livespec-ui-$(id -u)"
mkdir -p "$STATE"
if [ -f "$STATE/pid" ] && kill -0 "$(cat "$STATE/pid")" 2>/dev/null; then
  echo "already running (pid $(cat "$STATE/pid"))"; head -n 1 "$STATE/log"; exit 0
fi
if command -v livespec >/dev/null 2>&1 && livespec ui --help >/dev/null 2>&1; then
  set -- livespec ui
else
  set -- uvx livespec@0.33.0 ui
fi
nohup "$@" $ARGUMENTS >"$STATE/log" 2>&1 &
echo $! >"$STATE/pid"
for _ in $(seq 1 60); do
  grep -q '^livespec ui: http' "$STATE/log" && break
  kill -0 "$(cat "$STATE/pid")" 2>/dev/null || break
  sleep 0.5
done
grep '^livespec ui: http' "$STATE/log" || { echo "failed to start:"; cat "$STATE/log"; exit 1; }
```

Then report, briefly:

- the URL from the `livespec ui: http://…` line (the page lists every indexed
  project; a group page shows cross-repo Specs and route hops);
- how to stop it: `kill "$(cat "${TMPDIR:-/tmp}/livespec-ui-$(id -u)/pid")"`;
- if the page shows no projects: index a repo first (`index_project` or
  `livespec index /abs/repo`), or register an index made before the registry
  existed with `livespec ui --add /abs/repo`.

If it failed because the installed or pinned `livespec` has no `ui`
subcommand, say that this livespec release predates the UI and suggest
upgrading (`uvx livespec@latest ui`), rather than generating any HTML yourself.
