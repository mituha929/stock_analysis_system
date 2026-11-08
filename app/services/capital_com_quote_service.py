"""Direct COM backend, confined to the native worker's single STA thread.

No order objects, database access, extra login or history apartment. Callbacks
record state/data only; native calls and subscription restoration run in poll.
"""
from collections import deque
from datetime import datetime
import gc
import os
from pathlib import Path
import re
import struct
import threading
import time

from app.schemas.models import Instrument, Quote, Tick
from app.services.capital_dll_environment import ensure_dll_directory_on_path
from app.services.capital_dll_quote_service import (
    CapitalApiError, CapitalQuoteService, EXCLUDED_STOCK_LIST_CATEGORY_NOS, STOCK_LIST_MARKETS,
)
from app.services.capital_history_connection import require_history_connection
from app.services.capital_history_service import (
    HistoryQuoteEvents, TAIPEI, request_history_candles, validate_history_range,
)


def integer(value):
    return int(getattr(value, 'value', value))


class ComQuoteEvents(HistoryQuoteEvents):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def OnConnection(self, kind, code):
        kind, code = integer(kind), integer(code)
        super().OnConnection(kind, code)
        self.service._on_com_connection(kind, code)

    def OnNotifyQuoteLONG(self, market, index):
        self.service._pending.append(('quote', (integer(market), integer(index))))

    def OnNotifyBest5LONG(self, market, index, *data):
        self.service._pending.append(('best5', (integer(market), integer(index), *map(integer, data))))

    def OnNotifyTicksLONG(self, *data):
        self.service._pending.append(('tick', tuple(map(integer, data))))

    def OnNotifyCommodityListWithTypeNo(self, market, data):
        self.service._on_commodity_list(integer(market), str(data))


