from datetime import date
import unittest

from app.services.capital_history_service import CapitalHistoryError, max_history_days, parse_kline_record, validate_history_range


class HistoricalKLineTests(unittest.TestCase):
    def test_parse_documented_new_kline_format(self):
        candle = parse_kline_record("2021/8/2, 12.95, 13.00, 12.80, 12.95, 291")
        self.assertEqual(candle.close, 12.95)
        self.assertEqual(candle.volume, 291)
        self.assertEqual(candle.timestamp.year, 2021)

    def test_parse_minute_kline_timestamp(self):
        candle = parse_kline_record("2026/8/14 09:30, 100, 102, 99, 101, 1234")
        self.assertEqual(candle.timestamp.hour, 9)
        self.assertEqual(candle.timestamp.minute, 30)

    def test_history_range_limits(self):
        self.assertEqual(max_history_days("day"), 400)
        validate_history_range("day", date(2025, 1, 1), date(2026, 2, 4))
        with self.assertRaises(CapitalHistoryError):
            validate_history_range("1m", date(2026, 1, 1), date(2026, 1, 8))
        with self.assertRaises(CapitalHistoryError):
            validate_history_range("day", date(2026, 2, 1), date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
