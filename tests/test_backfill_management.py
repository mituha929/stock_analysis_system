import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, Response

from app.api.routes import backfill_status, retry_failed_backfill, run_scheduled_backfill
from app.services.capital_dll_quote_service import CapitalApiError
from app.services.backfill_scheduler import DailyBackfillConfig, DailyBackfillScheduler


def scheduler_snapshot():
    return {
        "enabled": False, "state": "disabled", "timezone": "Asia/Taipei",
        "local_time": "18:30", "markets": [0, 1, 4, 5, 6], "intervals": ["day"],
        "target_bars": 1200, "target_bars_by_interval": {"day": 1200},
        "resume": True, "next_run_at": None,
        "last_started_at": None, "last_finished_at": None, "last_message": None,
    }


class BackfillManagementTests(unittest.TestCase):
    def setUp(self):
        day=patch("app.api.routes.trading_day_status",return_value=(True,"交易日"))
        day.start(); self.addCleanup(day.stop)

    @patch("app.api.routes.MySQLCandleRepository")
    def test_status_counts_latest_jobs_and_exposes_schedule(self, repository_class):
        repository_class.return_value.get_backfill_dashboard_status.return_value = {
            "storage_ready": True,
            "items": {(0, "day"): {
                "storage_ready": True, "active_instruments": 10, "tracked_symbols": 9,
                "job_counts": {"pending": 1, "running": 0, "completed": 7, "failed": 1, "insufficient_history": 0},
                "data_through": date(2026, 9, 21),
                "oldest_successful_update_at": datetime(2026, 9, 21, 18, 0),
                "latest_successful_update_at": datetime(2026, 9, 21, 18, 5),
                "latest_status_at": datetime(2026, 9, 21, 18, 6),
                "latest_failure_symbol": "2330", "latest_failure_error": "timeout",
                "latest_failure_at": datetime(2026, 9, 21, 18, 6),
            }},
        }
        repository_class.return_value.list_daily_backfill_runs.return_value = []
        scheduler = type("Scheduler", (), {"snapshot": staticmethod(scheduler_snapshot)})()
        request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {"backfill_scheduler": scheduler})()})()})()

        result = backfill_status(request=request, markets="0", intervals="day")
        request.app.state.backfill_status_cache.pending.join()
        result = backfill_status(request=request, markets="0", intervals="day")

        self.assertEqual(result.items[0].coverage_percent, 90.0)
        self.assertEqual(result.items[0].job_counts.failed, 1)
        self.assertFalse(result.schedule.enabled)

    @patch("app.api.routes.MySQLCandleRepository")
    def test_retry_requeues_only_repository_selected_failures(self, repository_class):
        repository = repository_class.return_value
        repository.ensure_backfill_storage.return_value = ("candles", "candle_backfill_jobs")
        repository.failed_symbols.return_value = ["2330","2317"]
        repository.plan_incremental.return_value = dict(start_date=date(2026,9,20),queued_jobs=2,
            initialization_jobs=0,incremental_jobs=2)
        background = BackgroundTasks()
        response = Response()
        capital = type("Capital", (), {"quote_ready": True})()

        result = retry_failed_backfill(
            background_tasks=background, response=response, markets="0", intervals="day",
            target_bars=1200, capital=capital,
        )

        self.assertEqual(result.queued_jobs, 2)
        self.assertEqual(len(background.tasks), 1)
        repository.failed_symbols.assert_called_once_with(0,"day","candle_backfill_jobs","instruments")
        self.assertEqual(repository.plan_incremental.call_args.kwargs["only_symbols"],["2330","2317"])

    def test_daily_schedule_defaults_to_disabled(self):
        with patch.dict("os.environ", {}, clear=True):
            config = DailyBackfillConfig.from_env()
        scheduler = DailyBackfillScheduler(config, lambda _config: ("completed", "ok"))
        self.assertFalse(config.enabled)
        self.assertEqual(config.target_bars_by_interval["week"], 560)
        self.assertEqual(config.target_bars_by_interval["month"], 100)
        self.assertEqual(scheduler.snapshot()["state"], "disabled")

    def test_schedule_checkpoint_catches_up_week_and_month_after_restart(self):
        with TemporaryDirectory() as folder, patch.dict("os.environ", {}, clear=True):
            config = replace(
                DailyBackfillConfig.from_env(),
                enabled=True,
                state_file=Path(folder) / "scheduler-state.json",
                aggregate_week_month_from_day=False,
            )
            scheduler = DailyBackfillScheduler(config, lambda _config: ("completed", "ok"))
            intervals, reason, periods = scheduler._select_intervals(date(2026, 9, 23))
            self.assertEqual(intervals, ("day", "1m", "5m", "15m", "60m", "week", "month"))
            self.assertIn("補跑已完成交易週", reason)
            self.assertIn("補跑已完成交易月", reason)

            scheduler._save_checkpoint(periods)
            restarted = DailyBackfillScheduler(config, lambda _config: ("completed", "ok"))
            intervals, _reason, periods = restarted._select_intervals(date(2026, 9, 23))
            self.assertEqual(intervals, ("day", "1m", "5m", "15m", "60m"))
            self.assertEqual(periods, {})

            intervals, reason, periods = restarted._select_intervals(date(2026, 9, 24))
            self.assertIn("week", intervals)
            self.assertNotIn("month", intervals)
            self.assertIn("本週最後交易日", reason)
            self.assertEqual(periods["last_successful_week"], "2026-W39")

    @patch("app.api.routes._plan_stale_backfill")
    @patch("app.api.routes._sync_instrument_markets_unlocked")
    @patch("app.api.routes.MySQLCandleRepository")
    def test_daily_schedule_syncs_both_instrument_groups_before_kline_planning(self, repository_class, sync, plan):
        with patch.dict("os.environ", {}, clear=True):
            config = replace(DailyBackfillConfig.from_env(), enabled=True, intervals=("day",))
        sync.side_effect = [
            [SimpleNamespace(fetched_count=10), SimpleNamespace(fetched_count=20), SimpleNamespace(fetched_count=5)],
            [SimpleNamespace(fetched_count=9), SimpleNamespace(fetched_count=8)],
        ]
        plan.return_value = ([], [], date(2026, 9, 22), "交易日")
        repository_class.return_value.start_daily_backfill_run.return_value = 1
        capital = type("Capital", (), {"quote_ready": True})()

        state, message = run_scheduled_backfill(capital, config)

        self.assertEqual(state, "completed")
        self.assertIn("整張 35 檔、零股 17 檔", message)
        self.assertEqual(sync.call_count, 2)
        plan.assert_called_once()
        repository_class.return_value.update_daily_backfill_run.assert_called()

    @patch("app.api.routes._plan_stale_backfill")
    @patch("app.api.routes._sync_instrument_markets_unlocked")
    @patch("app.api.routes.MySQLCandleRepository")
    def test_daily_schedule_stops_before_kline_when_instrument_sync_fails(self, repository_class, sync, plan):
        with patch.dict("os.environ", {}, clear=True):
            config = replace(DailyBackfillConfig.from_env(), enabled=True, intervals=("day",))
        sync.side_effect = CapitalApiError("商品主檔連線失敗")
        repository_class.return_value.start_daily_backfill_run.return_value = 1
        capital = type("Capital", (), {"quote_ready": True})()

        with self.assertRaisesRegex(RuntimeError, "未執行 K 線更新"):
            run_scheduled_backfill(capital, config)

        plan.assert_not_called()
        repository_class.return_value.update_daily_backfill_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
