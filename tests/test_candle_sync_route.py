from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from app.api.routes import sync_historical_candles
from app.schemas.models import Candle


class HistoricalCandleSyncRouteTests(unittest.TestCase):
    def test_fetches_and_upserts_candles(self):
        candles = [
            Candle(
                timestamp=datetime(2026, 8, 20, tzinfo=timezone.utc),
                open=100, high=102, low=99, close=101, volume=1_000,
            )
        ]
        capital = SimpleNamespace(
            fetch_historical_candles=lambda symbol, interval, start, end, **options: candles,
        )
        with patch("app.api.routes.MySQLCandleRepository") as repository_type:
            repository_type.return_value.upsert_candles.return_value = 1
            response = sync_historical_candles(
                "2330", interval="day", table_name="candles", start_date=date(2026, 8, 20),
                end_date=date(2026, 8, 20), capital=capital,
            )
        self.assertEqual(response.symbol, "2330")
        self.assertEqual(response.fetched_count, 1)
        self.assertEqual(response.upserted_count, 1)
        self.assertEqual(response.table_name, "candles")
        repository_type.return_value.upsert_candles.assert_called_once_with("2330", "day", candles, "candles")


if __name__ == "__main__":
    unittest.main()
