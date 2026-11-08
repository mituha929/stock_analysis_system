from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.schemas.models import Candle, Quote, Tick
from app.services.capital_dll_environment import ensure_dll_directory_on_path
from app.services.capital_history_service import INTERVAL_OPTIONS, CapitalHistoryError, parse_kline_record, validate_history_range
from app.services.quote_cache import QuoteCache
from app.services.watchlist_store import WatchlistStore

SK_SUBJECT_CONNECTION_CONNECTED = 3001
SK_SUBJECT_CONNECTION_DISCONNECT = 3002
SK_SUBJECT_CONNECTION_STOCKS_READY = 3003


class CapitalApiError(RuntimeError):
    pass


class CapitalQuoteService:
    """Owns the SKCOM objects and runs their callbacks on one COM thread."""

    def __init__(self, cache: QuoteCache, dll_path: str | None = None, watchlist_store: WatchlistStore | None = None) -> None:
        self.cache = cache
        default_dll = Path(__file__).resolve().parents[3] / "CapitalAPI_2.13.58_PythonExample" / "元件" / "x64" / "SKCOM.dll"
        configured = dll_path or os.getenv("CAPITAL_SKCOM_DLL")
        self.dll_path = Path(configured).expanduser() if configured else default_dll
        self._commands: queue.Queue[tuple[str, tuple[Any, ...], queue.Queue[Any]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._stop = threading.Event()
        self._stocks_ready = threading.Event()
        self._announcement_received = threading.Event()
        self._announcement_messages: deque[str] = deque(maxlen=5)
        self._status = "not_started"
        self._status_message = "尚未連線群益報價主機"
        self._quote_connection_state = "not_started"
        self._quote_connection_count: int | None = None
        self._quote_out_limit: bool | None = None
        self._quote_status_code: int | None = None
        self._connection_events: list[tuple[int, int]] = []
        self._subscriptions_pending = False
        self._dll_directory_handle: Any | None = None
        default_store = Path(__file__).resolve().parents[2] / "data" / "watchlist.json"
        self._watchlist_store = watchlist_store or WatchlistStore(default_store)
        self._watchlist: dict[str, int] = self._watchlist_store.load()
        self._history_candles: list[Candle] = []
        self._history_complete = threading.Event()
        self._history_parse_error: Exception | None = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_message(self) -> str:
        return self._status_message

    @property
    def quote_ready(self) -> bool:
        """Whether SKQuoteLib has reported STOCKS_READY (connection kind 3003)."""
        return self._stocks_ready.is_set()

    @property
    def connection_events(self) -> list[tuple[int, int]]:
        """Recent OnConnection(kind, code) callbacks, for connection diagnostics."""
        return list(self._connection_events)

    @property
    def quote_connection_state(self) -> str:
        """Current value reported by SKQuoteLib_IsConnected()."""
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

    def refresh_quote_connection_state(self) -> str:
        """Query SKQuoteLib on its owning COM thread for an immediate diagnostic."""
        if not self._thread or not self._thread.is_alive():
            return self._quote_connection_state
        return self._call("diagnostics", timeout=4)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._started.clear()
        self._thread = threading.Thread(target=self._run, name="capital-com", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise CapitalApiError("群益 COM 背景服務啟動逾時")
        if self._status == "error":
            raise CapitalApiError(self._status_message)

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            try:
                self._call("disconnect")
            finally:
                self._stop.set()
                self._thread.join(timeout=5)

    def connect(self, user_id: str, password: str, authority: int = 0) -> tuple[str, str, bool]:
        self.start()
        return self._call("connect", user_id, password, authority, timeout=15)

    def disconnect(self) -> str:
        return self._call("disconnect")

    def subscribe(self, symbol: str, page_no: int) -> str:
        normalized = symbol.upper()
        self._watchlist[normalized] = page_no
        self._watchlist_store.save(self._watchlist)
        if self.status != "connected":
            self._subscriptions_pending = True
            return "已加入自選股；連線群益後會自動訂閱"
        return self._call("subscribe", normalized, page_no)

    def unsubscribe(self, symbol: str) -> str:
        normalized = symbol.upper()
        self._watchlist.pop(normalized, None)
        self._watchlist_store.save(self._watchlist)
        if self.status != "connected":
            return "已從自選股移除"
        return self._call("unsubscribe", normalized)

    def watchlist(self) -> list[str]:
        return list(self._watchlist)

    def fetch_historical_candles(self, symbol: str, interval: str, start_date: date, end_date: date) -> list[Candle]:
        if not self.quote_ready:
            raise CapitalApiError("群益主行情尚未就緒，請先完成報價主機連線")
        try:
            validate_history_range(interval, start_date, end_date)
        except CapitalHistoryError as error:
            raise CapitalApiError(str(error)) from error
        return self._call("history_kline", symbol.upper(), interval, start_date, end_date, timeout=40)

    def _call(self, name: str, *args: Any, timeout: float = 25) -> Any:
        if not self._thread or not self._thread.is_alive():
            raise CapitalApiError("群益 COM 背景服務尚未啟動")
        result: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._commands.put((name, args, result))
        try:
            value = result.get(timeout=timeout)
        except queue.Empty as error:
            raise CapitalApiError(f"群益 COM 執行 {name} 逾時，目前狀態：{self._status_message}") from error
        if isinstance(value, Exception):
            raise value
        return value

    def _run(self) -> None:
        try:
            import comtypes
            import comtypes.client

            if not self.dll_path.is_file():
                raise CapitalApiError("找不到 SKCOM.dll。請設定 CAPITAL_SKCOM_DLL。")
            comtypes.CoInitialize()
            # FastAPI 的工作目錄與群益 DLL 資料夾不同；明確加入相依元件
            # （SecuCompx64.dll、CTSecuritiesATL.dll 等）的搜尋路徑。
            if hasattr(os, "add_dll_directory"):
                self._dll_directory_handle = os.add_dll_directory(str(self.dll_path.parent))
            ensure_dll_directory_on_path(self.dll_path.parent)
            # 直接在記憶體產生 typelib wrapper，避免新版 DLL 沿用舊版
            # comtypes 快取而發生 Typelib different than module。
            comtypes.client.gen_dir = None
            sk = comtypes.client.GetModule(str(self.dll_path))

            center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
            quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
            reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
            # SKCOM exposes several event source interfaces. Explicitly select
            # the intended ones so callbacks are not bound to another default
            # source interface in this non-GUI FastAPI process.
            # 事件接收物件必須存活到 COM 執行緒結束。官方 Quote.py 也以全域
            # 變數保留這些物件；若只在 GetEvents 呼叫中建立，可能被 GC 回收，
            # 導致 OnConnection / OnNotifyQuote 等回呼完全不會進來。
            reply_event = self._reply_event()
            quote_event = self._quote_event(sk, quote)
            reply_handler = comtypes.client.GetEvents(reply, reply_event)
            quote_handler = comtypes.client.GetEvents(quote, quote_event)
            self._status = "ready"
            self._status_message = "SKCOM 已初始化，等待登入"
            self._started.set()

            while not self._stop.is_set():
                try:
                    name, args, result = self._commands.get(timeout=0.05)
                    self._handle_command(name, args, result, center, quote)
                except queue.Empty:
                    pass
                comtypes.client.PumpEvents(0.01)
                if self._stocks_ready.is_set() and self._subscriptions_pending:
                    self._subscribe_pending(quote, center)
            del reply_handler, quote_handler, reply_event, quote_event
            comtypes.CoUninitialize()
        except Exception as error:
            self._status = "error"
            self._status_message = str(error)
            self._started.set()

    def _handle_command(self, name: str, args: tuple[Any, ...], result: queue.Queue[Any], center: Any, quote: Any) -> None:
        try:
            if name == "connect":
                user_id, password, authority = args
                self._announcement_received.clear()
                self._announcement_messages.clear()
                self._assert_code(center.SKCenterLib_SetAuthority(authority), center)
                self._assert_code(center.SKCenterLib_Login(user_id, password), center)
                # 官方要求登入前先註冊公告 callback。登入後保留短暫時間
                # 處理 COM 訊息，以便把是否收到公告回傳給 HTTP 呼叫端。
                announcement_deadline = time.monotonic() + 3
                while not self._announcement_received.is_set() and time.monotonic() < announcement_deadline:
                    import comtypes.client
                    comtypes.client.PumpEvents(0.05)
                self._stocks_ready.clear()
                self._connection_events.clear()
                self._quote_connection_state = "connecting"
                self._status = "connecting"
                self._status_message = "已登入，等待報價主機下載商品基本資料"
                self._assert_code(quote.SKQuoteLib_EnterMonitorLONG(), center)
                # EnterMonitorLONG 的 0 只代表開始連線；3003 callback 才可訂閱。
                # 不在 HTTP 請求中阻塞等待，避免延遲下載造成登入看似失敗。
                self._subscriptions_pending = bool(self._watchlist)
                result.put((self._status, self._status_message, self._announcement_received.is_set()))
            elif name == "disconnect":
                quote.SKQuoteLib_LeaveMonitor()
                self._stocks_ready.clear()
                self._quote_connection_state = "disconnected"
                self._status = "ready"
                self._status_message = "已中斷報價主機"
                result.put(self._status_message)
            elif name == "diagnostics":
                # 官方範例：0=斷線、1=連線中、2=下載中。
                connection_state = int(quote.SKQuoteLib_IsConnected())
                state_names = {0: "disconnected", 1: "connecting", 2: "downloading"}
                self._quote_connection_state = state_names.get(connection_state, f"unknown({connection_state})")
                # 官方範例會同時查詢報價連線數及是否超過帳號連線限制。
                count, out_limit, status_code = quote.SKQuoteLib_GetQuoteStatus(0, False)
                self._quote_connection_count = int(count)
                self._quote_out_limit = bool(out_limit)
                self._quote_status_code = int(status_code)
                result.put(self._quote_connection_state)
            elif name == "subscribe":
                symbol, page_no = args
                self._subscribe(quote, center, symbol, page_no)
                result.put(f"已訂閱 {symbol}")
            elif name == "unsubscribe":
                self._assert_code(quote.SKQuoteLib_CancelRequestStocks(args[0]), center)
                result.put(f"已取消訂閱 {args[0]}")
            elif name == "history_kline":
                symbol, interval, start_date, end_date = args
                self._history_candles = []
                self._history_parse_error = None
                self._history_complete.clear()
                kline_type, minute_number, _maximum = INTERVAL_OPTIONS[interval]
                self._assert_code(
                    quote.SKQuoteLib_RequestKLineAMByDate(
                        symbol, kline_type, 1, 0, start_date.strftime("%Y%m%d"),
                        end_date.strftime("%Y%m%d"), minute_number,
                    ),
                    center,
                )
                deadline = time.monotonic() + 30
                while not self._history_complete.is_set() and time.monotonic() < deadline:
                    import comtypes.client
                    comtypes.client.PumpEvents(0.05)
                if not self._history_complete.is_set():
                    raise CapitalApiError("群益歷史 K 線回補逾時，未收到 OnKLineComplete（##）")
                if self._history_parse_error is not None:
                    raise CapitalApiError(f"群益歷史 K 線資料解析失敗：{self._history_parse_error}")
                candles = sorted(self._history_candles, key=lambda candle: candle.timestamp)
                self.cache.set_historical_candles(symbol, interval, candles)
                result.put(candles)
            else:
                raise CapitalApiError(f"未知指令：{name}")
        except Exception as error:
            result.put(error)

    @staticmethod
    def _assert_code(code: int, center: Any) -> None:
        if code != 0:
            message = center.SKCenterLib_GetReturnCodeMessage(code)
            last_log = center.SKCenterLib_GetLastLogInfo()
            detail = f"；最後診斷：{last_log}" if last_log else ""
            raise CapitalApiError(f"群益 API 錯誤 {code}: {message}{detail}")

    def _subscribe(self, quote: Any, center: Any, symbol: str, page_no: int) -> None:
        _, code = quote.SKQuoteLib_RequestStocks(page_no, symbol)
        self._assert_code(code, center)

    def _subscribe_pending(self, quote: Any, center: Any) -> None:
        try:
            for symbol, page_no in self._watchlist.items():
                self._subscribe(quote, center, symbol, page_no)
            self._subscriptions_pending = False
        except Exception as error:
            self._status_message = f"報價主機已就緒，但自選股訂閱失敗：{error}"

    def _reply_event(self) -> Any:
        service = self

        class ReplyEvent:
            def OnReplyMessage(self, bstrUserID: str, bstrMessages: str) -> int:
                # 不保存登入帳號；只保留公告內容供診斷使用。
                service._announcement_messages.append(str(bstrMessages))
                service._announcement_received.set()
                return -1

        return ReplyEvent()

    def _quote_event(self, sk: Any, quote_api: Any) -> Any:
        cache = self.cache
        service = self

        class QuoteEvent:
            def OnConnection(self, nKind: int, nCode: int) -> None:
                # SKCOM 的 Python typelib 未匯出這些常數；數值依官方
                # V2.13.58 手冊：3001=CONNECTED、3002=DISCONNECT、3003=STOCKS_READY。
                try:
                    # comtypes may deliver a Python int or a ctypes scalar.
                    kind = int(getattr(nKind, "value", nKind))
                    code = int(getattr(nCode, "value", nCode))
                    service._connection_events.append((kind, code))
                    del service._connection_events[:-10]
                    if kind == SK_SUBJECT_CONNECTION_STOCKS_READY:
                        service._stocks_ready.set()
                        service._quote_connection_state = "connected"
                        service._status = "connected"
                        service._status_message = "已登入並連線報價主機，商品基本資料已就緒"
                    elif kind == SK_SUBJECT_CONNECTION_DISCONNECT:
                        service._stocks_ready.clear()
                        service._quote_connection_state = "disconnected"
                        service._status = "ready"
                        service._status_message = f"報價主機已斷線（代碼 {code}）"
                except Exception as error:
                    # Do not let a COM sink exception disappear silently.
                    service._status_message = f"OnConnection callback 處理失敗：{error}"

            def OnNotifyQuoteLONG(self, sMarketNo: int, nIndex: int) -> None:
                stock = sk.SKSTOCKLONG()
                stock, code = quote_api.SKQuoteLib_GetStockByIndexLONG(sMarketNo, nIndex, stock)
                if code != 0:
                    return
                cache.upsert_quote(Quote(
                    symbol=str(stock.bstrStockNo), name=str(stock.bstrStockName),
                    price=cache.price(stock.nClose), open=cache.price(stock.nOpen),
                    high=cache.price(stock.nHigh), low=cache.price(stock.nLow),
                    prev_close=cache.price(stock.nRef), bid=cache.price(stock.nBid),
                    bid_volume=int(stock.nBc), ask=cache.price(stock.nAsk),
                    ask_volume=int(stock.nAc), volume=int(stock.nTQty), updated_at=cache.now(),
                ))

            def OnNotifyBest5LONG(self, sMarketNo: int, nStockidx: int, *data: int) -> None:
                # 官方 callback 順序是買一價量至買五、延伸買盤、賣一價量至賣五、延伸賣盤、試撮。
                stock = sk.SKSTOCKLONG()
                stock, code = quote_api.SKQuoteLib_GetStockByIndexLONG(sMarketNo, nStockidx, stock)
                if code != 0 or len(data) < 22:
                    return
                bids = [(data[i], data[i + 1]) for i in range(0, 10, 2)]
                asks = [(data[i], data[i + 1]) for i in range(12, 22, 2)]
                cache.upsert_best5(str(stock.bstrStockNo), bids, asks)

            def OnNotifyTicksLONG(self, sMarketNo: int, nIndex: int, nPtr: int, nDate: int, nTimehms: int, nTimemillismicros: int, nBid: int, nAsk: int, nClose: int, nQty: int, nSimulate: int) -> None:
                stock = sk.SKSTOCKLONG()
                stock, code = quote_api.SKQuoteLib_GetStockByIndexLONG(sMarketNo, nIndex, stock)
                if code != 0:
                    return
                text = f"{nDate:08d}{nTimehms:06d}"
                timestamp = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                cache.add_tick(str(stock.bstrStockNo), Tick(timestamp=timestamp, price=cache.price(nClose), volume=int(nQty), bid=cache.price(nBid), ask=cache.price(nAsk)))

            def OnNotifyKLineData(self, bstrStockNo: str, bstrData: str) -> None:
                try:
                    service._history_candles.append(parse_kline_record(str(bstrData)))
                except Exception as error:
                    service._history_parse_error = error

            def OnKLineComplete(self, bstrEndString: str) -> None:
                if str(bstrEndString).startswith("##"):
                    service._history_complete.set()

        return QuoteEvent()
