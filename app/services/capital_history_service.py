"""Historical K-line client backed by Capital's COM quote API."""

from __future__ import annotations

import gc
import os
import importlib
import logging
import queue
import struct
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.schemas.models import Candle
from app.services.candle_validation import CandleDataError, parse_volume
from app.services.history_exclusions import allowed_history_ranges, excluded_label, excluded_periods, load_history_exclusions
from app.services.backfill_policy import history_request_limiter
from app.services.backfill_eligibility import excluded_backfill_symbols
from app.services.capital_dll_environment import ensure_dll_directory_on_path
from app.services.capital_history_connection import (
    HISTORY_RECOVERY_ATTEMPTS, HISTORY_RECOVERY_BACKOFF_SECONDS,
    CapitalHistoryConnectionError, CapitalHistoryError, assert_history_code,
    enter_history_monitor, reconnect_history_monitor, require_history_connection,
    wait_for_history_completion, with_history_connection_recovery,
)
from app.services.capital_process_lock import CapitalProcessLockError, get_capital_process_lock


INTERVAL_OPTIONS: dict[str, tuple[int, int, int]] = {
    "1m": (0, 1, 7), "5m": (0, 5, 7), "15m": (0, 15, 7),
    "60m": (0, 60, 7),
    "day": (4, 0, 400), "week": (5, 0, 400), "month": (6, 0, 400),
}
TAIPEI = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


def max_history_days(interval: str) -> int:
    """Configured inclusive calendar-day limit, shared by every caller.

    Resolve at call time: main.py and the CLI load dotenv after importing some
    modules. These are application window sizes, not documented vendor caps.
    Aggregate windows must be large enough for one complete calendar period.
    """
    try:
        default = INTERVAL_OPTIONS[interval][2]
    except KeyError as error:
        raise CapitalHistoryError(f"不支援的 K 線週期：{interval}") from error
    key = f"CAPITAL_KLINE_MAX_DAYS_{interval.upper()}"
    raw = os.getenv(key, "").strip()
    minimum = {"week": 7, "month": 31}.get(interval, 1)
    try:
        value = int(raw) if raw else default
    except ValueError as error:
        raise CapitalHistoryError(f"{key} 必須是至少 {minimum} 天的整數（日曆天）") from error
    if value < minimum:
        raise CapitalHistoryError(f"{key} 必須是至少 {minimum} 天的整數（日曆天）")
    return value


def configured_history_limits() -> dict[str, int]:
    """Validate all intervals before starting a server or preparing CLI jobs."""
    load_history_exclusions()
    excluded_backfill_symbols()
    return {interval: max_history_days(interval) for interval in INTERVAL_OPTIONS}


