"""Bounded recovery for an existing Capital quote login (no new Login calls)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, TypeVar


logger = logging.getLogger(__name__)
HISTORY_RECOVERY_ATTEMPTS = 2
HISTORY_RECOVERY_BACKOFF_SECONDS = 2.0
T = TypeVar("T")


class CapitalHistoryError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CapitalHistoryConnectionError(CapitalHistoryError):
    """A connection problem, not a stock with missing/invalid K-line data."""


def is_history_connection_error(error: BaseException) -> bool:
    """Preserve classification through service exception wrappers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, CapitalHistoryConnectionError):
            return True
        if isinstance(current, CapitalHistoryError) and current.code == 1095:
            return True
        current = current.__cause__
    return False


def assert_history_code(code: int, center: Any, *, allow_login_warning: bool = False) -> None:
    value = int(code)
    if value == 0 or (allow_login_warning and value == 2003):
        # 2003 means already logged in, and is valid ONLY for Login.
        return
    try:
        message = str(center.SKCenterLib_GetReturnCodeMessage(value))
    except Exception:
        message = ""
    error_type = CapitalHistoryConnectionError if value == 1095 else CapitalHistoryError
    raise error_type(
        f"群益歷史 K 線 API 失敗：{value}{f'（{message}）' if message else ''}", code=value,
    )


def require_history_connection(connected: threading.Event) -> None:
    if not connected.is_set():
        raise CapitalHistoryConnectionError("歷史 K 線報價主機尚未就緒或已斷線")


def enter_history_monitor(
    quote: Any, center: Any, connected: threading.Event,
    pump: Callable[[], Any], timeout_seconds: float,
) -> None:
    connected.clear()
    assert_history_code(quote.SKQuoteLib_EnterMonitorLONG(), center)
    deadline = time.monotonic() + timeout_seconds
    while not connected.is_set() and time.monotonic() < deadline:
        pump()
        time.sleep(0.05)
    if not connected.is_set():
        raise CapitalHistoryConnectionError("歷史 K 線報價主機未完成連線（等待 OnConnection 3003 逾時）")


def reconnect_history_monitor(
    quote: Any, center: Any, connected: threading.Event,
    pump: Callable[[], Any], timeout_seconds: float,
) -> None:
    """Run on the owning COM thread; retain the existing authenticated session."""
    connected.clear()
    try:
        # An already-disconnected monitor can report an error on LeaveMonitor.
        quote.SKQuoteLib_LeaveMonitor()
    except Exception:
        pass
    try:
        pump()  # Consume old disconnect callbacks before entering the new monitor.
        enter_history_monitor(quote, center, connected, pump, timeout_seconds)
    except CapitalHistoryConnectionError:
        raise
    except Exception as error:
        raise CapitalHistoryConnectionError(f"歷史 K 線報價主機恢復連線失敗：{error}") from error


def wait_for_history_completion(
    connected: threading.Event, completed: threading.Event,
    pump: Callable[[], Any], timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not completed.is_set() and time.monotonic() < deadline:
        pump()
        require_history_connection(connected)
        time.sleep(0.05)
    require_history_connection(connected)
    if not completed.is_set():
        # A silent/stalled history connection should not fail every later job.
        raise CapitalHistoryConnectionError("群益歷史 K 線回補逾時（未收到 OnKLineComplete）")


def with_history_connection_recovery(
    operation: Callable[[], T], reconnect: Callable[[], None],
    *, attempts: int = HISTORY_RECOVERY_ATTEMPTS,
    backoff_seconds: float = HISTORY_RECOVERY_BACKOFF_SECONDS,
) -> T:
    """Retry the SAME operation, at most twice, then let the worker pause."""
    try:
        return operation()
    except CapitalHistoryConnectionError as error:
        last_error = error
    for attempt in range(1, attempts + 1):
        logger.warning("歷史 K 線連線異常，嘗試恢復 %s/%s：%s", attempt, attempts, last_error)
        time.sleep(backoff_seconds)
        try:
            reconnect()
            result = operation()
        except CapitalHistoryConnectionError as error:
            last_error = error
            continue
        logger.info("歷史 K 線連線已恢復，原查詢重試成功")
        return result
    raise CapitalHistoryConnectionError(
        f"歷史 K 線連線恢復 {attempts} 次仍失敗，暫停回補：{last_error}", code=last_error.code,
    ) from last_error