class CapitalComQuoteService(CapitalQuoteService):
    """Reuse public state/cache/validation, NOT the ctypes wrapper or callbacks."""
    def __init__(self, cache, *, dll_path=None, watchlist_store=None):
        super().__init__(cache, watchlist_store=watchlist_store)
        architecture = 'x64' if struct.calcsize('P') == 8 else 'x86'
        root = Path(__file__).resolve().parents[3] / 'CapitalAPI_2.13.58_PythonExample'
        candidates = [root / 'CapitalAPI_2.13.58_PythonExample' / '元件' / architecture / 'SKCOM.dll',
                      root / '元件' / architecture / 'SKCOM.dll']
        configured = dll_path or os.getenv('CAPITAL_SKCOM_DLL')
        self.dll_path = Path(configured).expanduser() if configured else next(
            (p for p in candidates if p.is_file()), candidates[0])
        self._owner = None
        self._pythoncom = self._client = self._types = None
        self._center = self._quote = self._reply = None
        self._handlers = []
        self._quote_events = None
        self._directory = None
        self._apartment = False
        self._pending = deque(maxlen=2000)
        self._restore_requested = False
        self._list_market = None
        self._list_done = threading.Event()
        self._list_rows = {}
        self._list_error = None

    @property
    def connection_diagnostics(self):
        return dict(self._connection_diagnostics.snapshot(), backend='com', owner_thread_id=self._owner,
                    sdk_connection_value=getattr(self, '_sdk_connection_value', None))

    def _check_owner(self):
        if self._owner is not None and self._owner != threading.get_ident():
            raise CapitalApiError('COM 操作必須留在建立物件的 STA 執行緒')

    @staticmethod
    def _load_com_runtime():
        import pythoncom
        import comtypes.client
        return pythoncom, comtypes.client

    def start(self):
        self._check_owner()
        if self._started:
            return
        if not self.dll_path.is_file():
            raise CapitalApiError('找不到 SKCOM.dll，請檢查 CAPITAL_SKCOM_DLL')
        self._owner = threading.get_ident()
        try:
            self._process_lock.acquire(timeout_seconds=0)
            self._owns_process_lock = True
            self._pythoncom, self._client = self._load_com_runtime()
            self._pythoncom.CoInitializeEx(self._pythoncom.COINIT_APARTMENTTHREADED)
            self._apartment = True
            if hasattr(os, 'add_dll_directory'):
                self._directory = os.add_dll_directory(str(self.dll_path.parent))
            ensure_dll_directory_on_path(self.dll_path.parent)
            self._client.gen_dir = None  # Child-local: do not reuse stale typelib files.
            self._types = self._client.GetModule(str(self.dll_path))
            sk = self._types
            self._center = self._client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
            self._quote = self._client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
            self._reply = self._client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
            service = self

            class ReplyEvents:
                def OnReplyMessage(self, _login_id, _message):
                    service._on_reply_message('', '')  # Never retain account or body.
                    return -1

            self._reply_events = ReplyEvents()
            self._quote_events = ComQuoteEvents(self)
            self._handlers.append(self._client.GetEvents(
                self._reply, self._reply_events, interface=sk._ISKReplyLibEvents))
            self._handlers.append(self._client.GetEvents(
                self._quote, self._quote_events, interface=sk._ISKQuoteLibEvents))
            self._started = True
            self._status = 'ready'
            self._status_message = 'COM STA 已初始化，等待登入'
        except Exception:
            self.stop()
            raise

    def _sdk_code(self, operation, function):
        self._check_owner()
        self._connection_diagnostics.record('sdk_start', operation=operation)
        try:
            code = integer(function())
        except Exception as error:
            self._connection_diagnostics.record('sdk_exception', operation=operation,
                                                exception_type=type(error).__name__)
            raise
        self._connection_diagnostics.record('sdk_return', operation=operation, return_code=code)
        return code

    def _assert_code(self, code):
        if integer(code) != 0:
            # Do not expose SDK LastLogInfo, which can contain account/token data.
            raise CapitalApiError(f'群益 COM API 錯誤 {integer(code)}')

    def connect(self, user_id, password, authority=0):
        self._check_owner()
        requested = (user_id, password, authority)
        if self._credentials is not None:
            if self._credentials != requested:
                raise CapitalApiError('群益已使用另一組帳號登入，請先中斷背景程序')
            if self.quote_ready:
                return ('connected', '報價主機已連線，略過重複登入', True)
            if self._quote_connection_state == 'connecting':
                return (self._status, '仍在等待報價主機就緒，略過重複登入',
                        self._announcement_received.is_set())
        self._connection_diagnostics.begin()
        try:
            self.start()
            self._connection_cleanup = False
            self._connection_events.clear()
            self._stocks_ready.clear()
            self._quote_events.connected.clear()
            self._subscribed_symbols.clear()
            if self._credentials is None:
                self._announcement_received.clear()
                self._connection_diagnostics.phase = 'login'
                self._assert_code(self._sdk_code('SKCenterLib_SetAuthority',
                    lambda: self._center.SKCenterLib_SetAuthority(authority)))
                self._assert_code(self._sdk_code('SKCenterLib_Login',
                    lambda: self._center.SKCenterLib_Login(user_id, password)))
                self._credentials = requested
            self._status = self._quote_connection_state = 'connecting'
            self._status_message = 'COM 已登入，等待國內報價商品就緒'
            self._connection_diagnostics.phase = 'opening_quote'
            self._assert_code(self._sdk_code('SKQuoteLib_EnterMonitorLONG',
                                            self._quote.SKQuoteLib_EnterMonitorLONG))
            if not self.quote_ready:
                self._connection_diagnostics.phase = 'waiting_quote_ready'
            self._restore_requested = bool(self._watchlist)
            return (self._status, self._status_message, self._announcement_received.is_set())
        except Exception:
            self._connection_diagnostics.phase = 'connect_failed'
            self._connection_diagnostics.record('connect_failed')
            raise

    def _on_com_connection(self, kind, code):
        with self._connection_diagnostics.lock:
            applied = not self._connection_cleanup
            self._connection_diagnostics.record('connection_event', channel='domestic_quote',
                source='SKQuoteLib', code=kind, error_code=code, applied=applied,
                during_cleanup=self._connection_cleanup)
            self._connection_events.append((kind, code))
            if not applied:
                return
            self._connection_diagnostics.last_quote_code = kind
            if kind == 3003 and code == 0:
                self._stocks_ready.set()
                self._status = self._quote_connection_state = 'connected'
                self._status_message = 'COM 國內報價主機與商品資料已就緒'
                self._connection_diagnostics.phase = 'quote_ready'
                self._restore_requested = bool(self._watchlist)
            elif kind in (3001, 3002, 3033) or code != 0:
                self._stocks_ready.clear()
                self._subscribed_symbols.clear()
                self._quote_connection_state = 'connecting' if kind == 3001 and code == 0 else 'disconnected'
                self._status = 'connecting' if self._quote_connection_state == 'connecting' else 'ready'
                self._connection_diagnostics.phase = 'waiting_quote_ready' if self._status == 'connecting' else 'quote_disconnected'
                self._status_message = f'COM 報價狀態 {kind}，附帶代碼 {code}'

    def poll(self):
        self._check_owner()
        if not self._apartment:
            return
        self._pythoncom.PumpWaitingMessages()
        if self.quote_ready and self._restore_requested:
            self._restore_requested = False  # One pass, not an unbounded retry loop.
            for symbol in list(self._watchlist):
                try:
                    self._subscribe(symbol)
                except Exception as error:
                    self._connection_diagnostics.record('subscription_error', exception_type=type(error).__name__)
        for _ in range(min(200, len(self._pending))):
            kind, data = self._pending.popleft()
            if self.quote_ready:
                try:
                    self._consume_market_event(kind, data)
                except Exception as error:
                    self._connection_diagnostics.record('market_event_error', exception_type=type(error).__name__)

    def _stock(self, market, index):
        stock, code = self._quote.SKQuoteLib_GetStockByIndexLONG(market, index, self._types.SKSTOCKLONG())
        self._assert_code(code)
        return stock

    def _consume_market_event(self, kind, data):
        stock = self._stock(data[0], data[1])
        symbol = str(stock.bstrStockNo)
        if kind == 'quote':
            self.cache.upsert_quote(Quote(symbol=symbol, name=str(stock.bstrStockName),
                price=self.cache.price(stock.nClose), open=self.cache.price(stock.nOpen),
                high=self.cache.price(stock.nHigh), low=self.cache.price(stock.nLow),
                prev_close=self.cache.price(stock.nRef), bid=self.cache.price(stock.nBid),
                bid_volume=stock.nBc, ask=self.cache.price(stock.nAsk), ask_volume=stock.nAc,
                volume=stock.nTQty, updated_at=self.cache.now()))
        elif kind == 'best5' and len(data) >= 24:
            values = data[2:]
            self.cache.upsert_best5(symbol, [(values[i], values[i+1]) for i in range(0, 10, 2)],
                                   [(values[i], values[i+1]) for i in range(12, 22, 2)])
        elif kind == 'tick':
            _, _, _, day, hms, micro, bid, ask, close, qty, _simulate = data
            timestamp = datetime.strptime(f'{day:08d}{hms:06d}', '%Y%m%d%H%M%S').replace(
                tzinfo=TAIPEI, microsecond=micro)
            self.cache.add_tick(symbol, Tick(timestamp=timestamp, price=self.cache.price(close),
                volume=qty, bid=self.cache.price(bid), ask=self.cache.price(ask)))

    def _subscribe(self, symbol):
        self._check_owner()
        require_history_connection(self._stocks_ready)
        if symbol in self._subscribed_symbols:
            return
        _, code = self._quote.SKQuoteLib_RequestStocks(self._watchlist[symbol], symbol)
        self._assert_code(code)
        _, code = self._quote.SKQuoteLib_RequestTicks(self._watchlist[symbol], symbol)
        self._assert_code(code)
        self._subscribed_symbols.add(symbol)

    def unsubscribe(self, symbol):
        self._check_owner()
        symbol = symbol.upper()
        if self.quote_ready and self.is_subscribed(symbol):
            self._assert_code(self._quote.SKQuoteLib_CancelRequestStocks(symbol))
            self._assert_code(self._quote.SKQuoteLib_CancelRequestTicks(symbol))
        self._watchlist.pop(symbol, None)
        self._watchlist_store.save(self._watchlist)
        self._subscribed_symbols.discard(symbol)
        return f'已取消訂閱 {symbol}'

    def fetch_historical_candles(self, symbol, interval, start_date, end_date, *, cache_result=True):
        self._check_owner()
        validate_history_range(interval, start_date, end_date)
        require_history_connection(self._stocks_ready)
        # Existing gap/exclusion/volume validation and bounded recovery, on this
        # SAME quote object. Never creates the legacy second COM/Login session.
        rows = request_history_candles(self._quote, self._center, self._quote_events,
                                      self.poll, symbol.upper(), interval, start_date, end_date,
                                      self._history_client.timeout_seconds)
        if cache_result:
            self.cache.set_historical_candles(symbol, interval, rows)
        return rows

    def _on_commodity_list(self, market, raw):
        if market != self._list_market:
            return
        if raw.startswith('##'):
            self._list_done.set()
            return
        try:
            parts = re.split(r'%(\d+)%([^%]*)%', raw)
            if len(parts) < 4 or parts[0].strip():
                raise ValueError('Unknown commodity format')
            for offset in range(1, len(parts), 3):
                category, name, body = int(parts[offset]), parts[offset+1].strip(), parts[offset+2]
                if category in EXCLUDED_STOCK_LIST_CATEGORY_NOS:
                    continue
                for record in body.strip().split(';'):
                    if not record.strip():
                        continue
                    fields = [s.strip() for s in record.split(',')]
                    if len(fields) < 4 or not fields[0] or not fields[1]:
                        raise ValueError('Incomplete commodity')
                    item = Instrument(market_no=market, category_no=category, category_name=name,
                        symbol=fields[0].upper(), name=fields[1], order_symbol=fields[3] or fields[0],
                        expiry_date=None if fields[2] in ('', '0') else fields[2])
                    self._list_rows[(category, item.symbol)] = item
                    if len(self._list_rows) > 30000:
                        raise ValueError('Commodity list exceeds safety limit')
        except Exception:
            self._list_error = CapitalApiError('COM 商品清單格式異常；不回傳部分清單供覆寫')
            self._list_done.set()

    def fetch_stock_list(self, market_no):
        self._check_owner()
        require_history_connection(self._stocks_ready)
        if market_no not in STOCK_LIST_MARKETS:
            raise CapitalApiError('不支援的商品清單市場')
        self._list_market = market_no
        self._list_rows.clear()
        self._list_error = None
        self._list_done.clear()
        try:
            self._assert_code(self._quote.SKQuoteLib_RequestStockList(market_no))
            deadline = time.monotonic() + self._history_client.timeout_seconds
            while not self._list_done.is_set() and time.monotonic() < deadline:
                self.poll()
                require_history_connection(self._stocks_ready)
                time.sleep(0.01)
            if self._list_error:
                raise self._list_error
            if not self._list_done.is_set() or not self._list_rows:
                raise CapitalApiError('COM 商品清單未完整回傳；保留既有商品資料')
            return list(self._list_rows.values())
        finally:
            self._list_market = None
            self._list_rows.clear()

    def refresh_quote_connection_state(self):
        self._check_owner()
        if self._quote is not None and not self._connection_cleanup:
            try:
                self._sdk_connection_value = integer(self._quote.SKQuoteLib_IsConnected())
                count, limit, code = self._quote.SKQuoteLib_GetQuoteStatus(0, False)
                self._quote_connection_count = integer(count)
                self._quote_out_limit = bool(limit)
                self._quote_status_code = integer(code)
            except Exception as error:
                self._connection_diagnostics.record('status_query_error', exception_type=type(error).__name__)
        return self._quote_connection_state  # Numeric status never replaces 3003.

    def mark_connection_timeout(self):
        self.refresh_quote_connection_state()
        return super().mark_connection_timeout()

    def stop(self):
        self._check_owner()
        if not self._apartment and not self._owns_process_lock:
            return
        self._connection_cleanup = True
        self._connection_diagnostics.phase = 'cleanup'
        self._connection_diagnostics.record('cleanup_start')
        self._stocks_ready.clear()
        self._subscribed_symbols.clear()
        self._restore_requested = False
        if self._quote_events is not None:
            self._quote_events.connected.clear()
            self._quote_events.accepting_records = False
        try:
            if self._quote is not None:
                try:
                    self._sdk_code('SKQuoteLib_LeaveMonitor', self._quote.SKQuoteLib_LeaveMonitor)
                except Exception:
                    pass
        finally:
            while self._handlers:
                handler = self._handlers.pop()
                try:
                    handler.disconnect()
                except Exception:
                    pass
                finally:
                    del handler  # Release connection-point refs before CoUninitialize.
            self._quote_events = self._reply_events = None
            self._center = self._quote = self._reply = self._types = None
            self._pending.clear()
            self._list_rows.clear()
            gc.collect()
            directory, self._directory = self._directory, None
            try:
                if directory is not None:
                    directory.close()
            except Exception as error:
                self._connection_diagnostics.record('cleanup_resource_error', resource='dll_directory',
                                                    exception_type=type(error).__name__)
            try:
                if self._apartment:
                    self._apartment = False
                    self._pythoncom.CoUninitialize()
            finally:
                try:
                    self._release_process_lock()
                finally:
                    self._started = False
                    self._status = 'ready'
                    self._quote_connection_state = 'disconnected'
                    self._connection_diagnostics.phase = 'stopped'
