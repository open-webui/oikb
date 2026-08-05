"""File watcher — debounced fswatch integration for live sync.

Supports both native filesystem events (FSEvents / inotify) and
PollingObserver for network-mounted volumes (SMB / CIFS / NFS).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver


class _DebouncedHandler(FileSystemEventHandler):
    """Collects filesystem events and fires a callback after a quiet period."""

    def __init__(
        self,
        callback: Callable[[], None],
        debounce_seconds: float = 1.0,
        ignore: frozenset[str] | None = None,
    ):
        super().__init__()
        self._callback = callback
        self._debounce = debounce_seconds
        self._ignore = ignore or frozenset()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _should_ignore(self, path: str) -> bool:
        """Skip events for ignored files/directories."""
        parts = Path(path).parts
        return any(part in self._ignore or part.startswith(".") for part in parts)

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return

        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._callback)
            self._timer.daemon = True
            self._timer.start()


def watch_directory(
    directory: str | Path,
    on_change: Callable[[], None],
    debounce_seconds: float = 1.0,
    ignore: frozenset[str] | None = None,
    polling: bool = False,
    polling_interval: float = 5.0,
) -> None:
    """Watch a directory for changes and call on_change after debounce.

    Blocks until interrupted (Ctrl+C).

    Args:
        directory:        Path to watch.
        on_change:        Callback fired after changes settle.
        debounce_seconds: Quiet period before triggering sync.
        ignore:           File/dir names to ignore.
        polling:          Use PollingObserver instead of native events.
                          Required for SMB/CIFS/NFS network mounts where
                          kernel-level filesystem events are not available.
        polling_interval: How often (seconds) the PollingObserver checks
                          for changes. Only used when polling=True.
                          Lower values detect changes faster but increase
                          I/O load on the network share. Default: 5.0.
    """
    path = Path(directory).resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"Not a directory: {path}")

    handler = _DebouncedHandler(
        callback=on_change,
        debounce_seconds=debounce_seconds,
        ignore=ignore,
    )

    if polling:
        observer = PollingObserver(timeout=polling_interval)
    else:
        observer = Observer()
    observer.schedule(handler, str(path), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
