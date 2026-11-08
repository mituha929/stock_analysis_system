"""Bounded, credential-free diagnostics for the SDK's shared event callback."""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import logging
import threading
import time

logger = logging.getLogger(__name__)


def connection_source(login_id):
    # The SDK uses the literal SKQuote for domestic quotes; other values can
    # contain a real login ID. Never retain those values in logs or health JSON.
    if isinstance(login_id, bytes):
        login_id = login_id.decode('ascii', errors='replace')
    if isinstance(login_id, str) and login_id.strip().casefold() == 'skquote':
        return 'domestic_quote', 'SKQuote'
    return 'other', '[REDACTED]' if login_id else 'unknown'


class ConnectionDiagnostics:
    def __init__(self):
        # Independent of the DLL call lock: callbacks can run on another native
        # thread while Login/ManageServerConnection is still executing.
        self.lock = threading.RLock()
        self._events = deque(maxlen=50)
        self.attempt = 0
        self.started_at = None
        self.started_monotonic = None
        self.phase = 'idle'
        self.last_quote_code = None
        self.event_count = 0

    def begin(self):
        with self.lock:
            self.attempt += 1
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.started_monotonic = time.monotonic()
            self.phase = 'starting'
            self.last_quote_code = None
            self.event_count = 0
            self._events.clear()
            self.record('attempt_start')

    def record(self, kind, **fields):
        # Callers pass only fixed operation/channel labels and numeric codes,
        # never native arguments, login IDs, passwords or announcement bodies.
        with self.lock:
            entry = dict(at=datetime.now(timezone.utc).isoformat(), kind=kind,
                         phase=self.phase, **fields)
            if self.started_monotonic is not None:
                entry['elapsed_ms'] = round((time.monotonic()-self.started_monotonic)*1000, 1)
            self._events.append(entry)
            self.event_count += 1
        logger.debug('群益連線診斷：%s', entry)

    def snapshot(self):
        with self.lock:
            return dict(version=1, attempt=self.attempt, started_at=self.started_at,
                        phase=self.phase, last_quote_code=self.last_quote_code,
                        event_count=self.event_count, events=deepcopy(list(self._events)))
