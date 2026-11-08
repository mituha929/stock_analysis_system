import unittest
import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app.services.taiwan_trading_calendar import (
    TaiwanTradingCalendarError,
    refresh_official_holidays,
    latest_trading_day_on_or_before,
    trading_day_status,
)


class TaiwanTradingCalendarTests(unittest.TestCase):
    def test_weekend_is_not_a_trading_day(self):
        self.assertEqual(trading_day_status(date(2026, 8, 30)), (False, "週末"))

    def test_official_2026_closure_is_not_a_trading_day(self):
        self.assertEqual(trading_day_status(date(2026, 2, 16)), (False, "台股休市日"))

    def test_regular_weekday_is_a_trading_day(self):
        self.assertEqual(trading_day_status(date(2026, 8, 31)), (True, "交易日"))

    def test_sunday_uses_the_previous_friday_as_freshness_boundary(self):
        self.assertEqual(latest_trading_day_on_or_before(date(2026, 8, 30)), date(2026, 8, 28))

    def test_environment_can_add_future_closure(self):
        self.assertEqual(
            trading_day_status(date(2027, 1, 4), "2027-01-04"),
            (False, "台股休市日"),
        )

    def test_invalid_environment_date_is_rejected(self):
        with self.assertRaises(TaiwanTradingCalendarError):
            trading_day_status(date(2027, 1, 4), "not-a-date")

    @patch("app.services.taiwan_trading_calendar.urlopen")
    def test_official_calendar_uses_first_day_of_requested_year_and_filters_trading_rows(self, opener):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "stat": "ok",
            "data": [
                ["2027-01-01", "中華民國開國紀念日", "休市"],
                ["2027-01-04", "國曆新年開始交易日", "開始交易"],
                ["2027-02-05", "市場無交易，僅辦理結算交割作業", ""],
                ["2027-02-06", "農曆春節", "休市"],
            ],
        }, ensure_ascii=False).encode("utf-8")
        opener.return_value.__enter__.return_value = response
        with TemporaryDirectory() as folder:
            cache = Path(folder) / "holidays.json"
            downloaded, _message = refresh_official_holidays(
                date(2026, 12, 1), target_year=2027, cache_path=cache,
            )
            payload = json.loads(cache.read_text(encoding="utf-8"))

        self.assertTrue(downloaded)
        requested_url = opener.call_args.args[0].full_url
        self.assertIn("date=20270101", requested_url)
        self.assertEqual(payload["years"]["2027"], ["2027-01-01", "2027-02-05"])


if __name__ == "__main__":
    unittest.main()
