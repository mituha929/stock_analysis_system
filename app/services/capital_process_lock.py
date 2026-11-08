"""One shared, re-entrant lock for Capital's native DLL per Windows machine.

``threading.Lock`` only coordinates threads inside one Python process.  This
module additionally locks a small file, so the API server, K-line backfill
script, and diagnostic scripts cannot use SKCOM concurrently.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class CapitalProcessLockError(RuntimeError):
    pass


class CapitalProcessLock:
    """A process-wide re-entrant exclusive file lock.

    Re-entrancy is deliberately per Python process rather than per thread:
    SKCOM callbacks and history queries can run on different threads while a
    logged-in API session already owns the machine-level lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._state_lock = threading.Lock()
        self._acquire_gate = threading.Lock()
        self._handle: object | None = None
        self._depth = 0

    def acquire(self, timeout_seconds: float = 60.0) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds 不可小於 0")
        with self._state_lock:
            if self._depth:
                self._depth += 1
                return

        # Only one local thread attempts the OS lock at a time.  Once it has
        # succeeded, following local callers become re-entrant owners.
        with self._acquire_gate:
            with self._state_lock:
                if self._depth:
                    self._depth += 1
                    return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")
            # Only initialize a newly created file. Writing an already locked
            # byte would itself fail on Windows before we get a chance to wait.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + timeout_seconds
            while True:
                if self._try_lock(handle):
                    with self._state_lock:
                        self._handle = handle
                        self._depth = 1
                    return
                if time.monotonic() >= deadline:
                    handle.close()
                    raise CapitalProcessLockError(
                        f"群益 DLL 正由另一個程式使用，等待 {timeout_seconds:g} 秒後仍未取得鎖：{self.path}"
                    )
                time.sleep(0.1)

    def release(self) -> None:
        with self._state_lock:
            if not self._depth:
                raise RuntimeError("群益 DLL 鎖尚未取得，無法釋放")
            self._depth -= 1
            if self._depth:
                return
            handle = self._handle
            self._handle = None
        assert handle is not None
        try:
            self._unlock(handle)
        finally:
            handle.close()

    @contextmanager
    def hold(self, timeout_seconds: float = 60.0) -> Iterator[None]:
        self.acquire(timeout_seconds)
        try:
            yield
        finally:
            self.release()

    @staticmethod
    def _try_lock(handle: object) -> bool:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)  # type: ignore[union-attr]
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[union-attr]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
            return True
        except OSError:
            return False

    @staticmethod
    def _unlock(handle: object) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)  # type: ignore[union-attr]
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]


_shared_lock: CapitalProcessLock | None = None
_shared_lock_guard = threading.Lock()


def get_capital_process_lock() -> CapitalProcessLock:
    """Return the one lock shared by all Capital callers in this process."""
    global _shared_lock
    with _shared_lock_guard:
        if _shared_lock is None:
            configured = os.getenv("CAPITAL_DLL_LOCK_FILE")
            default = Path(__file__).resolve().parents[2] / "data" / "capital_skcom.lock"
            _shared_lock = CapitalProcessLock(Path(configured).expanduser() if configured else default)
        return _shared_lock
