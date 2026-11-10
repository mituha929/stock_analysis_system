"""Recent, completed index candles from the application's Capital quote session."""
from datetime import timedelta
from decimal import Decimal
from time import monotonic

from app.services.capital_dll_quote_service import CapitalApiError
from app.services.capital_history_service import CapitalHistoryError, max_history_days
from app.services.daily_candle_aggregation import daily_aggregate_end, normalized_daily

INDEX_PRODUCTS = {
    "TWSE": ("發行量加權股價指數", "TSEA"),
    "TPEx": ("櫃買指數", "OTCA"),
}
PRICE_FIELDS = ("open", "high", "low", "close")
HISTORY_DAYS = 31
CACHE_SECONDS = 300
RETRY_SECONDS = 15


def index_rows(candles):
    rows = {}
    previous_close = None
    for bar in candles:
        prices = {field: Decimal(str(getattr(bar, field))) for field in PRICE_FIELDS}
        if prices["low"] <= 0:
            raise ValueError("指數點數無效")
        close = prices["close"]
        prices["change"] = close - previous_close if previous_close is not None else None
        previous_close = close
        period = bar.timestamp.date().isoformat()
        rows[period] = {
            "period": period,
            "values": {key: format(value, ".2f") if value is not None else None for key, value in prices.items()},
            "unit": dict.fromkeys(prices, "點"),
        }
    return rows


class IndexSnapshots:
    def __init__(self, capital):
        self.capital = capital
        self.snapshots = {}

    def read(self, market):
        end = daily_aggregate_end()
        cached_end, expires, rows, stale = self.snapshots.get(market, (None, 0, {}, True))
        if end == cached_end and monotonic() < expires:
            return rows, stale

        _, symbol = INDEX_PRODUCTS[market]
        try:
            start = end - timedelta(days=min(HISTORY_DAYS, max_history_days("day")) - 1)
            candles = self.capital.fetch_historical_candles(symbol, "day", start, end)
            refreshed = index_rows(normalized_daily(candles, start, end))
            if not refreshed or (rows and max(refreshed) < max(rows)):
                raise ValueError("群益未回傳最新的可用日 K")
            rows, stale = refreshed, False
        except (CapitalApiError, CapitalHistoryError, ValueError):
            stale = True
        lifetime = RETRY_SECONDS if stale else CACHE_SECONDS
        self.snapshots[market] = end, monotonic() + lifetime, rows, stale
        return rows, stale
