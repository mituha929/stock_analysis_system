"""Single native-worker supervisor with post-commit, memory-based recycling.

The API/CLI retains database ownership. Returned backfill data pins the current
generation until history_checkpoint() acknowledges COMMIT or rollback. Thus
rotation cannot discard an uncommitted window or advance a MySQL cursor.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
import logging
import multiprocessing
import os
from pathlib import Path
import threading
import time

from app.services.capital_dll_quote_service import CapitalApiError
from app.services.capital_history_connection import CapitalHistoryConnectionError, CapitalHistoryError
from app.services.candle_validation import CandleDataError
from app.services.history_exclusions import allowed_history_ranges
from app.services.capital_worker_runtime import native_worker_main
from app.services.process_memory import process_memory_for_pid
from app.services.quote_cache import QuoteCache
from app.services.watchlist_store import WatchlistStore

logger = logging.getLogger(__name__)


def unavailable(message):
    result = CapitalApiError(message)
    result.__cause__ = CapitalHistoryConnectionError(message)
    return result


def positive_env(name, default):
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} 必須為正數') from None
    if not 0 < value < float('inf'):
        raise ValueError(f'{name} 必須為有限正數')
    return value


@dataclass(frozen=True)
class NativeWorkerConfig:
    recycle_mb: float = 3072
    connect_timeout: float = 30
    rpc_timeout: float = 180
    stop_timeout: float = 20
    poll_seconds: float = 1
    retry_seconds: float = 15
    history_timeout: float = 30
    quote_backend: str = 'com'

    def __post_init__(self):
        if self.quote_backend not in ('dll', 'com'):
            raise ValueError('CAPITAL_WORKER_QUOTE_BACKEND 僅支援 dll 或 com')

    @classmethod
    def from_env(cls):
        threshold = positive_env('CAPITAL_WORKER_RECYCLE_MB', 3072)
        stop = positive_env('API_MEMORY_STOP_BACKFILL_MB', 4096)
        if threshold >= stop:
            raise ValueError('CAPITAL_WORKER_RECYCLE_MB 必須低於 API_MEMORY_STOP_BACKFILL_MB，保留查詢尖峰餘裕')
        return cls(threshold, positive_env('CAPITAL_WORKER_CONNECT_TIMEOUT_SECONDS', 30),
                   positive_env('CAPITAL_WORKER_RPC_TIMEOUT_SECONDS', 180),
                   positive_env('CAPITAL_WORKER_STOP_TIMEOUT_SECONDS', 20),
                   positive_env('CAPITAL_WORKER_POLL_SECONDS', 1),
                   positive_env('CAPITAL_WORKER_RETRY_SECONDS', 15),
                   positive_env('CAPITAL_WORKER_HISTORY_TIMEOUT_SECONDS', 30),
                   os.getenv('CAPITAL_WORKER_QUOTE_BACKEND', 'com').strip().lower())


class CapitalWorkerService:
    def __init__(self, cache: QuoteCache, *, control=None, watchlist_store=None,
                 config=None, context=None, worker_target=None, start_monitor=True):
        self.cache = cache
        self.control = control
        self.config = config or NativeWorkerConfig.from_env()
        default_store = Path(__file__).resolve().parents[2] / 'data' / 'watchlist.json'
        self._store = watchlist_store or WatchlistStore(default_store)
        self._watchlist = self._store.load()
        self._context = context or multiprocessing.get_context('spawn')
        self._worker_target = worker_target or native_worker_main
        self._rpc_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._process = self._pipe = None
        self._request_id = 0
        self._credentials = None  # Only the credentials explicitly supplied to connect().
        self._authenticated = False
        self._want_connected = False
        self._closed = threading.Event()
        self._pending_windows = set()
        self._broken = False
        self._rotate_due = False
        self._phase = 'not_started'
        self._state = {}
        self._recycle_count = self._forced_exits = self._failures = 0
        self._retry_at = 0
        self._last_error = None
        self._last_connect_failure = None
        self._last_exit_code = None
        self._last_publish = 0
        self._monitor = None
        if start_monitor:
            self._monitor = threading.Thread(target=self._monitor_loop, name='capital-worker-supervisor', daemon=True)
            self._monitor.start()

    def _clean(self, message):
        if self._credentials:
            for value in self._credentials[:2]:
                if value:
                    message = message.replace(value, '[REDACTED]')
        return message

    @property
    def quote_ready(self):
        with self._state_lock:
            process = self._process
            return bool(self._want_connected and self._phase == 'connected' and
                        process is not None and process.is_alive() and self._state.get('quote_ready'))

    @property
    def status(self):
        return 'connected' if self.quote_ready else 'waiting_connection' if self._phase == 'connected' else self._phase

    @property
    def status_message(self):
        if self._last_error:
            return self._last_error
        if self._phase == 'recycling':
            return '正在輪替群益背景程序；API 與已儲存資料仍可讀取'
        return self._state.get('status_message') or '尚未連線群益背景程序'

    @property
    def quote_connection_state(self):
        return self._state.get('quote_connection_state', 'not_started') if self._phase == 'connected' else self._phase

    @property
    def quote_connection_count(self): return self._state.get('quote_connection_count')
    @property
    def quote_out_limit(self): return self._state.get('quote_out_limit')
    @property
    def quote_status_code(self): return self._state.get('quote_status_code')
    @property
    def connection_events(self): return list(self._state.get('connection_events', []))

    @property
    def connection_diagnostics(self):
        with self._state_lock:
            return deepcopy(self._state.get('connection_diagnostics') or {})

    def refresh_quote_connection_state(self):
        return self.quote_connection_state

    def watchlist(self):
        with self._state_lock:
            return list(self._watchlist)

    def is_subscribed(self, symbol):
        return self.quote_ready and symbol.upper() in self._state.get('subscribed', [])

    def worker_snapshot(self):
        with self._state_lock:
            return dict(enabled=True, api_pid=os.getpid(), pid=self._state.get('pid'), phase=self._phase,
                        quote_backend=self.config.quote_backend,
                        private_mb=self._state.get('private_mb'), working_set_mb=self._state.get('working_set_mb'),
                        recycle_mb=self.config.recycle_mb, recycle_due=self._rotate_due,
                        pending_windows=len(self._pending_windows), recycle_count=self._recycle_count,
                        forced_exits=self._forced_exits, last_exit_code=self._last_exit_code,
                        subscription_errors=dict(self._state.get('subscription_errors', {})),
                        last_error=self._last_error, retry_in_seconds=max(0, round(self._retry_at-time.monotonic(), 1)),
                        connection_diagnostics=self.connection_diagnostics,
                        last_connect_failure=deepcopy(self._last_connect_failure),
                        observed_at=datetime.now(timezone.utc).isoformat())

    def _publish(self, force=False):
        if self.control is not None and (force or time.monotonic()-self._last_publish >= 2):
            try:
                self.control.update(native_worker=self.worker_snapshot())
            except Exception:
                logger.warning('無法保存群益背景程序遙測；原資料斷點不受影響')
            self._last_publish = time.monotonic()

    def _accept(self, response):
        with self._state_lock:
            self._state.update(response.get('state', {}))
            value = self._state.get('private_mb')
            if value is not None and value >= self.config.recycle_mb:
                self._rotate_due = True
        self.cache.apply_native_events(response.get('events', {}))

    def _exchange_locked(self, command, arguments=None, timeout=None):
        if self._process is None or self._pipe is None or not self._process.is_alive() or self._broken:
            raise unavailable('群益背景程序未執行；工作斷點保留等待恢復')
        self._request_id += 1
        ident = self._request_id
        try:
            self._pipe.send(dict(id=ident, command=command, arguments=arguments or {}))
            deadline = time.monotonic() + (timeout or self.config.rpc_timeout)
            while not self._pipe.poll(min(0.2, max(0, deadline-time.monotonic()))):
                if not self._process.is_alive() or time.monotonic() >= deadline:
                    raise TimeoutError
            response = self._pipe.recv()
            if response.get('id') != ident:
                raise ValueError('IPC response ID mismatch')
        except (OSError, EOFError, TimeoutError, ValueError):
            self._broken = True
            self._phase = 'waiting_connection'
            self._state['quote_ready'] = False
            raise unavailable('群益背景程序回應中斷或逾時；未確認區間不推進斷點')
        self._accept(response)
        if not response.get('ok'):
            message = self._clean(response.get('error') or '群益背景程序呼叫失敗')
            if response.get('kind') == 'data':
                raise CandleDataError(message, details=response.get('data_error'))
            if response.get('kind') == 'connection':
                raise unavailable(message)
            if response.get('kind') == 'history':
                raise CapitalHistoryError(message)
            raise CapitalApiError(message)
        return response.get('result')

    def _launch_locked(self):
        if self._closed.is_set() or not self._want_connected or self._credentials is None:
            raise unavailable('尚未取得使用者登入授權，不能自動登入群益')
        self._shutdown_locked()
        self._phase = 'connecting'
        with self._state_lock:
            self._last_error = None
            self._last_connect_failure = None
            self._state.update(status_message='正在啟動群益背景程序並連線',
                               connection_events=[],connection_diagnostics={})
        self.cache.reset_native_generation()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(target=self._worker_target, args=(child, dict(
            watchlist=dict(self._watchlist), connect_timeout=self.config.connect_timeout,
            history_timeout=self.config.history_timeout, quote_backend=self.config.quote_backend)), name='capital-native-worker', daemon=True)
        self._process, self._pipe = process, parent
        self._broken = False
        try:
            process.start()
            child.close()
            if not parent.poll(self.config.connect_timeout):
                raise unavailable('群益背景程序啟動逾時')
            response = parent.recv()
            if response.get('id') != 0 or response.get('state', {}).get('pid') != process.pid:
                raise unavailable('群益背景程序啟動識別不符')
            self._accept(response)
            user_id, password, authority = self._credentials
            result = self._exchange_locked('connect', dict(user_id=user_id,password=password,authority=authority),
                                           timeout=self.config.connect_timeout+15)
            if not self._state.get('quote_ready'):
                raise unavailable('群益背景程序尚未完成報價主機連線')
            if (self._state.get('private_mb') or 0) >= self.config.recycle_mb:
                raise unavailable('新群益背景程序登入後即達輪替門檻；暫停自動重試，請檢查設定或記憶體')
            self._phase = 'connected'
            self._authenticated = True
            self._last_error = None
            self._failures = 0
            self._retry_at = 0
            self._rotate_due = False
            self._publish(True)
            return result
        except Exception as error:
            # Preserve the failed CONNECT response BEFORE shutdown callbacks
            # overwrite live state. Cleanup is not the original failure cause.
            with self._state_lock:
                self._last_connect_failure = dict(
                    at=datetime.now(timezone.utc).isoformat(),pid=self._state.get('pid'),
                    message=self._clean(str(error)),quote_ready=self._state.get('quote_ready',False),
                    connection_events=deepcopy(self._state.get('connection_events',[])),
                    connection_diagnostics=self.connection_diagnostics)
            child.close()
            self._shutdown_locked()
            raise

    def _shutdown_locked(self):
        process, pipe = self._process, self._pipe
        if process is None:
            return
        try:
            if process.pid is not None and process.is_alive():
                try:
                    self._exchange_locked('shutdown', timeout=self.config.stop_timeout)
                except Exception:
                    pass
                process.join(self.config.stop_timeout)
                if process.is_alive():
                    # This supervisor can terminate ONLY its own spawned child,
                    # and a replacement cannot start until exit is confirmed.
                    process.terminate()
                    self._forced_exits += 1
                    process.join(5)
                if process.is_alive():
                    raise unavailable('舊群益背景程序尚未退出；拒絕建立第二個 DLL 程序')
            if process.pid is not None:
                process.join(0)
                self._last_exit_code = process.exitcode
        finally:
            if process.pid is None or not process.is_alive():
                if pipe is not None:
                    pipe.close()
                self._process = self._pipe = None
                self._state['quote_ready'] = False
                self._state['pid'] = None

    def _failed(self, error):
        self._last_error = self._clean(str(error))
        self._phase = 'waiting_connection'
        self._state['quote_ready'] = False
        self._failures += 1
        self._retry_at = time.monotonic() + min(300, self.config.retry_seconds * 2**min(5, self._failures-1))
        self._publish(True)

    def connect(self, user_id, password, authority=0):
        with self._rpc_lock:
            if self._closed.is_set():
                raise CapitalApiError('群益背景服務已停止')
            requested = (user_id, password, authority)
            if self._credentials is not None and self._credentials != requested:
                raise CapitalApiError('群益已使用另一組帳號登入；請先中斷連線，再使用新帳號')
            self._credentials = requested
            self._want_connected = True
            if self.quote_ready:
                return ('connected', '報價主機已連線，略過重複登入', True)
            try:
                return self._launch_locked()
            except Exception as error:
                self._failed(error)
                if not self._authenticated:
                    # An unaccepted password must not be retried indefinitely.
                    # Known-good credentials remain in memory across recycling.
                    self._want_connected = False
                    self._credentials = None
                raise

    def _ensure_locked(self):
        if not self._want_connected or self._credentials is None:
            raise unavailable('請先登入群益，才能抓取資料')
        if self.quote_ready and not self._broken:
            return
        if self._pending_windows:
            raise unavailable('正在等待已接收區間完成提交，暫不重建群益背景程序')
        if time.monotonic() < self._retry_at:
            raise unavailable('群益背景程序等待重試，斷點保留；請稍候')
        try:
            self._launch_locked()
        except Exception as error:
            self._failed(error)
            raise

    def _maybe_recycle_locked(self):
        if not self._rotate_due or self._pending_windows or not self._want_connected:
            return False
        self._phase = 'recycling'
        self._publish(True)
        try:
            self._launch_locked()
            self._recycle_count += 1
            self._publish(True)
            return True
        except Exception as error:
            self._failed(error)
            raise

    def fetch_historical_candles(self, symbol, interval, start_date, end_date, *, cache_result=True):
        with self._rpc_lock:
            self._ensure_locked()
            owner = threading.get_ident()
            if owner in self._pending_windows:
                raise CapitalApiError('前一個 K 線區間尚未確認提交，拒絕覆寫其輪替保護')
            if self._rotate_due and self._pending_windows:
                raise unavailable('群益程序已達輪替門檻；等待現有區間提交後再接受新查詢')
            self._maybe_recycle_locked()
            if not cache_result:
                self._pending_windows.add(owner)
            try:
                result = self._exchange_locked('history', dict(symbol=symbol, interval=interval,
                    start_date=start_date, end_date=end_date),
                    timeout=self.config.rpc_timeout * max(1, len(allowed_history_ranges(symbol, interval, start_date, end_date))))
            except BaseException:
                self._pending_windows.discard(owner)
                raise
            if cache_result:
                self.cache.set_historical_candles(symbol, interval, result)
            self._publish()
            return result

    def history_checkpoint(self, committed):
        with self._rpc_lock:
            self._pending_windows.discard(threading.get_ident())
            if committed:
                self._maybe_recycle_locked()

    def fetch_stock_list(self, market_no):
        with self._rpc_lock:
            self._ensure_locked()
            if self._rotate_due and self._pending_windows:
                raise unavailable('群益程序已達輪替門檻；等待現有區間提交後再查詢商品')
            self._maybe_recycle_locked()
            return self._exchange_locked('instruments', dict(market_no=market_no))

    def subscribe(self, symbol, page_no):
        with self._rpc_lock:
            symbol = symbol.upper()
            self._watchlist[symbol] = page_no
            self._store.save(self._watchlist)
            if not self._want_connected:
                return '已加入自選股；登入群益後會自動訂閱'
            self._ensure_locked()
            return self._exchange_locked('subscribe', dict(symbol=symbol,page_no=page_no))

    def unsubscribe(self, symbol):
        with self._rpc_lock:
            symbol = symbol.upper()
            self._watchlist.pop(symbol, None)
            self._store.save(self._watchlist)
            if self.quote_ready:
                return self._exchange_locked('unsubscribe', dict(symbol=symbol))
            return f'已取消訂閱 {symbol}'

    def disconnect(self):
        self._want_connected = False
        with self._rpc_lock:
            self._shutdown_locked()
            self._credentials = None
            self._authenticated = False
            self._rotate_due = False
            self._last_error = None
            self._phase = 'disconnected'
            self._publish(True)
            return '已結束群益背景程序並清除記憶體中的登入資料；再次連線需要登入'

    def stop(self):
        self._closed.set()
        self._want_connected = False
        with self._rpc_lock:
            self._shutdown_locked()
            self._credentials = None
            self._phase = 'stopped'
            self._publish(True)
        if self._monitor and self._monitor is not threading.current_thread():
            self._monitor.join(2)

    def __enter__(self): return self
    def __exit__(self, *_errors): self.stop()

    def _monitor_loop(self):
        while not self._closed.wait(self.config.poll_seconds):
            # External memory observation works even while a native RPC is
            # blocked. It never kills/restarts an unacknowledged query/window.
            with self._state_lock:
                pid = self._state.get('pid')
            if pid:
                point = process_memory_for_pid(pid)
                if point.private_usage_mb is not None:
                    with self._state_lock:
                        self._state.update(private_mb=point.private_usage_mb,working_set_mb=point.working_set_mb)
                        self._rotate_due |= point.private_usage_mb >= self.config.recycle_mb
            if not self._rpc_lock.acquire(blocking=False):
                continue
            try:
                if not self._want_connected or self._credentials is None:
                    continue
                self._ensure_locked()
                self._exchange_locked('snapshot', timeout=5)
                self._maybe_recycle_locked()
                self._publish()
            except Exception as error:
                # Do not reset the timer on each readiness poll.
                if time.monotonic() >= self._retry_at:
                    self._failed(error)
            finally:
                self._rpc_lock.release()
