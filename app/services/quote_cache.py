from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic

from app.schemas.models import Best5, Candle, PriceLevel, Quote, Tick


class QuoteCache:
    """Thread-safe store written by COM callbacks and read by FastAPI."""

    def __init__(
        self, max_ticks_per_symbol: int = 500, max_historical_entries: int = 20,
        max_historical_candles: int = 1_200, historical_ttl_seconds: float = 300,
    ) -> None:
        if max_historical_entries < 1 or max_historical_candles < 2 or historical_ttl_seconds <= 0:
            raise ValueError("歷史 K 線快取限制必須為正值")
        self._lock = RLock()
        self._quotes: dict[str, Quote] = {}
        self._best5: dict[str, Best5] = {}
        self._ticks: dict[str, deque[Tick]] = {}
        # Interactive historical data is useful briefly for /analysis, but a
        # market-wide backfill must never turn the API process into a second
        # database.  This is a bounded LRU cache with a short TTL.
        self._historical_candles: OrderedDict[tuple[str, str], tuple[float, list[Candle]]] = OrderedDict()
        self._max_ticks = max_ticks_per_symbol
        self._max_historical_entries = max_historical_entries
        self._max_historical_candles = max_historical_candles
        self._historical_ttl_seconds = historical_ttl_seconds

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def upsert_quote(self, quote: Quote) -> None:
        with self._lock:
            self._quotes[quote.symbol] = quote

    def get_quote(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(symbol)

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        with self._lock:
            return [self._quotes[symbol] for symbol in symbols if symbol in self._quotes]

    def upsert_best5(self, symbol: str, bids: list[tuple[int, int]], asks: list[tuple[int, int]]) -> None:
        best5 = Best5(
            symbol=symbol,
            bids=[PriceLevel(price=self.price(value), volume=qty) for value, qty in bids],
            asks=[PriceLevel(price=self.price(value), volume=qty) for value, qty in asks],
            updated_at=self.now(),
        )
        with self._lock:
            self._best5[symbol] = best5

    def get_best5(self, symbol: str) -> Best5 | None:
        with self._lock:
            return self._best5.get(symbol)

    def add_tick(self, symbol: str, tick: Tick) -> None:
        with self._lock:
            self._ticks.setdefault(symbol, deque(maxlen=self._max_ticks)).append(tick)

    def get_ticks(self, symbol: str, limit: int | None = None) -> list[Tick]:
        with self._lock:
            ticks = list(self._ticks.get(symbol, ()))
        return ticks[-limit:] if limit is not None else ticks

    def apply_native_events(self, events: dict) -> None:
        """Import a bounded worker event batch without altering timestamps."""
        with self._lock:
            for quote in events.get("quotes", ()):
                self._quotes[quote.symbol] = quote
            for best5 in events.get("best5", ()):
                self._best5[best5.symbol] = best5
            for symbol, ticks in events.get("ticks", {}).items():
                self._ticks.setdefault(symbol, deque(maxlen=self._max_ticks)).extend(ticks)

    def reset_native_generation(self) -> None:
        """A reconnect may replay ticks; do not combine two native generations."""
        with self._lock:
            self._quotes.clear()
            self._best5.clear()
            self._ticks.clear()

    def get_candles(self, symbol: str, interval_minutes: int) -> list[Candle]:
        """Build intraday OHLCV candles from the retained real-time ticks."""
        ticks = self.get_ticks(symbol)
        buckets: dict[datetime, list[Tick]] = {}
        for tick in ticks:
            if tick.price is None:
                continue
            local = tick.timestamp.astimezone(timezone(timedelta(hours=8)))
            minute = local.minute - (local.minute % interval_minutes)
            bucket = local.replace(minute=minute, second=0, microsecond=0)
            buckets.setdefault(bucket, []).append(tick)
        return [
            Candle(
                timestamp=key,
                open=items[0].price,  # price is checked above
                high=max(item.price for item in items if item.price is not None),
                low=min(item.price for item in items if item.price is not None),
                close=items[-1].price,
                volume=sum(item.volume for item in items),
            )
            for key, items in sorted(buckets.items())
        ]

    def set_historical_candles(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        key = (symbol.upper(), interval)
        # Retain the latest bars: these are the only bars relevant to the
        # current technical-analysis endpoint.
        bounded = list(candles[-self._max_historical_candles:])
        with self._lock:
            self.prune_historical()
            self._historical_candles.pop(key, None)
            self._historical_candles[key] = (monotonic() + self._historical_ttl_seconds, bounded)
            while len(self._historical_candles) > self._max_historical_entries:
                self._historical_candles.popitem(last=False)

    def get_historical_candles(self, symbol: str, interval: str) -> list[Candle]:
        key = (symbol.upper(), interval)
        with self._lock:
            item = self._historical_candles.get(key)
            if item is None:
                return []
            expires_at, candles = item
            if monotonic() >= expires_at:
                del self._historical_candles[key]
                return []
            self._historical_candles.move_to_end(key)
            return list(candles)

    def prune_historical(self) -> int:
        """Called by the maintenance loop even when entries are never read."""
        with self._lock:
            now = monotonic()
            expired = [key for key, (expires, _rows) in self._historical_candles.items() if expires <= now]
            for key in expired:
                del self._historical_candles[key]
            return len(expired)

    @staticmethod
    def price(value: int) -> float | None:
        # 群益以整數價格 * 100 回傳；0 和市價特殊值不當成一般報價。
        return value / 100.0 if value > 0 else None
