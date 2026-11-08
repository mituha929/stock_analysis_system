"""Spawn-safe native DLL host. Only this process ever loads Capital DLLs.

The parent owns MySQL/checkpoints and credentials are delivered over a private
pipe, never command-line arguments or a disk queue. The command whitelist has
no trading/order operations. No database is opened by this module.
"""
from __future__ import annotations

from collections import deque
import os
import time

from app.services.process_memory import current_process_memory
from app.services.quote_cache import QuoteCache


class MemoryWatchlistStore:
    def __init__(self, initial):
        self.value = dict(initial)

    def load(self):
        return dict(self.value)

    def save(self, value):
        self.value = dict(value)


class ForwardingCache(QuoteCache):
    """Bounded event tail, including the original vendor timestamps."""
    def __init__(self):
        super().__init__()
        self._dirty_quotes = {}
        self._dirty_best5 = {}
        self._dirty_ticks = {}

    def upsert_quote(self, quote):
        with self._lock:
            super().upsert_quote(quote)
            self._dirty_quotes[quote.symbol] = quote

    def upsert_best5(self, symbol, bids, asks):
        with self._lock:
            super().upsert_best5(symbol, bids, asks)
            self._dirty_best5[symbol] = self._best5[symbol]

    def add_tick(self, symbol, tick):
        with self._lock:
            super().add_tick(symbol, tick)
            self._dirty_ticks.setdefault(symbol, deque(maxlen=self._max_ticks)).append(tick)

    def drain(self):
        with self._lock:
            result = dict(quotes=list(self._dirty_quotes.values()), best5=list(self._dirty_best5.values()),
                          ticks={symbol:list(rows) for symbol,rows in self._dirty_ticks.items()})
            self._dirty_quotes.clear()
            self._dirty_best5.clear()
            self._dirty_ticks.clear()
            return result


def native_worker_main(pipe, config):
    """multiprocessing spawn entry point; import SDK only in the child."""
    from app.services.capital_dll_quote_service import CapitalQuoteService
    from app.services.capital_history_connection import CapitalHistoryError, is_history_connection_error
    from app.services.candle_validation import find_candle_data_error
    import pythoncom

    cache = ForwardingCache()
    backend = config.get('quote_backend', 'com')
    if backend == 'com':
        from app.services.capital_com_quote_service import CapitalComQuoteService
        service = CapitalComQuoteService(cache, dll_path=config.get('dll_path'),
                                        watchlist_store=MemoryWatchlistStore(config['watchlist']))
    elif backend == 'dll':
        service = CapitalQuoteService(cache, wrapper_path=config.get('wrapper_path'),
                                     watchlist_store=MemoryWatchlistStore(config['watchlist']))
    else:
        raise ValueError('Unknown quote backend')
    pump = getattr(service, 'poll', pythoncom.PumpWaitingMessages)
    service._history_client.timeout_seconds = config.get('history_timeout', 30)
    secrets = ()
    subscription_errors = {}

    def clean(message):
        for value in secrets:
            if value:
                message = message.replace(value, '[REDACTED]')
        return message

    def state():
        memory = current_process_memory()
        return dict(pid=os.getpid(), quote_backend=backend, status=service.status, status_message=clean(service.status_message),
                    quote_ready=service.quote_ready, quote_connection_state=service.quote_connection_state,
                    quote_connection_count=service.quote_connection_count, quote_out_limit=service.quote_out_limit,
                    quote_status_code=service.quote_status_code, connection_events=service.connection_events,
                    connection_diagnostics=service.connection_diagnostics,
                    subscribed=[symbol for symbol in service.watchlist() if service.is_subscribed(symbol)],
                    subscription_errors=dict(subscription_errors),
                    private_mb=memory.private_usage_mb, working_set_mb=memory.working_set_mb)

    try:
        pipe.send(dict(id=0, ok=True, state=state(), events=cache.drain()))
        while True:
            pump()
            if not pipe.poll(0.05):
                continue
            request = pipe.recv()
            ident, command, arguments = request['id'], request['command'], request.get('arguments', {})
            stopping = command == 'shutdown'
            try:
                if command == 'connect':
                    secrets = (arguments['user_id'], arguments['password'])
                    result = service.connect(**arguments)
                    deadline = time.monotonic() + config['connect_timeout']
                    while not service.quote_ready:
                        pump()
                        if service.quote_ready:
                            break
                        if time.monotonic() >= deadline:
                            if service.mark_connection_timeout():
                                raise TimeoutError('群益背景程序登入後，等待報價主機就緒逾時（未收到 SKQuote 的 3003；請查看 last_connect_failure）')
                            break
                        time.sleep(0.05)
                    # Idempotently restore all durable subscriptions. A failure
                    # is reported, not misrepresented as successful restoration.
                    for symbol, page in service._watchlist_store.load().items():
                        try:
                            service.subscribe(symbol, page)
                            subscription_errors.pop(symbol, None)
                        except Exception as error:
                            subscription_errors[symbol] = clean(str(error))
                    result = ('connected', service.status_message, bool(result[2]))
                elif command == 'snapshot':
                    service.refresh_quote_connection_state()
                    result = None
                elif command == 'subscribe':
                    result = service.subscribe(**arguments)
                    subscription_errors.pop(arguments['symbol'], None)
                elif command == 'unsubscribe':
                    result = service.unsubscribe(**arguments)
                    subscription_errors.pop(arguments['symbol'], None)
                elif command == 'history':
                    result = service.fetch_historical_candles(**arguments, cache_result=False)
                elif command == 'instruments':
                    result = service.fetch_stock_list(**arguments)
                elif stopping:
                    service.stop()
                    result = None
                else:
                    raise ValueError('不支援的群益背景程序命令')
                pipe.send(dict(id=ident, ok=True, result=result, state=state(), events=cache.drain()))
            except Exception as error:
                if command == 'subscribe':
                    subscription_errors[arguments['symbol']] = clean(str(error))
                data_error = find_candle_data_error(error)
                kind = 'data' if data_error is not None else 'connection' if is_history_connection_error(error) or isinstance(error, TimeoutError) else 'history' if isinstance(error, CapitalHistoryError) else 'api'
                pipe.send(dict(id=ident, ok=False, error=clean(str(error)), kind=kind,
                               data_error={key:clean(value) if isinstance(value,str) else value for key,value in data_error.details.items()} if data_error else None,
                               state=state(), events=cache.drain()))
            if stopping:
                break
    except (EOFError, BrokenPipeError, OSError):
        pass  # Parent exited; release only this worker's resources.
    finally:
        service.stop()
        pipe.close()
