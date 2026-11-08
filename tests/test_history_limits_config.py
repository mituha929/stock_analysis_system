"""Pure offline checks: configuration, inclusive bounds and period progress."""
import asyncio
from contextlib import redirect_stderr
from datetime import date, timedelta
import io
import os
import unittest
from unittest.mock import patch

from app.services.backfill_policy import history_window, period_start
from app.services.capital_history_service import (
    CapitalHistoryError, INTERVAL_OPTIONS, configured_history_limits,
    max_history_days, validate_history_range,
)


class HistoryLimitConfigTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            f"CAPITAL_KLINE_MAX_DAYS_{interval.upper()}": ""
            for interval in INTERVAL_OPTIONS
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_defaults_and_blank_values(self):
        self.assertEqual(configured_history_limits(), {
            "1m": 7, "5m": 7, "15m": 7, "60m": 7,
            "day": 400, "week": 400, "month": 400,
        })
        os.environ["CAPITAL_KLINE_MAX_DAYS_DAY"] = "  "
        self.assertEqual(max_history_days("day"), 400)

    def test_every_interval_is_independently_configurable(self):
        values = {"1m": 1, "5m": 3, "15m": 5, "60m": 14,
                  "day": 90, "week": 280, "month": 800}
        for interval, days in values.items():
            os.environ[f"CAPITAL_KLINE_MAX_DAYS_{interval.upper()}"] = str(days)
        self.assertEqual(configured_history_limits(), values)

    def test_configuration_is_not_frozen_at_module_import(self):
        os.environ["CAPITAL_KLINE_MAX_DAYS_DAY"] = "10"
        self.assertEqual(max_history_days("day"), 10)
        self.assertEqual(max_history_days("week"), 400)
        os.environ["CAPITAL_KLINE_MAX_DAYS_DAY"] = "20"
        self.assertEqual(max_history_days("day"), 20)

    def test_invalid_values_are_rejected(self):
        for raw in ("abc", "0", "-1", "1.5", "NaN", "inf"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"CAPITAL_KLINE_MAX_DAYS_DAY": raw}):
                with self.assertRaisesRegex(CapitalHistoryError, "CAPITAL_KLINE_MAX_DAYS_DAY"):
                    configured_history_limits()

    def test_aggregate_minimums(self):
        for interval, minimum in (("week", 7), ("month", 31)):
            key = f"CAPITAL_KLINE_MAX_DAYS_{interval.upper()}"
            with self.subTest(interval=interval), patch.dict(os.environ, {key: str(minimum - 1)}):
                with self.assertRaises(CapitalHistoryError):
                    configured_history_limits()
            with patch.dict(os.environ, {key: str(minimum)}):
                self.assertEqual(max_history_days(interval), minimum)

    def test_unknown_interval(self):
        with self.assertRaises(CapitalHistoryError):
            max_history_days("30m")

    def test_range_validation_uses_configured_inclusive_days(self):
        os.environ["CAPITAL_KLINE_MAX_DAYS_DAY"] = "10"
        validate_history_range("day", date(2026, 1, 1), date(2026, 1, 10))
        with self.assertRaisesRegex(CapitalHistoryError, "最多可查詢 10 天"):
            validate_history_range("day", date(2026, 1, 1), date(2026, 1, 11))

    def test_api_splitting_reads_the_same_configuration(self):
        from app.api.routes import history_windows

        os.environ["CAPITAL_KLINE_MAX_DAYS_DAY"] = "10"
        self.assertEqual(history_windows("day", date(2026, 1, 1), date(2026, 1, 21)), [
            (date(2026, 1, 1), date(2026, 1, 10)),
            (date(2026, 1, 11), date(2026, 1, 20)),
            (date(2026, 1, 21), date(2026, 1, 21)),
        ])

    def test_invalid_api_configuration_stops_before_capital_construction(self):
        from app.main import app, lifespan

        async def start():
            async with lifespan(app):
                self.fail("invalid settings unexpectedly started the API")

        with patch.dict(os.environ, {"CAPITAL_KLINE_MAX_DAYS_MONTH": "7"}), \
             patch("app.main.CapitalQuoteService") as capital:
            with self.assertRaises(CapitalHistoryError):
                asyncio.run(start())
            capital.assert_not_called()

    def test_invalid_cli_configuration_stops_before_mysql_or_login(self):
        from scripts import backfill_market_candles as cli

        with patch.dict(os.environ, {"CAPITAL_KLINE_MAX_DAYS_MONTH": "7"}), \
             patch.object(cli, "load_dotenv"), \
             patch("sys.argv", ["backfill", "--markets", "0", "--intervals", "day"]), \
             patch("app.services.backfill_control.BackfillControl"), \
             patch("app.services.incremental_candle_repository.MySQLCandleRepository") as repository, \
             patch.object(cli, "CapitalKLineSession") as session, \
             redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(), 2)
            self.assertIn("CAPITAL_KLINE_MAX_DAYS_MONTH", errors.getvalue())
            repository.assert_not_called()
            session.assert_not_called()

    def test_week_and_month_minimums_advance_in_both_directions(self):
        for interval, maximum in (("week", 7), ("month", 31)):
            for backward in (False, True):
                with self.subTest(interval=interval, backward=backward):
                    self.check_windows(interval, maximum, backward)

    def test_non_multiple_period_limits_also_preserve_periods(self):
        for interval, maximum in (("week", 10), ("month", 35), ("month", 62)):
            for backward in (False, True):
                with self.subTest(interval=interval, maximum=maximum, backward=backward):
                    self.check_windows(interval, maximum, backward)

    def check_windows(self, interval, maximum, backward):
        start = period_start(interval, date(2024, 1, 1))
        end = date(2024, 4, 26) if interval == "week" else date(2024, 4, 30)
        cursor = end if backward else start
        covered = set()
        for _ in range(200):
            if cursor < start or cursor > end:
                break
            first, last = history_window(interval, start, end, cursor, backward, maximum)
            self.assertLessEqual(first, last)
            self.assertLessEqual((last - first).days + 1, maximum)
            self.assertEqual(first, period_start(interval, first))
            if last != end:
                self.assertEqual(last + timedelta(days=1), period_start(interval, last + timedelta(days=1)))
            days = {first + timedelta(days=i) for i in range((last - first).days + 1)}
            self.assertFalse(covered & days)
            covered.update(days)
            cursor = first - timedelta(days=1) if backward else last + timedelta(days=1)
        else:
            self.fail("period windows did not make progress")
        self.assertEqual(covered, {start + timedelta(days=i) for i in range((end - start).days + 1)})

    def test_large_limit_is_clamped_before_date_arithmetic(self):
        for backward in (False, True):
            first, last = history_window("day", date(2026, 1, 1), date(2026, 2, 1),
                date(2026, 2, 1) if backward else date(2026, 1, 1), backward, 10**30)
            self.assertEqual((first, last), (date(2026, 1, 1), date(2026, 2, 1)))


if __name__ == "__main__":
    unittest.main()
