from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.routes import _parse_analysis_output_keys, analysis as analysis_route, historical_candles
from app.indicators.core import NormalizedCandle
from app.services.quote_cache import QuoteCache
from app.signals import analyze_technical


class AnalysisRouteContractTests(unittest.TestCase):
    """Verify the computation used by the temporary /analysis route.

    The FastAPI runtime is vendor/user-environment specific; this test keeps
    the route's data contract testable without a live Capital DLL connection.
    """

    def test_analysis_payload_is_json_ready_after_removing_internal_candles(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candles = [
            NormalizedCandle(start + timedelta(minutes=index), 100 + index, 101 + index, 99 + index, 100 + index, 1_000 + index)
            for index in range(40)
        ]
        result = analyze_technical(candles)
        indicators = {key: value for key, value in result["indicators"].items() if key != "candles"}
        self.assertNotIn("candles", indicators)
        self.assertIn("ma", indicators)
        self.assertIn("divergences", result)
        self.assertIn("direction_score", result["scores"])

    @patch("app.api.routes.MySQLCandleRepository")
    def test_mysql_default_includes_aligned_dates(self, repository_class):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candles = [
            NormalizedCandle(start + timedelta(days=index), 100 + index, 101 + index, 99 + index, 100 + index, 1_000 + index)
            for index in range(40)
        ]
        repository_class.return_value.get_candles_with_metadata.return_value = (candles, start)
        repository_class.return_value.get_instrument_name.return_value = "台積電"
        response = analysis_route("2330")
        self.assertEqual(response.source, "mysql")
        self.assertEqual(response.name, "台積電")
        self.assertEqual(response.previous_close, candles[-2].close)
        self.assertEqual(response.price_change, candles[-1].close - candles[-2].close)
        self.assertEqual(response.timestamps, [c.timestamp for c in candles])
        self.assertEqual([c.close for c in response.candles], [c.close for c in candles])
        self.assertEqual(response.latest_candle_at, candles[-1].timestamp)
        self.assertEqual(response.first_candle_at, candles[0].timestamp)
        self.assertEqual(response.latest_written_at, start)
        self.assertEqual(response.interval, "day")
        self.assertEqual(response.candle_count, 40)
        self.assertEqual(response.output_indicator_names["rsi"], "相對強弱指標（RSI）")
        self.assertEqual(response.output_indicator_names["stock_evaluation"], "股票評價")
        self.assertEqual(response.stock_evaluation["name"], "股票評價")
        self.assertIn(response.stock_evaluation["rating"], {"強勢偏多", "偏多", "中性／觀望", "偏空", "強勢偏空"})
        self.assertTrue(response.action_evaluation["available"])
        self.assertEqual(response.action_evaluation["scoring_version"], "v1")
        self.assertIn("action_score", response.action_evaluation)
        self.assertIn("warning_penalty", response.action_evaluation)

    @patch("app.api.routes.MySQLCandleRepository")
    def test_analysis_can_limit_output_to_selected_indicators(self, repository_class):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candles = [
            NormalizedCandle(start + timedelta(days=index), 100 + index, 101 + index, 99 + index, 100 + index, 1_000 + index)
            for index in range(40)
        ]
        repository_class.return_value.get_candles_with_metadata.return_value = (candles, None)
        repository_class.return_value.get_instrument_name.return_value = "台積電"

        response = analysis_route(
            "2330", output_keys="ma,rsi,stock_evaluation",
        )

        self.assertEqual(set(response.indicators), {"ma", "rsi"})
        self.assertEqual(set(response.output_indicator_names), {"ma", "rsi", "stock_evaluation"})
        self.assertEqual(response.signals, {})
        self.assertEqual(response.stock_evaluation["name"], "股票評價")

    @patch("app.api.routes.MySQLCandleRepository")
    def test_under_twenty_daily_candles_do_not_receive_a_stock_evaluation(self, repository_class):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candles = [
            NormalizedCandle(start + timedelta(days=index), 100 + index, 101 + index, 99 + index, 100 + index, 1_000 + index)
            for index in range(19)
        ]
        repository_class.return_value.get_candles_with_metadata.return_value = (candles, None)
        repository_class.return_value.get_instrument_name.return_value = "台積電"

        response = analysis_route("2330")

        self.assertEqual(response.scores, {})
        self.assertEqual(response.stock_evaluation["status"], "insufficient_data")
        self.assertFalse(response.stock_evaluation["available"])
        self.assertFalse(response.action_evaluation["available"])
        self.assertEqual(response.stock_evaluation["message"], "未滿20個交易日，無法提供評價")
        self.assertIn("未滿20個交易日，無法提供評價", response.warnings)
        self.assertIn("ma", response.indicators)

    def test_analysis_rejects_unknown_output_key(self):
        with self.assertRaises(HTTPException) as caught:
            _parse_analysis_output_keys("ma,unknown")
        self.assertEqual(caught.exception.status_code, 422)

    @patch("app.api.routes.MySQLCandleRepository")
    def test_mysql_candles_can_be_analyzed_without_cache_or_dll(self, repository_class):
        start = datetime(2026, 1, 1)
        candles = [
            NormalizedCandle(start + timedelta(days=index), 100 + index, 101 + index, 99 + index, 100 + index, 1_000 + index)
            for index in range(40)
        ]
        repository_class.return_value.get_candles_with_metadata.return_value = (candles, None)
        repository_class.return_value.get_instrument_name.return_value = "台積電"

        response = analysis_route(
            "2330", history_interval="day", candle_table="odd_lot_candle",
        )

        repository_class.return_value.get_candles_with_metadata.assert_called_once_with("2330", "day", "odd_lot_candle")
        self.assertEqual(response.candle_table, "odd_lot_candle")
        self.assertEqual(response.source, "mysql")
        self.assertEqual(response.interval, "day")
        self.assertEqual(response.candle_count, 40)

    def test_history_endpoint_rejects_an_oversized_date_range_before_calling_dll(self):
        with self.assertRaises(HTTPException) as caught:
            historical_candles(
                "2330", interval="1m", start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 8), capital=SimpleNamespace(),
            )
        self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
