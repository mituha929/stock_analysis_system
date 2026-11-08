from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from app.schemas.models import Instrument, Quote, Tick
from app.schemas.models import Candle
from app.services.capital_history_service import CapitalHistoryClient, CapitalHistoryError
from app.services.capital_history_connection import (
    CapitalHistoryConnectionError, require_history_connection, with_history_connection_recovery,
)
from app.services.capital_process_lock import CapitalProcessLockError, get_capital_process_lock
from app.services.quote_cache import QuoteCache
from app.services.watchlist_store import WatchlistStore
from app.services.capital_connection_diagnostics import ConnectionDiagnostics, connection_source
from app.services.capital_dll_environment import configure_quote_callback_abi, ensure_dll_directory_on_path


SK_SUBJECT_CONNECTION_CONNECTED = 3001
SK_SUBJECT_CONNECTION_DISCONNECT = 3002
SK_SUBJECT_CONNECTION_STOCKS_READY = 3003

STOCK_LIST_MARKETS: dict[int, str] = {
    0: "上市",
    1: "上櫃",
    4: "興櫃",
    5: "盤中零股－上市",
    6: "盤中零股－上櫃",
}
# Exclude products outside the current stock-analysis scope.  The category
# identifiers are supplied by Capital's RequestStockList response.
EXCLUDED_STOCK_LIST_CATEGORY_NOS = {94, 98}  # 94=受益憑證、98=權證


class CapitalApiError(RuntimeError):
    pass


