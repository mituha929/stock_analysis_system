"""Regression tests for connection/database pauses and resumed scheduling."""
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch
from app.api.routes import _plan_stale_backfill,run_scheduled_backfill
from app.services.backfill_scheduler import DailyBackfillConfig
from app.services.backfill_engine import BackfillRunner
from app.services.capital_history_connection import CapitalHistoryConnectionError
from app.services.mysql_instrument_repository import MySQLCandleRepository,MySQLRepositoryError,MySQLSettings
from test_incremental_backfill import job,plan,candle,FakeRepository,FakeControl

class BackfillPauseWorkerTests(unittest.TestCase):
    @patch.object(BackfillRunner,"memory_check",return_value=False)
    def test_success_before_disconnect_keeps_its_completion(self,_memory):
        first=job(interval="day",end=date(2026,10,2))
        second=job(interval="day",end=date(2026,10,2),job_id=2)
        repo=FakeRepository([first,second])
        capital=SimpleNamespace(fetch_historical_candles=Mock(side_effect=[[candle()],CapitalHistoryConnectionError("offline")]))
        result=BackfillRunner(capital,repo,FakeControl()).run([plan(first)])
        self.assertEqual(result["completed"],1)
        self.assertEqual(result["failed"],0)
        self.assertEqual(second["status"],"pending")

    @patch.object(BackfillRunner,"memory_check",return_value=False)
    def test_stock_error_fails_only_that_job_and_continues(self,_memory):
        first=job(interval="day",end=date(2026,10,2))
        second=job(interval="day",end=date(2026,10,2),job_id=2)
        repo=FakeRepository([first,second])
        capital=SimpleNamespace(fetch_historical_candles=Mock(side_effect=[RuntimeError("bad stock"),[candle()]]))
        result=BackfillRunner(capital,repo,FakeControl()).run([plan(first)])
        self.assertEqual(result["failed"],1)
        self.assertEqual(result["completed"],1)

class BackfillPauseIntegrationTests(unittest.TestCase):
    @patch("app.api.routes.BackfillControl")
    @patch("app.api.routes.MySQLCandleRepository")
    def test_week_plan_clamps_pending_work_to_completed_week(self,repo_type,control):
        control.return_value.worker_active.return_value=False
        repo_type.return_value.plan_incremental.return_value=dict(
            start_date=date(2026,9,28),queued_jobs=3,initialization_jobs=0,incremental_jobs=3)
        plans,work,_latest,_reason=_plan_stale_backfill([0],["week"],1200,True,today=date(2026,10,7))
        self.assertEqual(len(work),1)
        self.assertEqual(work[0]["end_date"],date(2026,10,2))
        self.assertEqual(plans[0].action,"planning_at_group_start")
        repo_type.assert_not_called()
        repo_type.return_value.get_oldest_backfill_updates.assert_not_called()

    @patch("app.api.routes._run_stale_backfill_unlocked")
    @patch("app.api.routes._plan_stale_backfill")
    @patch("app.api.routes._sync_instrument_markets_unlocked")
    @patch("app.api.routes.MySQLCandleRepository")
    def test_schedule_log_marks_pause_not_completion(self,repo_type,sync,planner,worker):
        with patch.dict("os.environ",{},clear=True):
            config=replace(DailyBackfillConfig.from_env(),enabled=True,intervals=("week",))
        sync.return_value=[SimpleNamespace(fetched_count=10)]
        planner.return_value=([SimpleNamespace(queued_jobs=100)],[plan(job())],date(2026,10,7),"交易日")
        worker.return_value=dict(processed=1,completed=1,insufficient_history=0,failed=0,paused=True,pause_reason="offline")
        repo_type.return_value.start_daily_backfill_run.return_value=1
        state,_message=run_scheduled_backfill(SimpleNamespace(quote_ready=True),config)
        self.assertEqual(state,"paused")
        final=repo_type.return_value.update_daily_backfill_run.call_args.kwargs
        self.assertEqual(final["status"],"paused")
        self.assertEqual(final["failed_jobs"],0)

class LegacyRepositoryPauseTests(unittest.TestCase):
    def test_repository_release_only_updates_owned_running_job(self):
        repo = MySQLCandleRepository(MySQLSettings("unused", 3306, "test", "test", "test"))
        connection = Mock()
        connection.cursor.return_value.rowcount = 1
        with patch.object(repo, "_connector"), patch.object(repo, "_connect_database", return_value=connection):
            repo.pause_backfill_job(9, "offline", "odd_lot_candle_backfill_jobs", received_count=3)
        sql, params = connection.cursor.return_value.execute.call_args.args
        self.assertIn("`odd_lot_candle_backfill_jobs`", sql)
        self.assertIn("status = 'pending'", sql)
        self.assertIn("WHERE id = %s AND status = 'running'", sql)
        self.assertEqual(params[0], 3)
        self.assertEqual(params[-1], 9)
        connection.commit.assert_called_once()

    def test_repository_release_failure_rolls_back_and_reports_it(self):
        repo = MySQLCandleRepository(MySQLSettings("unused", 3306, "test", "test", "test"))
        connection = Mock()
        connection.cursor.return_value.rowcount = 0
        with patch.object(repo, "_connector"), patch.object(repo, "_connect_database", return_value=connection):
            with self.assertRaises(MySQLRepositoryError):
                repo.pause_backfill_job(9, "offline")
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_pending_groups_only_select_active_instruments_and_requested_scope(self):
        repo = MySQLCandleRepository(MySQLSettings("unused", 3306, "test", "test", "test"))
        connection = Mock()
        connection.cursor.return_value.fetchall.return_value = []
        with patch.object(repo, "_connector"), patch.object(repo, "_connect_database", return_value=connection):
            self.assertEqual(repo.get_pending_backfill_groups(
                [6], ["week", "month"], "odd_lot_candle_backfill_jobs", "odd_lot_instruments",
            ), [])
        sql, params = connection.cursor.return_value.execute.call_args.args
        self.assertIn("jobs.status = 'pending'", sql)
        self.assertIn("instruments.is_active = TRUE", sql)
        self.assertIn("`odd_lot_candle_backfill_jobs`", sql)
        self.assertEqual(params, (6, "week", "month"))


if __name__ == "__main__":
    unittest.main()
