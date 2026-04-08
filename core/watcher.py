"""
ShadowSync - Core File Watcher
Uses FSEvents (via watchdog) with hash-based deduplication and debounce logic.
"""

import hashlib
import os
import time
import threading
import logging
from pathlib import Path
from collections import defaultdict
from typing import Callable, Dict, Set
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

logger = logging.getLogger("shadowsync.watcher")

DEBOUNCE_SECONDS = 0.5
IGNORED_PATTERNS: Set[str] = {
    ".DS_Store", ".git", "__pycache__", ".shadowsync_tmp",
    "*.swp", "*.tmp", "~*"
}


def _is_ignored(path: str) -> bool:
    name = os.path.basename(path)
    for pattern in IGNORED_PATTERNS:
        if pattern.startswith("*"):
            if name.endswith(pattern[1:]):
                return True
        elif name == pattern:
            return True
    return False


def _file_hash(path: str) -> str | None:
    """SHA-256 of file content for change deduplication."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


class ChangeEvent:
    __slots__ = ("event_type", "src_path", "dest_path", "timestamp", "file_hash", "size")

    def __init__(self, event_type: str, src_path: str, dest_path: str = None):
        self.event_type = event_type      # created | modified | deleted | moved
        self.src_path = src_path
        self.dest_path = dest_path
        self.timestamp = time.time()
        self.file_hash = _file_hash(src_path) if event_type != "deleted" else None
        self.size = os.path.getsize(src_path) if event_type != "deleted" and os.path.exists(src_path) else 0

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "src_path": self.src_path,
            "dest_path": self.dest_path,
            "timestamp": self.timestamp,
            "file_hash": self.file_hash,
            "size": self.size,
        }

    def __repr__(self):
        return f"<ChangeEvent {self.event_type} {self.src_path}>"


class _DebouncedHandler(FileSystemEventHandler):
    """
    Watchdog handler with debouncing + hash dedup.
    Only fires callback if the file actually changed (different hash).
    """

    def __init__(self, watch_root: str, callback: Callable[[ChangeEvent], None]):
        super().__init__()
        self.watch_root = watch_root
        self.callback = callback
        self._pending: Dict[str, tuple] = {}   # path -> (timer, event_type, dest)
        self._known_hashes: Dict[str, str] = {}
        self._lock = threading.Lock()

    def _schedule(self, event_type: str, src: str, dest: str = None):
        if _is_ignored(src):
            return
        with self._lock:
            if src in self._pending:
                self._pending[src][0].cancel()
            t = threading.Timer(DEBOUNCE_SECONDS, self._fire, args=(event_type, src, dest))
            self._pending[src] = (t, event_type, dest)
            t.start()

    def _fire(self, event_type: str, src: str, dest: str = None):
        with self._lock:
            self._pending.pop(src, None)

        # Hash dedup for modified events
        if event_type == "modified":
            new_hash = _file_hash(src)
            if new_hash and self._known_hashes.get(src) == new_hash:
                return   # No real change
            self._known_hashes[src] = new_hash
        elif event_type == "deleted":
            self._known_hashes.pop(src, None)
        elif event_type == "created":
            h = _file_hash(src)
            self._known_hashes[src] = h

        ev = ChangeEvent(event_type, src, dest)
        logger.info(f"[WATCHER] {ev}")
        try:
            self.callback(ev)
        except Exception as e:
            logger.error(f"[WATCHER] Callback error: {e}")

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory:
            self._schedule("created", event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self._schedule("modified", event.src_path)

    def on_deleted(self, event: FileSystemEvent):
        if not event.is_directory:
            self._schedule("deleted", event.src_path)

    def on_moved(self, event: FileSystemEvent):
        if not event.is_directory:
            self._schedule("moved", event.src_path, event.dest_path)


class FileWatcher:
    """
    Public API: watch one or more directories.
    Usage:
        watcher = FileWatcher(["/Users/me/Documents"], on_change_callback)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(self, watch_dirs: list[str], on_change: Callable[[ChangeEvent], None]):
        self.watch_dirs = [str(Path(d).resolve()) for d in watch_dirs]
        self.on_change = on_change
        self._observer = Observer()
        self._handlers: list[_DebouncedHandler] = []

    def start(self):
        for d in self.watch_dirs:
            if not os.path.isdir(d):
                logger.warning(f"[WATCHER] Directory not found, skipping: {d}")
                continue
            handler = _DebouncedHandler(d, self.on_change)
            self._handlers.append(handler)
            self._observer.schedule(handler, d, recursive=True)
            logger.info(f"[WATCHER] Watching: {d}")
        self._observer.start()

    def stop(self):
        self._observer.stop()
        self._observer.join()
        logger.info("[WATCHER] Stopped.")

    def add_directory(self, path: str):
        path = str(Path(path).resolve())
        handler = _DebouncedHandler(path, self.on_change)
        self._handlers.append(handler)
        self._observer.schedule(handler, path, recursive=True)
        logger.info(f"[WATCHER] Added watch: {path}")
