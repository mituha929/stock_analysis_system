import unittest
from datetime import date, datetime

from app.services.mysql_instrument_repository import MySQLCandleRepository


class BackfillSelectionTests(unittest.TestCase):
    def test_missing_or_short_data_requires_a_refresh(self):
        self.assertTrue(MySQLCandleRepository._needs_candle_refresh(0, None, 360, date(2026, 8, 25)))
        self.assertTrue(MySQLCandleRepository._needs_candle_refresh(
            359, datetime(2026, 8, 25, 8), 360, date(2026, 8, 25),
        ))

    def test_complete_data_written_today_is_not_requeued(self):
        self.assertFalse(MySQLCandleRepository._needs_candle_refresh(
            360, datetime(2026, 8, 25, 8), 360, date(2026, 8, 25),
        ))

    def test_complete_data_from_an_older_day_requires_an_update(self):
        self.assertTrue(MySQLCandleRepository._needs_candle_refresh(
            360, datetime(2026, 8, 24, 20), 360, date(2026, 8, 25),
        ))


if __name__ == "__main__":
    unittest.main()
