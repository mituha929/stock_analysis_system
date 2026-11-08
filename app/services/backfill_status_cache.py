"""Small single-flight cache for expensive read-only dashboard snapshots."""
from collections import OrderedDict
from copy import deepcopy
import threading
import time
import queue


class BackfillStatusCache:
    def __init__(self, ttl_seconds=5, max_entries=12):
        self.ttl_seconds, self.max_entries = ttl_seconds, max_entries
        self.entries = OrderedDict()
        self.loading = set()
        self.condition = threading.Condition()
        self.errors = OrderedDict()
        self.pending = queue.Queue(maxsize=max_entries)
        self.worker = None

    def peek(self, key, loader, force=False):
        """Return immediately; one background thread refreshes DB snapshots."""
        with self.condition:
            now = time.monotonic()
            entry = self.entries.get(key)
            failure = self.errors.get(key)
            fresh = entry and now-entry[0] < self.ttl_seconds
            retry = not failure or now-failure[0] >= 15
            if (force or not fresh) and retry and key not in self.loading and len(self.loading)<self.max_entries:
                self.loading.add(key)
                self.pending.put_nowait((key,loader))
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(target=self._refresh,name="backfill-status-reader",daemon=True)
                    self.worker.start()
            return (deepcopy(entry[1]) if entry else None,
                    round(now-entry[0],3) if entry else 0.0,
                    key in self.loading, str(failure[1]) if failure else None)

    def _refresh(self):
        while True:
            try:
                key,loader = self.pending.get(timeout=30)
            except queue.Empty:
                with self.condition:
                    if self.pending.empty():
                        self.worker = None
                        return
                continue
            try:
                value = loader()
                with self.condition:
                    self.entries[key] = (time.monotonic(),deepcopy(value))
                    self.entries.move_to_end(key)
                    self.errors.pop(key,None)
                    while len(self.entries)>self.max_entries: self.entries.popitem(last=False)
            except Exception as error:
                with self.condition:
                    # Keep only text, not a traceback retaining query buffers.
                    self.errors[key] = (time.monotonic(),str(error))
                    self.errors.move_to_end(key)
                    while len(self.errors)>self.max_entries: self.errors.popitem(last=False)
            finally:
                with self.condition:
                    self.loading.remove(key)
                    self.condition.notify_all()
                self.pending.task_done()

    def get(self, key, loader):
        with self.condition:
            while key in self.loading:
                self.condition.wait()
            now = time.monotonic()
            entry = self.entries.get(key)
            if entry and now-entry[0] < self.ttl_seconds:
                self.entries.move_to_end(key)
                return deepcopy(entry[1]), round(now-entry[0], 3), True
            self.loading.add(key)
        try:
            value = loader()
            with self.condition:
                self.entries[key] = (time.monotonic(), deepcopy(value))
                self.entries.move_to_end(key)
                while len(self.entries) > self.max_entries:
                    self.entries.popitem(last=False)
            return value, 0.0, False
        finally:
            with self.condition:
                self.loading.remove(key)
                self.condition.notify_all()
