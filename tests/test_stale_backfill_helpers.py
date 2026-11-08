import unittest
from datetime import date, datetime
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, Response

from app.api.routes import history_windows, parse_refresh_intervals, parse_refresh_markets, refresh_stale_backfill


class StaleBackfillHelperTests(unittest.TestCase):
    def test_all_intervals_and_explicit_interval_selection(self):
        self.assertIn("day", parse_refresh_intervals("all"))
        self.assertEqual(parse_refresh_intervals("1m,day,1m"), ["day", "1m"])

    def test_all_markets_and_explicit_market_selection(self):
        self.assertEqual(parse_refresh_markets("all"), [0, 1, 4, 5, 6])
        self.assertEqual(parse_refresh_markets("1,4,1"), [1, 4])

    def test_rejects_unknown_interval(self):
        with self.assertRaises(HTTPException):
            parse_refresh_intervals("day,2m")

    def test_long_range_is_split_into_vendor_safe_windows(self):
        windows = history_windows("1m", date(2026, 8, 1), date(2026, 8, 15))
        self.assertEqual(windows, [
            (date(2026, 8, 1), date(2026, 8, 7)),
            (date(2026, 8, 8), date(2026, 8, 14)),
            (date(2026, 8, 15), date(2026, 8, 15)),
        ])

    @patch("app.api.routes.MySQLCandleRepository")
    def test_holiday_does_not_create_or_resume_work(self,repository_class):
        from app.api.routes import _plan_stale_backfill
        plans,work,_latest,reason=_plan_stale_backfill([0],["day"],1200,True,today=date(2026,10,9))
        self.assertEqual(plans,[])
        self.assertEqual(work,[])
        repository_class.assert_not_called()
        self.assertEqual(reason,"台股休市日")

    @patch("app.api.routes.BackfillControl")
    @patch("app.api.routes.MySQLCandleRepository")
    def test_queues_each_market_using_actual_candle_policy(self,repository_class,control):
        from app.api.routes import _plan_stale_backfill
        control.return_value.worker_active.return_value=False
        repository_class.return_value.plan_incremental.return_value={
            "start_date":date(2026,10,12),"queued_jobs":2,
            "initialization_jobs":1,"incremental_jobs":1,
        }
        plans,work,_latest,_reason=_plan_stale_backfill([0,1],["day"],1200,True,today=date(2026,10,14))
        self.assertEqual(len(work),2)
        self.assertEqual(plans[0].action,"planning_at_group_start")
        self.assertIsNone(plans[0].start_date)
        self.assertEqual(work[0]["as_of_date"],date(2026,10,14))
        repository_class.assert_not_called()
        repository_class.return_value.get_oldest_backfill_updates.assert_not_called()

    @patch("app.api.routes.BackfillControl")
    @patch("app.api.routes.MySQLCandleRepository")
    def test_resume_zero_forces_only_current_missing_ranges(self,repository_class,control):
        from app.api.routes import _plan_stale_backfill
        control.return_value.worker_active.return_value=False
        repository_class.return_value.plan_incremental.return_value={
            "start_date":date(2026,10,12),"queued_jobs":2,
            "initialization_jobs":0,"incremental_jobs":2,
        }
        _,work,_,_ = _plan_stale_backfill([0],["day"],1200,False,today=date(2026,10,14))
        self.assertTrue(work[0]["force"])
        repository_class.assert_not_called()

if __name__ == "__main__":
    unittest.main()
