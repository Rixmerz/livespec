"""File-system watcher: keep the index live without manual `index_project` calls.

P2.3: this is what makes "living documentation" actually live. A `Watcher`
listens to filesystem changes under a workspace path, debounces them (so a
multi-file save doesn't trigger N reindexes), and runs `index_project` in a
background thread.

The watcher is per-workspace; it lives inside the per-workspace AppState. We
hold one global registry so `start_watcher` / `stop_watcher` tools can be
called repeatedly without leaking observers.
"""

from __future__ import annotations

import atexit
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from livespec_mcp.domain.indexer import DEFAULT_IGNORES
from livespec_mcp.domain.languages import detect_language


@dataclass
class WatcherStats:
    started_at: float = field(default_factory=time.time)
    events_received: int = 0
    reindex_runs: int = 0
    last_reindex_at: float | None = None
    last_run_files_changed: int = 0


def _is_relevant(path: Path) -> bool:
    if any(part in DEFAULT_IGNORES or part.startswith(".") for part in path.parts):
        return False
    return detect_language(path) is not None


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "Watcher") -> None:
        self.watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        try:
            p = Path(event.src_path)
        except Exception:
            return
        if not _is_relevant(p):
            return
        self.watcher.notify(p)


class Watcher:
    """One filesystem observer + a debounced reindex worker."""

    def __init__(
        self,
        workspace: Path,
        on_reindex: Callable[[], None],
        debounce_seconds: float = 2.0,
    ) -> None:
        self.workspace = workspace
        self._on_reindex = on_reindex
        self._debounce = debounce_seconds
        self._observer = Observer()
        self._handler = _Handler(self)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        self.stats = WatcherStats()
        self._lock = threading.Lock()

    def start(self) -> None:
        self._observer.schedule(self._handler, str(self.workspace), recursive=True)
        self._observer.start()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        try:
            self._observer.stop()
            self._observer.join(timeout=2.0)
        except Exception:
            pass
        if self._worker is not None:
            self._worker.join(timeout=3.0)

    def notify(self, path: Path) -> None:
        with self._lock:
            self.stats.events_received += 1
        self._wake_event.set()

    def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait()
            if self._stop_event.is_set():
                return
            # Coalesce: keep waiting while events keep arriving within debounce window
            while True:
                self._wake_event.clear()
                if self._stop_event.wait(self._debounce):
                    return
                if not self._wake_event.is_set():
                    break  # quiet for full debounce window — go reindex
            try:
                self._on_reindex()
                with self._lock:
                    self.stats.reindex_runs += 1
                    self.stats.last_reindex_at = time.time()
            except Exception:
                pass


# ---------- Global per-workspace registry ----------

_registry: dict[Path, Watcher] = {}
_registry_lock = threading.Lock()


def get_watcher(workspace: Path) -> Watcher | None:
    with _registry_lock:
        return _registry.get(workspace)


def register_watcher(workspace: Path, watcher: Watcher) -> None:
    with _registry_lock:
        existing = _registry.get(workspace)
        if existing is not None:
            existing.stop()
        _registry[workspace] = watcher


def unregister_watcher(workspace: Path) -> bool:
    with _registry_lock:
        watcher = _registry.pop(workspace, None)
    if watcher is None:
        return False
    watcher.stop()
    return True


def all_watchers() -> dict[Path, Watcher]:
    with _registry_lock:
        return dict(_registry)


def stop_all_watchers() -> int:
    """Stop and unregister every active watcher. Idempotent. Returns the
    number of watchers stopped.

    Registered with atexit so a server shutdown flushes WAL files cleanly
    instead of leaving observer threads racing with interpreter teardown."""
    with _registry_lock:
        watchers = list(_registry.items())
        _registry.clear()
    stopped = 0
    for _ws, w in watchers:
        try:
            w.stop()
            stopped += 1
        except Exception:
            pass
    return stopped


atexit.register(stop_all_watchers)