class CapitalQuoteService:
    """Quote service using the vendor's ctypes-based SKDLLPython wrapper.

    This avoids the COM event sink used by the previous implementation and uses
    the same native callback registration as SKDLLPythonTester.
    """

    def __init__(
        self, cache: QuoteCache, wrapper_path: str | None = None, watchlist_store: WatchlistStore | None = None,
    ) -> None:
        self.cache = cache
        bundle = Path(__file__).resolve().parents[3] / 'CapitalAPI_2.13.58_PythonExample'
        candidates = [bundle / 'CapitalAPI_2.13.58_PythonExample' / 'SKDLLPythonTester' / 'SKDLLPython.py',
                      bundle / 'SKDLLPythonTester' / 'SKDLLPython.py']
        default_wrapper = next((path for path in candidates if path.is_file()), candidates[0])
        configured = wrapper_path or os.getenv("CAPITAL_SKDLL_PY")
        self.wrapper_path = Path(configured).expanduser() if configured else default_wrapper
        self._lock = threading.RLock()
        self._process_lock = get_capital_process_lock()
        self._owns_process_lock = False
        self._started = False
        self._SK: Any | None = None
        self._status = "not_started"
        self._status_message = "尚未連線群益報價主機"
        self._quote_connection_state = "not_started"
        self._quote_connection_count: int | None = None
        self._quote_out_limit: bool | None = None
        self._quote_status_code: int | None = None
        self._stocks_ready = threading.Event()
        self._announcement_received = threading.Event()
        self._connection_events: deque[tuple[int, int]] = deque(maxlen=10)
        self._connection_diagnostics = ConnectionDiagnostics()
        self._connection_cleanup = False
        self._quote_use_login_id = False
        self._native_owner = None
        self._vendor_module = None
        self._dll_directory_handle = None
        self._dll_search_active = False
        self._callback_abi_configured = False
        self._pending_quotes = deque(maxlen=2000)
        default_store = Path(__file__).resolve().parents[2] / "data" / "watchlist.json"
        self._watchlist_store = watchlist_store or WatchlistStore(default_store)
        self._watchlist: dict[str, int] = self._watchlist_store.load()
        self._subscribed_symbols: set[str] = set()
        self._subscriptions_pending = False
        self._credentials: tuple[str, str, int] | None = None
        self._history_client = CapitalHistoryClient()

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_message(self) -> str:
        return self._status_message

    @property
    def quote_ready(self) -> bool:
        return self._stocks_ready.is_set()

    @property
    def quote_connection_state(self) -> str:
        return self._quote_connection_state

    @property
    def quote_connection_count(self) -> int | None:
        return self._quote_connection_count

    @property
    def quote_out_limit(self) -> bool | None:
        return self._quote_out_limit

    @property
    def quote_status_code(self) -> int | None:
        return self._quote_status_code

    @property
    def connection_events(self) -> list[tuple[int, int]]:
        with self._connection_diagnostics.lock:
            return list(self._connection_events)

    @property
    def connection_diagnostics(self) -> dict:
        return dict(self._connection_diagnostics.snapshot(), backend='dll',
                    native_owner_thread_id=self._native_owner,
                    dll_search_configured=self._dll_search_active,
                    callback_abi_configured=self._callback_abi_configured,
                    pending_quotes=len(self._pending_quotes))

    def _check_native_thread(self) -> None:
        owner = getattr(self, '_native_owner', None)
        if owner is not None and owner != threading.get_ident():
            raise CapitalApiError('DLL 操作必須排回初始化原生元件的執行緒')

    def _prepare_dll_environment(self) -> None:
        if self._dll_search_active:
            return
        directory = self.wrapper_path.resolve().parent / 'libs'
        if not directory.is_dir():
            raise CapitalApiError('SKDLLPython 的 libs 資料夾不存在')
        ensure_dll_directory_on_path(directory)
        if hasattr(os, 'add_dll_directory'):
            self._dll_directory_handle = os.add_dll_directory(str(directory))
        self._dll_search_active = True

    def _close_dll_environment(self) -> None:
        handle, self._dll_directory_handle = self._dll_directory_handle, None
        self._dll_search_active = False
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                self._connection_diagnostics.record('cleanup_resource_error', resource='dll_directory',
                                                    exception_type=type(error).__name__)

    def is_subscribed(self, symbol: str) -> bool:
        """Whether this running API process has already subscribed a symbol."""
        return symbol.upper() in self._subscribed_symbols

    def start(self) -> None:
        self._check_native_thread()
        with self._lock:
            if self._started:
                if self._vendor_module is not None:
                    self._prepare_dll_environment()
                return
            if not self.wrapper_path.is_file():
                raise CapitalApiError(f"找不到 SKDLLPython.py：{self.wrapper_path}")
            try:
                # Do not even load a second copy while a different API owns
                # the SDK. The lock remains held until this service stops.
                self._process_lock.acquire(timeout_seconds=0)
                self._owns_process_lock = True
                self._native_owner = threading.get_ident()
                self._prepare_dll_environment()
                self._vendor_module = self._load_wrapper(self.wrapper_path.resolve())
                self._SK = self._vendor_module.SK
                configure_quote_callback_abi(self._SK)
                self._callback_abi_configured = True
                # Register callbacks before Login, as required by the guide.
                self._SK.OnReplyMessage(self._on_reply_message)
                self._SK.OnConnection(self._on_connection)
                self._SK.OnNotifyQuoteLONG(self._on_quote)
                self._SK.OnNotifyBest5LONG(self._on_best5)
                self._SK.OnNotifyTicksLONG(self._on_tick)
                self._started = True
                self._status = "ready"
                self._status_message = "SKDLLPython 已初始化，等待登入"
            except Exception as error:
                self._close_dll_environment()
                self._release_process_lock()
                self._SK = self._vendor_module = None
                if isinstance(error, CapitalProcessLockError):
                    raise CapitalApiError(str(error)) from error
                raise

    def stop(self) -> None:
        self._check_native_thread()
        with self._connection_diagnostics.lock:
            self._connection_cleanup = True
            self._connection_diagnostics.phase = 'cleanup'
            self._connection_diagnostics.record('cleanup_start')
        # The history client owns a separate COM apartment.  End it before
        # releasing the process lock held by the primary quote connection.
        self._history_client.close()
        if not self._started or self._SK is None:
            self._close_dll_environment()
            self._release_process_lock()
            return
        try:
            with self._lock:
                try:
                    self._call_quote_connection(1, use_login_id=self._quote_use_login_id)
                except Exception:
                    # The helper has already recorded the exception type only.
                    # Native cleanup must not obscure the original failure.
                    pass
                self._stocks_ready.clear()
                self._subscribed_symbols.clear()
                self._pending_quotes.clear()
                self._subscriptions_pending = False
                self._quote_connection_state = "disconnected"
                self._status = "ready"
                with self._connection_diagnostics.lock:
                    self._connection_diagnostics.phase = 'stopped'
        finally:
            self._release_process_lock()
            self._close_dll_environment()

    def connect(self, user_id: str, password: str, authority: int = 0) -> tuple[str, str, bool]:
        self.start()
        assert self._SK is not None
        requested_credentials = (user_id, password, authority)
        # Do this before clearing events or calling SKCenterLib_Login.  The
        # vendor returns 2003 (already logged in) on a duplicate Login call;
        # treating that as a new connection used to erase a healthy API-side
        # quote_ready state while the DLL remained connected.
        with self._lock:
            if self._credentials is not None:
                if self._credentials != requested_credentials:
                    raise CapitalApiError("群益 DLL 已使用另一組帳號登入；目前封裝沒有登出函式，請重啟 API 後再使用新帳號登入")
                if self.quote_ready:
                    return "connected", "報價主機已連線，略過重複登入", True
                if self._quote_connection_state == "connecting":
                    return (
                        self._status,
                        "群益 API 已登入，仍在等待報價主機就緒；略過重複登入",
                        self._announcement_received.is_set(),
                    )
                reconnect_existing_session = True
            else:
                reconnect_existing_session = False
        acquired_now = False
        try:
            if not self._owns_process_lock:
                self._process_lock.acquire()
                self._owns_process_lock = True
                acquired_now = True
            with self._lock:
                with self._connection_diagnostics.lock:
                    self._connection_diagnostics.begin()
                    self._connection_cleanup = False
                    self._announcement_received.clear()
                    self._stocks_ready.clear()
                    self._connection_events.clear()
                    self._quote_connection_state = "connecting"
                self._subscribed_symbols.clear()
                if not reconnect_existing_session:
                    self._connection_diagnostics.phase = 'login'
                    self._connection_diagnostics.record('sdk_start', operation='Login')
                    try:
                        login = self._SK.Login(user_id, password, authority)
                        code = int(login.Code)
                    except Exception as error:
                        self._connection_diagnostics.record('sdk_exception', operation='Login', exception_type=type(error).__name__)
                        raise
                    self._connection_diagnostics.record('sdk_return', operation='Login', return_code=code)
                    self._assert_code(code)
                    self._credentials = requested_credentials
                self._status = "connecting"
                self._status_message = (
                    "已登入，正在連線國內行情主機"
                    if not reconnect_existing_session else "正在重新連線國內行情主機（沿用既有登入 Session）"
                )
                # The guide specifies an empty ID, but the bundled tester
                # supplies the authenticated ID. Some DLL builds reject the
                # documented call with LOGIN_FIRST even after Login returned 0.
                # Try the tester's form ONCE in that specific case, without
                # another Login, a changed account, or a changed target/status.
                self._connection_diagnostics.phase = 'opening_quote'
                self._quote_use_login_id = False
                code = self._call_quote_connection(0)
                if code == 1000 and self._credentials == requested_credentials:
                    self._connection_diagnostics.record('quote_login_id_fallback', reason_code=1000,
                                                        login_id_mode='authenticated_user')
                    self._quote_use_login_id = True
                    code = self._call_quote_connection(0, use_login_id=True)
                self._assert_code(code)
                with self._connection_diagnostics.lock:
                    if not self.quote_ready:
                        self._connection_diagnostics.phase = 'waiting_quote_ready'
                self._subscriptions_pending = bool(self._watchlist)
        except CapitalProcessLockError as error:
            raise CapitalApiError(str(error)) from error
        except Exception:
            with self._connection_diagnostics.lock:
                self._connection_diagnostics.phase = 'connect_failed'
                self._connection_diagnostics.record('connect_failed')
                self._stocks_ready.clear()
                self._quote_connection_state = 'disconnected'
                self._status = 'ready'
            if acquired_now:
                self._release_process_lock()
            raise

        # The announcement callback is required before the first Login.  A
        # reconnect only reopens the quote server in an already logged-in DLL,
        # so waiting for another announcement merely adds a needless delay.
        if not reconnect_existing_session:
            deadline = time.monotonic() + 3
            while not self._announcement_received.is_set() and time.monotonic() < deadline:
                self.poll()
                time.sleep(0.01)
        return self._status, self._status_message, self._announcement_received.is_set()

    def _call_quote_connection(self, status: int, *, use_login_id: bool = False) -> int:
        self._check_native_thread()
        assert self._SK is not None
        if use_login_id and self._credentials is None:
            raise CapitalApiError("尚未成功登入，不可使用登入 ID 建立報價連線")
        login_id = self._credentials[0] if use_login_id else ""
        operation = 'ManageServerConnection.connect' if status == 0 else 'ManageServerConnection.disconnect'
        mode = 'authenticated_user' if use_login_id else 'empty'
        self._connection_diagnostics.record('sdk_start', operation=operation, login_id_mode=mode)
        try:
            code = int(self._SK.ManageServerConnection(login_id, status, 1))
        except Exception as error:
            self._connection_diagnostics.record('sdk_exception', operation=operation,
                                                login_id_mode=mode, exception_type=type(error).__name__)
            raise
        self._connection_diagnostics.record('sdk_return', operation=operation,
                                            login_id_mode=mode, return_code=code)
        return code

    def disconnect(self) -> str:
        self.stop()
        # SKDLLPython exposes ManageServerConnection but no CenterLib Logout.
        # Keep this in-process marker so a same-account reconnect does not
        # call Login again and receive 2003 (already logged in).
        self._status_message = "已中斷報價主機；群益登入 Session 保留，重新連線時不需再次登入"
        return self._status_message

    def subscribe(self, symbol: str, page_no: int) -> str:
        normalized = symbol.upper()
        self._watchlist[normalized] = page_no
        self._watchlist_store.save(self._watchlist)
        if self.is_subscribed(normalized):
            return f"{normalized} 已在訂閱中"
        if not self.quote_ready:
            self._subscriptions_pending = True
            return "已加入自選股；連線群益後會自動訂閱"
        self._subscribe(normalized)
        return f"已訂閱 {normalized}"

    def unsubscribe(self, symbol: str) -> str:
        self._check_native_thread()
        normalized = symbol.upper()
        self._watchlist.pop(normalized, None)
        self._watchlist_store.save(self._watchlist)
        if self._SK is not None and self.quote_ready and self.is_subscribed(normalized):
            with self._lock:
                self._assert_code(int(self._SK.SKQuoteLib_CancelRequestStocks(normalized)))
                self._assert_code(int(self._SK.SKQuoteLib_CancelRequestTicks(normalized)))
        self._subscribed_symbols.discard(normalized)
        return f"已取消訂閱 {normalized}"

    def watchlist(self) -> list[str]:
        return list(self._watchlist)

    def refresh_quote_connection_state(self) -> str:
        # The native wrapper exposes connection state through OnConnection.
        return self._quote_connection_state

    def fetch_historical_candles(
        self, symbol: str, interval: str, start_date: Any, end_date: Any, *, cache_result: bool = True,
    ) -> list[Candle]:
        self._check_native_thread()
        if self._credentials is None:
            error = CapitalHistoryConnectionError("請先成功登入群益 API，才能查詢歷史 K 線")
            raise CapitalApiError(str(error)) from error
        user_id, password, authority = self._credentials
        try:
            if not self.quote_ready:
                def reconnect() -> None:
                    # Only reconnect an already authenticated session. Never
                    # read .env credentials or issue another hidden Login.
                    try:
                        self.connect(user_id, password, authority)
                    except Exception as error:
                        raise CapitalHistoryConnectionError(str(error)) from error
                    deadline = time.monotonic() + self._history_client.timeout_seconds
                    while not self.quote_ready and time.monotonic() < deadline:
                        self.poll()
                        time.sleep(0.01)
                    if not self.quote_ready:
                        raise CapitalHistoryConnectionError("群益主行情恢復連線逾時")

                with_history_connection_recovery(
                    lambda: require_history_connection(self._stocks_ready), reconnect,
                )
            candles = self._history_client.fetch(user_id, password, authority, symbol.upper(), interval, start_date, end_date)
        except CapitalHistoryError as error:
            raise CapitalApiError(str(error)) from error
        if cache_result:
            self.cache.set_historical_candles(symbol, interval, candles)
        return candles

    def fetch_stock_list(self, market_no: int) -> list[Instrument]:
        """Return the current product master list for one supported market.

        The vendor's ctypes wrapper returns the complete list synchronously,
        already grouped by commodity category.  It is not a quote subscription
        and therefore does not change the API's watchlist.
        """
        self._check_native_thread()
        if market_no not in STOCK_LIST_MARKETS:
            allowed = "、".join(str(item) for item in STOCK_LIST_MARKETS)
            raise CapitalApiError(f"market_no 僅支援 {allowed}")
        if self._credentials is None:
            raise CapitalApiError("請先成功登入群益 API，才能查詢商品清單")
        if not self.quote_ready:
            raise CapitalApiError("群益主行情尚未就緒，請先完成報價主機連線")
        if self._SK is None:
            raise CapitalApiError("群益 DLL 尚未啟動")

        try:
            with self._lock:
                # The DLL guide explicitly requires LoadCommodity BEFORE
                # RequestStockList. It is not evidence of quote readiness.
                code = int(self._SK.LoadCommodity(market_no))
                self._connection_diagnostics.record('sdk_return', operation='LoadCommodity',
                                                    market_no=market_no, return_code=code)
                self._assert_code(code)
                stock_list = self._SK.RequestStockList(market_no)
        except Exception as error:
            raise CapitalApiError(f"群益商品清單 API 呼叫失敗：{error}") from error

        instruments: list[Instrument] = []
        try:
            type_lists = stock_list.AllTypeLists
            for category in type_lists:
                category_no = int(category.TypeNo)
                if category_no in EXCLUDED_STOCK_LIST_CATEGORY_NOS:
                    continue
                for item in category.Items:
                    expiry_date = str(item.strExpiryDate).strip() or None
                    instruments.append(Instrument(
                        market_no=market_no,
                        category_no=category_no,
                        category_name=str(category.TypeName).strip(),
                        symbol=str(item.strQuoteCode).strip().upper(),
                        name=str(item.strStockName).strip(),
                        order_symbol=str(item.strOrderCode).strip().upper(),
                        expiry_date=expiry_date,
                    ))
        except (AttributeError, TypeError, ValueError) as error:
            raise CapitalApiError(f"群益商品清單資料格式無法解析：{error}") from error
        return instruments

    def _release_process_lock(self) -> None:
        if not self._owns_process_lock:
            return
        try:
            self._process_lock.release()
        finally:
            self._owns_process_lock = False

    @staticmethod
    def _load_wrapper(path: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location("capital_vendor_skdll", path)
        if spec is None or spec.loader is None:
            raise CapitalApiError(f"無法載入 SKDLLPython.py：{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _assert_code(self, code: int) -> None:
        if code == 0:
            return
        message = ""
        if self._SK is not None:
            try:
                message = str(self._SK.GetMessage(code))
            except Exception:
                pass
        raise CapitalApiError(f"群益 API 錯誤 {code}{f'：{message}' if message else ''}")

    def _on_reply_message(self, _login_id: str, _message: str) -> None:
        self._connection_diagnostics.record('announcement_received', source='[REDACTED]')
        self._announcement_received.set()

    def _on_connection(self, login_id: str, code: int) -> None:
        channel, source = connection_source(login_id)
        try:
            code = int(code)
        except (TypeError, ValueError):
            self._connection_diagnostics.record('invalid_connection_event', channel=channel, source=source)
            return
        with self._connection_diagnostics.lock:
            applied = channel == 'domestic_quote' and not self._connection_cleanup
            self._connection_diagnostics.record('connection_event', channel=channel, source=source,
                code=code, error_code=None, applied=applied, during_cleanup=self._connection_cleanup)
            if channel != 'domestic_quote':
                return
            # Compatibility-only pair: this wrapper supplies NO nErrorCode.
            # The structured diagnostic explicitly reports error_code=null.
            self._connection_events.append((code, 0))
            if not applied:
                return
            self._connection_diagnostics.last_quote_code = code
            if code == SK_SUBJECT_CONNECTION_STOCKS_READY:
                self._stocks_ready.set()
                self._connection_diagnostics.phase = 'quote_ready'
                self._quote_connection_state = "connected"
                self._status = "connected"
                self._status_message = "已連線國內行情主機，商品基本資料已就緒"
            elif code in (SK_SUBJECT_CONNECTION_DISCONNECT, 3033):
                self._stocks_ready.clear()
                self._subscribed_symbols.clear()
                self._subscriptions_pending = bool(self._watchlist)
                self._connection_diagnostics.phase = 'quote_disconnected'
                self._quote_connection_state = "disconnected"
                self._status = "ready"
                self._status_message = f"報價主機已斷線（代碼 {code}）"
            elif code == SK_SUBJECT_CONNECTION_CONNECTED:
                self._stocks_ready.clear()
                self._subscribed_symbols.clear()
                self._subscriptions_pending = bool(self._watchlist)
                self._connection_diagnostics.phase = 'waiting_quote_ready'
                self._quote_connection_state = "connecting"
                self._status = "connecting"
                self._status_message = "報價主機已建立連線，等待商品資料"

    def mark_connection_timeout(self) -> bool:
        with self._connection_diagnostics.lock:
            if self.quote_ready:
                return False  # A ready callback raced the deadline check.
            self._connection_diagnostics.phase = 'timeout'
            self._connection_diagnostics.record('wait_quote_timeout', source='SKQuote', expected_code=3003)
            return True

    def _subscribe_pending(self) -> None:
        # One pass per connection; no background thread or per-poll retry loop.
        self._subscriptions_pending = False
        try:
            for symbol in list(self._watchlist):
                self._subscribe(symbol)
            self._subscriptions_pending = False
        except Exception as error:
            self._status_message = f"報價主機已就緒，但自選股訂閱失敗：{error}"

    def _subscribe(self, symbol: str) -> None:
        self._check_native_thread()
        if self._SK is None:
            raise CapitalApiError("群益 DLL 尚未啟動")
        with self._lock:
            if symbol in self._subscribed_symbols:
                return
            self._assert_code(int(self._SK.SKQuoteLib_RequestStocks(symbol)))
            # RequestTicks also drives the native Best5 and Tick callbacks.
            self._assert_code(int(self._SK.SKQuoteLib_RequestTicks(self._watchlist[symbol], symbol)))
            self._subscribed_symbols.add(symbol)

    def poll(self) -> None:
        self._check_native_thread()
        import pythoncom
        pythoncom.PumpWaitingMessages()
        if self._connection_cleanup:
            return
        if self.quote_ready and self._subscriptions_pending:
            self._subscribe_pending()
        for _ in range(min(200, len(self._pending_quotes))):
            market, symbol = self._pending_quotes.popleft()
            if self.quote_ready:
                try:
                    self._apply_quote(market, symbol)
                except Exception as error:
                    self._connection_diagnostics.record('quote_snapshot_error', exception_type=type(error).__name__)

    def _on_quote(self, market_no: int, stock_no: str) -> None:
        # A vendor callback may run while the initiating native call is still
        # waiting. Never acquire its DLL lock or issue another SDK call here.
        if not self._connection_cleanup:
            self._pending_quotes.append((int(market_no), str(stock_no)))

    def _apply_quote(self, market_no: int, stock_no: str) -> None:
        self._check_native_thread()
        if self._SK is None:
            return
        with self._lock:
            stock = self._SK.SKQuoteLib_GetStockByStockNo(int(market_no), str(stock_no))
        if int(stock.nCode) != 0:
            return
        self.cache.upsert_quote(Quote(
            symbol=str(stock.strStockNo), name=str(stock.strStockName),
            price=self.cache.price(int(stock.nClose)), open=self.cache.price(int(stock.nOpen)),
            high=self.cache.price(int(stock.nHigh)), low=self.cache.price(int(stock.nLow)),
            prev_close=self.cache.price(int(stock.nRef)), bid=self.cache.price(int(stock.nBid)),
            bid_volume=int(stock.nBc), ask=self.cache.price(int(stock.nAsk)),
            ask_volume=int(stock.nAc), volume=int(stock.nTQty), updated_at=self.cache.now(),
        ))

    def _on_best5(
        self, _market_no: int, stock_no: str, bids: list[int], bid_qtys: list[int],
        asks: list[int], ask_qtys: list[int], *_rest: int,
    ) -> None:
        # ctypes passes c_char_p callback values as bytes, unlike the wrapper's
        # quote and tick callbacks which already decode the stock number.
        symbol = stock_no.decode("cp950") if isinstance(stock_no, bytes) else str(stock_no)
        self.cache.upsert_best5(symbol, list(zip(bids, bid_qtys)), list(zip(asks, ask_qtys)))

    def _on_tick(
        self, _market_no: int, stock_no: str, _ptr: int, date: int, time_hms: int,
        _microseconds: int, bid: int, ask: int, close: int, qty: int, _simulate: int,
    ) -> None:
        try:
            timestamp = datetime.strptime(f"{date:08d}{time_hms:06d}", "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            timestamp = datetime.now(timezone.utc)
        self.cache.add_tick(str(stock_no), Tick(
            timestamp=timestamp, price=self.cache.price(int(close)), volume=int(qty),
            bid=self.cache.price(int(bid)), ask=self.cache.price(int(ask)),
        ))