def validate_history_range(interval: str, start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise CapitalHistoryError("start_date 不可晚於 end_date")
    requested_days = (end_date - start_date).days + 1
    maximum = max_history_days(interval)
    if requested_days > maximum:
        raise CapitalHistoryError(f"{interval} 單次最多可查詢 {maximum} 天，目前要求 {requested_days} 天")


def parse_kline_record(raw: str) -> Candle:
    """Parse the documented new-format ``date,open,high,low,close,volume`` row."""
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 6:
        raise ValueError(f"K 線欄位數錯誤：{raw!r}")
    timestamp = _parse_timestamp(parts[0])
    try:
        opening, high, low, close = (float(part.replace(",", "")) for part in parts[1:5])
    except ValueError as error:
        raise ValueError(f"K 線數值格式錯誤：{raw!r}") from error
    volume = parse_volume(parts[5], timestamp, raw)
    return Candle(timestamp=timestamp, open=opening, high=high, low=low, close=close, volume=volume)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(normalized, pattern).replace(tzinfo=TAIPEI)
        except ValueError:
            continue
    raise ValueError(f"無法解析 K 線日期時間：{value!r}")


class CapitalHistoryClient:
    """Reusable one-request-at-a-time COM client for ``RequestKLineAMByDate``."""

    def __init__(self, dll_path: str | None = None, timeout_seconds: int = 30) -> None:
        architecture = "x64" if struct.calcsize("P") * 8 == 64 else "x86"
        default_dll = (
            Path(__file__).resolve().parents[3]
            / "CapitalAPI_2.13.58_PythonExample"
            / "元件"
            / architecture
            / "SKCOM.dll"
        )
        configured = dll_path or os.getenv("CAPITAL_SKCOM_DLL")
        self.dll_path = Path(configured).expanduser() if configured else default_dll
        self.timeout_seconds = timeout_seconds
        self._lock = threading.RLock()
        self._process_lock = get_capital_process_lock()
        self._session: _ReusableHistoryComSession | None = None

    def fetch(
        self, user_id: str, password: str, authority: int, symbol: str,
        interval: str, start_date: date, end_date: date,
    ) -> list[Candle]:
        validate_history_range(interval, start_date, end_date)
        credentials = (user_id, password, authority)
        with self._lock:
            session = self._session
            if session is None or not session.is_alive or session.credentials != credentials:
                if session is not None:
                    session.close()
                session = _ReusableHistoryComSession(
                    self.dll_path, credentials, self.timeout_seconds, self._process_lock,
                )
                session.start()
                self._session = session
            return session.request(symbol.upper(), interval, start_date, end_date)

    def close(self) -> None:
        """End the reusable COM session, normally when the API disconnects."""
        with self._lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()

    def _fetch_on_com_thread(
        self, user_id: str, password: str, authority: int, symbol: str,
        interval: str, start_date: date, end_date: date,
    ) -> list[Candle]:
        try:
            import comtypes
            import comtypes.client
            import pythoncom
        except ImportError as error:
            raise CapitalHistoryError("歷史 K 線需要 Windows comtypes 與 pywin32 套件") from error
        if not self.dll_path.is_file():
            raise CapitalHistoryError(f"找不到 SKCOM.dll：{self.dll_path}")

        completed = threading.Event()
        ready = threading.Event()
        announcement_received = threading.Event()
        records: list[Candle] = []
        callback_error: list[Exception] = []
        connection_events: list[tuple[int, int]] = []
        directory_handle: Any | None = None
        center: Any | None = None
        quote: Any | None = None
        reply: Any | None = None
        # This must match the verified console script.  SKCOM dispatches its
        # COM callbacks through pywin32's Windows message pump; PumpEvents()
        # alone can leave OnConnection / OnKLineComplete undelivered.
        pythoncom.CoInitialize()
        try:
            if hasattr(os, "add_dll_directory"):
                directory_handle = os.add_dll_directory(str(self.dll_path.parent))
            ensure_dll_directory_on_path(self.dll_path.parent)
            # Keep the generated module available and import it explicitly.
            # Some installations can invoke methods with an ephemeral module
            # but then never deliver COM events to the event sink.
            comtypes.client.GetModule(str(self.dll_path))
            sk = importlib.import_module("comtypes.gen.SKCOMLib")
            center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
            quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
            reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

            class ReplyEvent:
                def OnReplyMessage(self, _user_id: str, _message: str) -> int:
                    # Capital requires -1 from SKReplyLib_OnReplyMessage
                    # after registration and before Login.
                    announcement_received.set()
                    return -1

            class QuoteEvent:
                def OnConnection(self, kind: int, code: int) -> None:
                    connection_events.append((int(kind), int(code)))
                    if int(kind) == 3003:
                        ready.set()

                def OnNotifyKLineData(self, _stock_no: str, data: str) -> None:
                    try:
                        if _excluded_record(symbol, interval, str(data)):
                            return
                        records.append(parse_kline_record(str(data)))
                    except (ValueError, CandleDataError) as error:
                        callback_error.append(error)

                def OnKLineComplete(self, end_string: str) -> None:
                    if str(end_string).startswith("##"):
                        completed.set()

            # COM connection points only keep weak references to Python event
            # sinks.  Keep both sink instances alive through Login and the
            # entire K-line request; otherwise SKCenterLib_Login returns 2017
            # (ReplyLib's OnReplyMessage was not registered first).
            reply_event = ReplyEvent()
            quote_event = QuoteEvent()
            # Match the vendor's Python example: let comtypes select each
            # object's default event source.  Explicitly forcing an event
            # interface can leave this secondary COM client without
            # OnConnection callbacks on some SKCOM installations.
            reply_handler = comtypes.client.GetEvents(reply, reply_event)
            quote_handler = comtypes.client.GetEvents(quote, quote_event)
            self._assert_code(center.SKCenterLib_SetAuthority(authority), center)
            assert_history_code(center.SKCenterLib_Login(user_id, password), center, allow_login_warning=True)
            self._assert_code(quote.SKQuoteLib_EnterMonitorLONG(), center)
            ready_deadline = time.monotonic() + self.timeout_seconds
            while not ready.is_set() and time.monotonic() < ready_deadline:
                pythoncom.PumpWaitingMessages()
                time.sleep(0.05)
            if not ready.is_set():
                try:
                    connection_state = int(quote.SKQuoteLib_IsConnected())
                    connection_count, out_limit, status_code = quote.SKQuoteLib_GetQuoteStatus(0, False)
                    diagnostic = (
                        f"IsConnected={connection_state}、"
                        f"GetQuoteStatus=({connection_count}, {out_limit}, {status_code})"
                    )
                except Exception as error:
                    diagnostic = f"無法讀取報價連線診斷：{error}"
                raise CapitalHistoryError(
                    "歷史 K 線報價主機未完成連線（等待 OnConnection 3003 逾時；"
                    f"收到事件：{connection_events or '無'}；{diagnostic}）"
                )
            kline_type, minute_number, _maximum = INTERVAL_OPTIONS[interval]
            self._assert_code(
                quote.SKQuoteLib_RequestKLineAMByDate(
                    symbol, kline_type, 1, 0, start_date.strftime("%Y%m%d"),
                    end_date.strftime("%Y%m%d"), minute_number,
                ),
                center,
            )
            deadline = time.monotonic() + self.timeout_seconds
            while not completed.is_set() and time.monotonic() < deadline:
                pythoncom.PumpWaitingMessages()
                time.sleep(0.05)
            if not completed.is_set():
                raise CapitalHistoryError("群益歷史 K 線回補逾時")
            if callback_error:
                raise callback_error[0]
            return _deduplicate(records)
        finally:
            if quote is not None:
                try:
                    quote.SKQuoteLib_LeaveMonitor()
                except Exception:
                    pass
            try:
                del reply_handler, quote_handler, reply_event, quote_event
            except UnboundLocalError:
                pass
            # COM wrappers own native references outside Python's managed
            # heap.  Drop them and collect while this COM apartment is still
            # initialized, rather than waiting for a later GC cycle.
            del reply, quote, center
            gc.collect()
            if directory_handle is not None:
                directory_handle.close()
            pythoncom.CoUninitialize()

    @staticmethod
    def _assert_code(code: int, center: Any) -> None:
        assert_history_code(code, center)


class HistoryQuoteEvents:
    """Quote readiness is cleared on disconnect, including during a request."""

    def __init__(self) -> None:
        self.connected = threading.Event()
        self.completed = threading.Event()
        self.records: list[Candle] = []
        self.callback_error: Exception | None = None
        self.connection_events: list[tuple[int, int]] = []
        self.accepting_records = False
        self.request_symbol = ""
        self.request_interval = ""

    def reset(self, symbol: str = "", interval: str = "") -> None:
        self.completed.clear()
        self.records = []
        self.callback_error = None
        self.accepting_records = True
        self.request_symbol, self.request_interval = symbol, interval

    def OnConnection(self, kind: int, code: int) -> None:
        kind, code = int(kind), int(code)
        self.connection_events.append((kind, code))
        self.connection_events = self.connection_events[-10:]
        if kind == 3003 and code == 0:
            self.connected.set()
        elif kind in (3001, 3002, 3033) or code != 0:
            self.connected.clear()

    def OnNotifyKLineData(self, _stock_no: str, data: str) -> None:
        if not self.accepting_records:
            return
        try:
            if _excluded_record(self.request_symbol, self.request_interval, str(data)):
                return
            self.records.append(parse_kline_record(str(data)))
        except (ValueError, CandleDataError) as error:
            if self.callback_error is None:
                self.callback_error = error

    def OnKLineComplete(self, end_string: str) -> None:
        if self.accepting_records and str(end_string).startswith("##"):
            self.completed.set()


def request_history_candles(
    quote: Any, center: Any, events: HistoryQuoteEvents, pump: Any,
    symbol: str, interval: str, start_date: date, end_date: date,
    timeout_seconds: int, connect_timeout_seconds: int | None = None,
) -> list[Candle]:
    """Used by both the API's STA thread and the standalone backfill script."""
    ranges = allowed_history_ranges(symbol, interval, start_date, end_date)
    for rule in excluded_periods(symbol, interval, start_date, end_date):
        logger.warning("歷史 K 線明確排除 %s %s %s～%s [%s]：%s",
                       symbol, interval, rule.start, rule.end, rule.id, rule.reason)

    def request_once(first, last) -> list[Candle]:
        # Drop late callbacks from the previous request/reconnect before reset.
        events.accepting_records = False
        pump()
        require_history_connection(events.connected)
        events.reset(symbol, interval)
        try:
            kline_type, minute_number, _maximum = INTERVAL_OPTIONS[interval]
            with history_request_limiter.slot():
                assert_history_code(
                    quote.SKQuoteLib_RequestKLineAMByDate(
                        symbol, kline_type, 1, 0, first.strftime("%Y%m%d"),
                        last.strftime("%Y%m%d"), minute_number,
                    ), center,
                )
                wait_for_history_completion(events.connected, events.completed, pump, timeout_seconds)
            if events.callback_error is not None:
                raise events.callback_error
            return _deduplicate(events.records)
        finally:
            events.accepting_records = False
            events.records.clear()
            events.callback_error = None

    result = []
    for first, last in ranges:
        result.extend(with_history_connection_recovery(
            lambda: request_once(first, last),
            lambda: reconnect_history_monitor(
                quote, center, events.connected, pump,
                connect_timeout_seconds if connect_timeout_seconds is not None else timeout_seconds,
            ),
        ))
    return _deduplicate(result)


def _excluded_record(symbol: str, interval: str, raw: str) -> bool:
    if (not symbol or interval not in {"week", "month"}
            or not excluded_periods(symbol, interval, date.min, date.max)):
        return False
    # Match the date BEFORE parsing the invalid volume, but never suppress a
    # malformed record/date or an unlisted error. This also guards SDK spillover.
    parts = raw.split(",")
    return len(parts) == 6 and excluded_label(symbol, interval, _parse_timestamp(parts[0]).date())


class _HistoryRequest:
    def __init__(self, symbol: str, interval: str, start_date: date, end_date: date) -> None:
        self.symbol = symbol
        self.interval = interval
        self.start_date = start_date
        self.end_date = end_date
        self.done = threading.Event()
        self.result: list[Candle] | None = None
        self.error: Exception | None = None


class _ReusableHistoryComSession:
    """One STA thread which retains Capital COM objects across K-line requests.

    COM callback objects must only be used from the apartment that created
    them.  A request queue gives the FastAPI worker a synchronous interface
    without recreating Login, EnterMonitorLONG, and event handlers per stock.
    The short idle timeout releases DLL resources after interactive use.
    """

    _STOP = object()

    def __init__(
        self, dll_path: Path, credentials: tuple[str, str, int], timeout_seconds: int,
        process_lock: Any, idle_timeout_seconds: float = 30.0,
    ) -> None:
        self.dll_path = dll_path
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.process_lock = process_lock
        self.idle_timeout_seconds = idle_timeout_seconds
        self.requests: queue.Queue[_HistoryRequest | object] = queue.Queue()
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.startup_error: Exception | None = None
        self.thread = threading.Thread(target=self._run, name="capital-history-com", daemon=True)

    @property
    def is_alive(self) -> bool:
        return self.thread.is_alive() and not self.closed.is_set()

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(
            self.timeout_seconds * (HISTORY_RECOVERY_ATTEMPTS + 1)
            + HISTORY_RECOVERY_BACKOFF_SECONDS * HISTORY_RECOVERY_ATTEMPTS + 10
        ):
            self.close()
            raise CapitalHistoryConnectionError("歷史 K 線 COM Session 啟動逾時")
        if self.startup_error is not None:
            if isinstance(self.startup_error, CapitalHistoryConnectionError):
                raise self.startup_error
            # Initial login/COM setup failures affect the whole batch, not an
            # individual stock. Stop rather than failing every later symbol.
            raise CapitalHistoryConnectionError(
                f"歷史 K 線連線初始化失敗：{self.startup_error}",
                code=getattr(self.startup_error, "code", None),
            ) from self.startup_error

    def request(self, symbol: str, interval: str, start_date: date, end_date: date) -> list[Candle]:
        if not self.is_alive:
            raise CapitalHistoryConnectionError("歷史 K 線 COM Session 已關閉")
        request = _HistoryRequest(symbol, interval, start_date, end_date)
        self.requests.put(request)
        if not request.done.wait(
            max(1, len(allowed_history_ranges(symbol, interval, start_date, end_date))) * (
                self.timeout_seconds * (2 * HISTORY_RECOVERY_ATTEMPTS + 1)
                + HISTORY_RECOVERY_BACKOFF_SECONDS * HISTORY_RECOVERY_ATTEMPTS + 15)
        ):
            self.close()
            raise CapitalHistoryConnectionError("歷史 K 線查詢逾時（含恢復連線等待）")
        if request.error is not None:
            if isinstance(request.error, CapitalHistoryError):
                raise request.error
            raise CapitalHistoryError(str(request.error)) from request.error
        return request.result or []

    def close(self) -> None:
        if not self.closed.is_set():
            self.requests.put(self._STOP)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(self.timeout_seconds + 10)

    def _run(self) -> None:
        pythoncom: Any | None = None
        directory_handle: Any | None = None
        center: Any | None = None
        quote: Any | None = None
        reply: Any | None = None
        reply_handler: Any | None = None
        quote_handler: Any | None = None
        reply_event: Any | None = None
        quote_event: Any | None = None
        try:
            try:
                import comtypes.client
                import pythoncom as imported_pythoncom
            except ImportError as error:
                raise CapitalHistoryError("歷史 K 線需要 Windows comtypes 與 pywin32 套件") from error
            if not self.dll_path.is_file():
                raise CapitalHistoryError(f"找不到 SKCOM.dll：{self.dll_path}")
            self.process_lock.acquire()
            pythoncom = imported_pythoncom
            pythoncom.CoInitialize()
            if hasattr(os, "add_dll_directory"):
                directory_handle = os.add_dll_directory(str(self.dll_path.parent))
            ensure_dll_directory_on_path(self.dll_path.parent)
            comtypes.client.GetModule(str(self.dll_path))
            sk = importlib.import_module("comtypes.gen.SKCOMLib")
            center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
            quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
            reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

            class ReplyEvent:
                def OnReplyMessage(self, _user_id: str, _message: str) -> int:
                    return -1

            reply_event = ReplyEvent()
            quote_event = HistoryQuoteEvents()
            reply_handler = comtypes.client.GetEvents(reply, reply_event)
            quote_handler = comtypes.client.GetEvents(quote, quote_event)
            user_id, password, authority = self.credentials
            CapitalHistoryClient._assert_code(center.SKCenterLib_SetAuthority(authority), center)
            assert_history_code(center.SKCenterLib_Login(user_id, password), center, allow_login_warning=True)
            try:
                enter_history_monitor(
                    quote, center, quote_event.connected, pythoncom.PumpWaitingMessages, self.timeout_seconds,
                )
            except CapitalHistoryConnectionError:
                with_history_connection_recovery(
                    lambda: require_history_connection(quote_event.connected),
                    lambda: reconnect_history_monitor(
                        quote, center, quote_event.connected, pythoncom.PumpWaitingMessages, self.timeout_seconds,
                    ),
                )
            self.ready.set()
            idle_deadline = time.monotonic() + self.idle_timeout_seconds
            while True:
                try:
                    item = self.requests.get(timeout=0.05)
                except queue.Empty:
                    pythoncom.PumpWaitingMessages()
                    if time.monotonic() >= idle_deadline:
                        break
                    continue
                if item is self._STOP:
                    break
                assert isinstance(item, _HistoryRequest)
                try:
                    item.result = request_history_candles(
                        quote, center, quote_event, pythoncom.PumpWaitingMessages,
                        item.symbol, item.interval, item.start_date, item.end_date, self.timeout_seconds,
                    )
                except Exception as error:
                    item.error = error
                finally:
                    # Time spent fetching/recovering is not idle time.
                    idle_deadline = time.monotonic() + self.idle_timeout_seconds
                    item.done.set()
                connection_failed = isinstance(item.error, CapitalHistoryConnectionError)
                # The waiting caller owns the result now. Do not retain the
                # final request's candle list during the idle session period.
                item = None
                if connection_failed:
                    # Do not reuse an exhausted/disconnected session next time.
                    break
        except Exception as error:
            self.startup_error = error
            self.ready.set()
        finally:
            self.closed.set()
            if quote is not None:
                try:
                    quote.SKQuoteLib_LeaveMonitor()
                except Exception:
                    pass
            reply_handler = quote_handler = reply_event = quote_event = None
            center = quote = reply = None
            gc.collect()
            if directory_handle is not None:
                directory_handle.close()
            if pythoncom is not None:
                pythoncom.CoUninitialize()
            # Any caller still waiting for an item must not wait indefinitely.
            while True:
                try:
                    item = self.requests.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, _HistoryRequest):
                    item.error = CapitalHistoryConnectionError("歷史 K 線 COM Session 已關閉")
                    item.done.set()
            try:
                self.process_lock.release()
            except RuntimeError:
                pass


def _deduplicate(candles: list[Candle]) -> list[Candle]:
    by_timestamp = {candle.timestamp: candle for candle in candles}
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]
