from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from app.schemas.models import Candle
from app.services.quote_cache import QuoteCache


def candles(count: int) -> list[Candle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(timestamp=base + timedelta(days=index), open=100, high=101, low=99, close=100, volume=1)
        for index in range(count)
    ]


class HistoricalQuoteCacheTests(unittest.TestCase):
    def test_historical_cache_keeps_only_newest_bars_and_lru_entries(self):
        cache = QuoteCache(max_historical_entries=2, max_historical_candles=3)
        cache.set_historical_candles("1111", "day", candles(5))
        cache.set_historical_candles("2222", "day", candles(2))
        # Touch 1111 so 2222 becomes the least recently used entry.
        self.assertEqual(len(cache.get_historical_candles("1111", "day")), 3)
        cache.set_historical_candles("3333", "day", candles(2))

        self.assertEqual(cache.get_historical_candles("2222", "day"), [])
        self.assertEqual(len(cache.get_historical_candles("1111", "day")), 3)
        self.assertEqual(len(cache.get_historical_candles("3333", "day")), 2)

    def test_historical_cache_entries_expire(self):
        cache = QuoteCache(historical_ttl_seconds=1)
        with patch("app.services.quote_cache.monotonic", return_value=10):
            cache.set_historical_candles("2330", "day", candles(2))
        with patch("app.services.quote_cache.monotonic", return_value=11):
            self.assertEqual(cache.get_historical_candles("2330", "day"), [])


if __name__ == "__main__":
    unittest.main()
