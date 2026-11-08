"""A due daily schedule safely preempts normal or slow collection."""
import os
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.api.routes import BACKFILL_WORKER_LOCK, _resume_scheduled_daily_priority, run_scheduled_backfill
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_scheduler import DailyBackfillConfig, DailyBackfillScheduler
from test_incremental_backfill import FakeRepository, candle, job, plan


class ScheduledPriorityTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory(prefix="scheduled-priority-"))
        self.enterContext(patch.dict(os.environ, {"CAPITAL_BACKFILL_CONTROL_DB": str(Path(folder) / "control.sqlite3")}))
        self.control = BackfillControl()
        self.capital = SimpleNamespace(quote_ready=True)
        self.config = DailyBackfillConfig.from_env()

    def _slow(self):
        self.control.update(state="waiting_backfill", source="api", pause_requested=False,
            normal_round_complete=True, plans=[dict(market_no=0, interval="day", end_date="2026-10-22",
                                                    planned_on="2026-10-22", job_table="candle_backfill_jobs")])

    def test_current_window_commits_before_daily_and_old_scope_restored(self):
        day = date(2026, 10, 16)
        item = job(interval="day", start=day, end=day)
        repo = FakeRepository([item])
        entered, release = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            entered.set(); release.wait(5)
            return [candle(day)]
        worker = BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch), repo, self.control)
        worker.memory_check = lambda: False
        def run_old():
            with BACKFILL_WORKER_LOCK:
                worker.run([plan(item)])
        thread = threading.Thread(target=run_old)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(self.control.snapshot()["normal_round_complete"])
            with patch("app.api.routes._run_scheduled_backfill_owned") as daily:
                state, _ = run_scheduled_backfill(self.capital, self.config)
                self.assertEqual(state, "queued")
                daily.assert_not_called()
                self.assertTrue(self.control.snapshot()["pause_requested"])
                release.set(); thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(repo.rows), 1)
                def complete(*args, **kwargs):
                    self.assertTrue(kwargs["force_completed_refresh"])
                    self.assertTrue(kwargs["fresh_daily"])
                    saved = self.control.snapshot()
                    token = saved["scheduled_daily_priority"]["id"]
                    self.assertTrue(self.control.start_scheduled_daily(token, {"run_id": 123}))
                    self.control.update(state="completed", normal_round_complete=True,
                                        pending_schedule=None, plans=[dict(market_no=1, interval="1m")])
                    return "completed", "ok"
                daily.side_effect = complete
                _resume_scheduled_daily_priority(self.capital)
                saved = self.control.snapshot()
                self.assertEqual(saved["scheduled_daily_priority"]["phase"], "done")
                self.assertEqual(saved["state"], "waiting_backfill")
                self.assertEqual([p["interval"] for p in saved["plans"]], ["1m", "day"])
                self.assertTrue(saved["plans"][1]["fixed_range"] if saved["plans"][1].get("planned_on") else True)
        finally:
            release.set(); thread.join(5)

    def test_failed_commit_preserves_old_scope_and_blocks_switch(self):
        self._slow()
        self.control.request_scheduled_priority({})
        self.control.update(state="paused", pause_kind="database")
        with patch("app.api.routes._run_scheduled_backfill_owned") as daily:
            _resume_scheduled_daily_priority(self.capital)
        daily.assert_not_called()
        saved = self.control.snapshot()
        self.assertEqual(saved["scheduled_daily_priority"]["phase"], "failed")
        self.assertEqual(len(saved["plans"]), 1)

    def test_pause_cancels_pending_switch(self):
        self._slow()
        self.control.request_scheduled_priority({})
        self.control.pause(True)
        _resume_scheduled_daily_priority(self.capital)
        self.assertEqual(self.control.snapshot()["scheduled_daily_priority"]["phase"], "cancelled")
        self.assertEqual(len(self.control.snapshot()["plans"]), 1)

    def test_restart_can_recover_ready_transition(self):
        self._slow()
        with BACKFILL_WORKER_LOCK:
            run_scheduled_backfill(self.capital, self.config)
        token = self.control.snapshot()["scheduled_daily_priority"]["id"]
        self.assertTrue(self.control.start_scheduled_priority(token))
        self.assertEqual(self.control.snapshot()["scheduled_daily_priority"]["phase"], "ready")
        with patch("app.api.routes._run_scheduled_backfill_owned") as daily:
            def complete(*args, **kwargs):
                self.assertTrue(self.control.start_scheduled_daily(token, {"run_id": 1}))
                self.control.update(state="completed", normal_round_complete=True, plans=[])
                return "completed", "ok"
            daily.side_effect = complete
            _resume_scheduled_daily_priority(self.capital)
        saved = self.control.snapshot()
        self.assertEqual(saved["scheduled_daily_priority"]["phase"], "done")
        self.assertEqual(saved["plans"][0]["as_of_date"], "2026-10-22")

    def test_user_paused_work_is_not_preempted(self):
        self._slow()
        self.control.update(normal_round_complete=True, pause_requested=True)
        _, created = self.control.request_scheduled_priority({})
        self.assertFalse(created)

    def test_busy_without_a_resumable_scope_returns_retry_state(self):
        self.control.update(state="planning", plans=[])
        with BACKFILL_WORKER_LOCK:
            state, _ = run_scheduled_backfill(self.capital, self.config, scheduled_due=True)
        self.assertEqual(state, "waiting_worker")
        self.assertFalse(self.control.snapshot()["pause_requested"])

    def test_busy_tick_retries_in_the_same_day(self):
        calls = []
        def callback(config):
            calls.append(config)
            return ("waiting_worker", "busy") if len(calls) == 1 else ("completed", "done")
        scheduler = DailyBackfillScheduler(replace(self.config, enabled=True), callback)
        first = scheduler._timezone.localize(datetime(2026,10,30,18,30))
        second = scheduler._timezone.localize(datetime(2026,10,30,18,30,15))
        with patch.object(scheduler, "_select_intervals", return_value=(("day",), "交易日", {})):
            scheduler._tick(first)
            self.assertIsNone(scheduler._last_attempted_date)
            scheduler._tick(second)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].as_of_date, date(2026,10,30))
        self.assertEqual(scheduler._state, "completed")

    def test_idle_due_round_forces_same_date_refresh(self):
        self.control.update(state="completed", plans=[])
        with patch("app.api.routes._run_scheduled_backfill_owned", return_value=("completed", "done")) as daily:
            run_scheduled_backfill(self.capital, self.config, scheduled_due=True)
        self.assertTrue(daily.call_args.kwargs["force_completed_refresh"])
        self.assertTrue(daily.call_args.kwargs["fresh_daily"])

    def test_next_day_can_be_scheduled_while_previous_callback_is_running(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def callback(config):
            calls.append(config)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
                return "paused", "previous day"
            return "queued", "new day"
        scheduler = DailyBackfillScheduler(replace(self.config, enabled=True), callback)
        first = scheduler._timezone.localize(datetime(2026,10,28,18,30))
        second = scheduler._timezone.localize(datetime(2026,10,29,18,30))
        with patch.object(scheduler, "_select_intervals", return_value=(("day",), "交易日", {})):
            thread = threading.Thread(target=scheduler._tick, args=(first,))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                scheduler._tick(second)
                self.assertEqual(len(calls), 2)
            finally:
                release.set(); thread.join(5)
        self.assertEqual(scheduler._state, "queued")
        self.assertIn("new day", scheduler._last_message)


if __name__ == "__main__":
    unittest.main()
