import os
import unittest
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch

from app.services.backfill_memory import BackfillMemoryGuard,memory_resume_ready,mark_memory_pause
from app.services.backfill_control import BackfillControl
from app.services.process_memory import ProcessMemory
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_scheduler import DailyBackfillConfig
from app.api.routes import run_scheduled_backfill,_resume_saved_backfill
from test_incremental_backfill import FakeControl,FakeRepository,job,plan,candle


class MemorySafetyTests(unittest.TestCase):
    @patch.dict(os.environ,{},clear=True)
    def test_warning_gc_is_rate_limited_and_stage_ring_is_bounded(self):
        control=FakeControl()
        guard=BackfillMemoryGuard(control,provider=lambda:ProcessMemory(2500,2500))
        with patch("app.services.backfill_memory.time.monotonic",return_value=100),patch("app.services.backfill_memory.gc.collect") as collect:
            for i in range(80): guard.check("after_write_release",i)
        collect.assert_called_once()
        self.assertEqual(len(control.snapshot()["memory_samples"]),32)
        self.assertEqual(control.snapshot()["memory_stage"],"after_write_release")

    @patch.dict(os.environ,{},clear=True)
    def test_normal_zero_windows_does_not_collect_on_every_check(self):
        guard=BackfillMemoryGuard(FakeControl(),provider=lambda:ProcessMemory(200,200))
        with patch("app.services.backfill_memory.gc.collect") as collect:
            for _ in range(10): guard.check("before_claim",0)
        collect.assert_not_called()

    def test_stage_history_and_gc_cooldown_survive_guard_handoff(self):
        control=FakeControl()
        with patch("app.services.backfill_memory.time.monotonic",return_value=100),patch("app.services.backfill_memory.gc.collect") as collect:
            first=BackfillMemoryGuard(control,provider=lambda:ProcessMemory(2500,2500))
            first.check("after_instruments")
            second=BackfillMemoryGuard(control,provider=lambda:ProcessMemory(2501,2501))
            second.check("after_group_planning")
        collect.assert_called_once()
        self.assertEqual([s["stage"] for s in control.snapshot()["memory_samples"]],["after_instruments","after_group_planning"])

    @patch.dict(os.environ,{},clear=True)
    def test_memory_pause_happens_before_group_planning(self):
        control=FakeControl(); repo=Mock(); vendor=Mock()
        scope=dict(plan(job()),batch_v2=True,target_bars=1200,planned_on=None)
        with patch("app.services.backfill_engine.current_process_memory",return_value=ProcessMemory(5000,5000)):
            result=BackfillRunner(vendor,repo,control).run([scope])
        self.assertEqual(result["pause_kind"],"memory")
        repo.plan_batch.assert_not_called()
        repo.claim_incremental_job.assert_not_called()
        vendor.fetch_historical_candles.assert_not_called()

    def test_received_rows_commit_before_memory_pause(self):
        item=job(interval="day",end=date(2026,10,2)); repo=FakeRepository([item]); control=FakeControl()
        vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle()]))
        runner=BackfillRunner(vendor,repo,control)
        runner.memory_check=lambda: runner.memory_stage=="after_request_before_write"
        result=runner.run([plan(item)])
        self.assertEqual(result["pause_kind"],"memory")
        self.assertEqual(item["status"],"pending")
        self.assertEqual(len(repo.rows),1)
        self.assertEqual(item["next_date"],date(2026,10,3))

    def test_new_api_request_cannot_replace_memory_paused_scope(self):
        saved=[dict(interval="day",market_no=0)]
        control=FakeControl(); control.update(state="paused",pause_kind="memory",plans=saved)
        repo=Mock(); vendor=Mock()
        with patch("app.services.backfill_engine.current_process_memory",return_value=ProcessMemory(5000,5000)):
            result=BackfillRunner(vendor,repo,control).run([dict(interval="week")])
        self.assertEqual(result["pause_kind"],"memory")
        self.assertEqual(control.snapshot()["plans"],saved)
        repo.plan_batch.assert_not_called()
        vendor.fetch_historical_candles.assert_not_called()

    @patch.dict(os.environ,{},clear=True)
    def test_recovery_requires_low_water_stability_and_cooldown(self):
        control=FakeControl(); control.update(pause_kind="memory")
        start=datetime(2026,10,16,10,tzinfo=timezone.utc)
        control.update(memory_retry_after=(start+timedelta(seconds=120)).isoformat())
        low=ProcessMemory(1000,3000)
        self.assertFalse(memory_resume_ready(control,start,low))
        self.assertFalse(memory_resume_ready(control,start+timedelta(seconds=60),low))
        self.assertTrue(memory_resume_ready(control,start+timedelta(seconds=120),low))
        self.assertFalse(memory_resume_ready(control,start+timedelta(seconds=121),ProcessMemory(1000,3100)))
        self.assertFalse(memory_resume_ready(control,start+timedelta(seconds=122),low))
        self.assertFalse(memory_resume_ready(control,start+timedelta(seconds=123),ProcessMemory(None,None)))

    def test_restart_does_not_reuse_stability_timer_from_old_process(self):
        control=FakeControl(); control.update(pause_kind="memory",memory_observer_pid=-1,
            memory_resume_below_since="2026-10-01T00:00:00+00:00")
        now=datetime(2026,10,16,10,tzinfo=timezone.utc)
        self.assertFalse(memory_resume_ready(control,now,ProcessMemory(100,100)))
        self.assertEqual(control.snapshot()["memory_resume_below_since"],now.isoformat())

    def test_reused_os_pid_still_cannot_reuse_old_observation_timer(self):
        control=FakeControl(); control.update(pause_kind="memory",memory_observer_pid=os.getpid(),
            memory_observer_instance="old-process",memory_resume_below_since="2026-10-01T00:00:00+00:00")
        self.assertFalse(memory_resume_ready(control,datetime(2026,10,16,10,tzinfo=timezone.utc),ProcessMemory(100,100)))

    def test_scheduler_does_not_requeue_memory_paused_existing_plans(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl(); control.update(pause_kind="memory",state="paused",plans=[dict(interval="day")])
            with patch("app.api.routes.MySQLCandleRepository") as repository,patch("app.api.routes._sync_instrument_markets_unlocked") as sync:
                with patch("app.services.backfill_memory.current_process_memory",return_value=ProcessMemory(4200,4200)):
                    state,_=run_scheduled_backfill(SimpleNamespace(quote_ready=True),DailyBackfillConfig.from_env())
            self.assertEqual(state,"paused")
            repository.assert_not_called(); sync.assert_not_called()
            self.assertEqual(control.snapshot()["plans"],[dict(interval="day")])

    def test_scheduler_stops_before_stock_sync_and_keeps_one_run_for_resume(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            config=replace(DailyBackfillConfig.from_env(),intervals=("day",))
            with patch("app.api.routes.MySQLCandleRepository") as repository,patch("app.api.routes._sync_instrument_markets_unlocked") as sync,patch("app.services.backfill_memory.current_process_memory",return_value=ProcessMemory(5000,5000)):
                repository.return_value.start_daily_backfill_run.return_value=101
                state,_=run_scheduled_backfill(SimpleNamespace(quote_ready=True),config)
            self.assertEqual(state,"paused"); sync.assert_not_called()
            saved=BackfillControl().snapshot()
            self.assertEqual(saved["pending_schedule"]["run_id"],101)
            with patch("app.api.routes.memory_resume_ready",return_value=True),patch("app.api.routes.run_scheduled_backfill") as scheduled:
                _resume_saved_backfill(SimpleNamespace(quote_ready=True),[],1200)
            scheduled.assert_called_once()

    def test_resumed_instrument_sync_reuses_same_schedule_id(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            config=replace(DailyBackfillConfig.from_env(),intervals=("day",))
            with patch("app.api.routes.MySQLCandleRepository") as repository,patch("app.api.routes._sync_instrument_markets_unlocked",return_value=[]) as sync,patch("app.api.routes._plan_stale_backfill",return_value=([],[],date(2026,10,16),"交易日")),patch("app.services.backfill_memory.current_process_memory",return_value=ProcessMemory(5000,5000)):
                repository.return_value.start_daily_backfill_run.return_value=101
                state,_=run_scheduled_backfill(SimpleNamespace(quote_ready=True),config)
                self.assertEqual(state,"paused")
                with patch("app.api.routes.memory_resume_ready",return_value=True),patch("app.services.backfill_memory.current_process_memory",return_value=ProcessMemory(200,200)):
                    state,_=run_scheduled_backfill(SimpleNamespace(quote_ready=True),config)
                self.assertEqual(state,"completed")
                repository.return_value.start_daily_backfill_run.assert_called_once()
                self.assertEqual(sync.call_count,2)
                self.assertTrue(all(call.args[0]==101 for call in repository.return_value.update_daily_backfill_run.call_args_list))
                self.assertIsNone(BackfillControl().snapshot()["pending_schedule"])
