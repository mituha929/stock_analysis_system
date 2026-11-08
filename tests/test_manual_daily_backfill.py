"""Manual daily refresh uses the existing worker, scope, and checkpoints."""
import asyncio
import os
import sqlite3
import threading
import unittest
import json
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI

from app.api.routes import (BACKFILL_WORKER_LOCK, _resume_saved_backfill, _resume_manual_daily_takeover,
                            _resume_saved_backfill_owned, router, run_scheduled_backfill)
from app.services.backfill_control import BackfillControl, BackfillRecovery
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_memory import BackfillMemoryPause
from app.services.backfill_scheduler import DailyBackfillConfig
from app.services.process_memory import ProcessMemory
from app.services.mysql_instrument_repository import MySQLRepositoryError
from test_backfill_api_control import asgi_request
from test_incremental_backfill import FakeControl, FakeRepository, candle, job, plan


class ManualDailyBackfillTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory(prefix="manual-daily-"))
        self.enterContext(patch.dict(os.environ, {
            "CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3"),
            "CAPITAL_DAILY_REFRESH_TARGETS":"1m:1200,5m:240,15m:60,60m:20,day:10,week:5,month:2",
            "CAPITAL_DAILY_AGGREGATE_FROM_DAY":"true",
        }))
        self.control = BackfillControl()
        self.control.update(state="completed", plans=[], pause_requested=False)
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.capital_service = SimpleNamespace(quote_ready=True)
        self.app.state.backfill_scheduler = SimpleNamespace(config=DailyBackfillConfig.from_env())
        self.enterContext(patch("app.api.routes.current_process_memory", return_value=ProcessMemory(100,100)))

    def post(self):
        return asyncio.run(asgi_request(self.app, "POST", "/api/v1/backfill/daily/run"))

    def test_runs_same_daily_configuration_under_both_locks(self):
        def execute(capital, config, **options):
            self.assertTrue(self.control.worker_active())
            self.assertTrue(BACKFILL_WORKER_LOCK.locked())
            self.assertEqual(options, dict(manual=True,force_completed_refresh=True,skip_day_integrity=True))
            self.assertEqual(config.intervals, ("day","1m","5m","15m","60m","week","month"))
            self.assertEqual(config.refresh_bars_by_interval["day"], 10)
            self.assertEqual(config.refresh_bars_by_interval["week"], 5)
            self.assertTrue(config.aggregate_week_month_from_day)
            self.assertTrue(config.resume)
            return "completed", "test complete"
        with patch("app.api.routes._run_scheduled_backfill_owned", side_effect=execute) as task, \
             patch("app.services.manual_daily_queue.archive_and_supersede",return_value=2):
            status, body = self.post()
        self.assertEqual(status,202)
        self.assertEqual(body["refresh_bars_by_interval"]["1m"],1200)
        self.assertEqual(body["refresh_bars_by_interval"]["month"],2)
        self.assertTrue(body["request_id"])
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["state"],"completed")
        self.assertFalse(self.control.worker_active())
        self.assertFalse(BACKFILL_WORKER_LOCK.locked())
        task.assert_called_once()

    def test_old_paused_scope_is_archived_and_replaced(self):
        old=[dict(market_no=0,interval="day",end_date="2026-10-22",job_table="candle_backfill_jobs")]
        self.control.update(state="paused",plans=old,pause_requested=True,pause_kind="manual")
        def run(*_args,**_kwargs):
            self.assertEqual(self.control.snapshot()["plans"],[])
            self.assertFalse(self.control.snapshot()["pause_requested"])
            with closing(sqlite3.connect(self.control.path)) as connection:
                payload=connection.execute("SELECT payload FROM manual_daily_displaced").fetchone()[0]
            self.assertIn('2026-10-22',payload)
            return "completed","daily done"
        with patch("app.api.routes._run_scheduled_backfill_owned",side_effect=run) as task, \
             patch("app.services.manual_daily_queue.archive_and_supersede",return_value=7) as archive:
            status,body=self.post()
        self.assertEqual(status,202)
        self.assertEqual(archive.call_args.args[1],old)
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["archived_jobs"],7)
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["phase"],"done")
        task.assert_called_once()

    def test_old_running_marker_does_not_block_paused_or_deferred_round(self):
        old_plans=[dict(market_no=0,interval="day",end_date="2026-10-22",job_table="candle_backfill_jobs")]
        for control_state, marker_state in (("paused","paused"),("waiting_backfill","waiting_backfill"),
                                             ("running","paused")):
            with self.subTest(control_state=control_state,marker_state=marker_state):
                self.control.update(state=control_state,pause_requested=control_state=="paused",plans=old_plans,
                    manual_daily_run=dict(id="old-request",phase="running",state=marker_state,
                                          requested_at=datetime.now(timezone.utc).isoformat()))
                with BACKFILL_WORKER_LOCK:
                    status,body=self.post()
                    self.assertEqual(status,202)
                    self.assertTrue(body["created"])
                    self.assertNotEqual(body["request_id"],"old-request")
                    self.assertEqual(body["replaces_request_id"],"old-request")
                    self.assertEqual(body["state"],"waiting_stop")
                    self.assertEqual(self.control.snapshot()["plans"],old_plans)
                    self.assertEqual(self.post()[1]["request_id"],body["request_id"])

    def test_previous_day_running_request_can_be_replaced(self):
        self.control.update(state="running",pause_requested=False,
            manual_daily_run=dict(id="yesterday-request",phase="running",state="running",
                requested_at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()))
        with BACKFILL_WORKER_LOCK:
            _,body=self.post()
        self.assertTrue(body["created"])
        self.assertNotEqual(body["request_id"],"yesterday-request")

    def test_same_day_active_round_is_idempotent(self):
        config=self.app.state.backfill_scheduler.config
        settings=dict(markets=list(config.markets),intervals=list(config.configured_intervals),
                      refresh_bars_by_interval=config.refresh_bars_by_interval,
                      aggregate_week_month_from_day=config.aggregate_week_month_from_day)
        self.control.update(state="running",pause_requested=False,
            manual_daily_run=dict(id="today-request",phase="running",state="running",settings=settings,
                                  requested_at=datetime.now(timezone.utc).isoformat()))
        with BACKFILL_WORKER_LOCK:
            _,body=self.post()
        self.assertFalse(body["created"])
        self.assertEqual(body["request_id"],"today-request")
        self.assertFalse(self.control.snapshot()["pause_requested"])

    def test_daily_success_declares_utf8_for_windows_powershell(self):
        messages=[]
        async def request():
            async def receive(): return {"type":"http.request","body":b"","more_body":False}
            async def send(message): messages.append(message)
            path="/api/v1/backfill/daily/run"
            await self.app(dict(type="http",asgi={"version":"3.0"},http_version="1.1",method="POST",
                path=path,raw_path=path.encode(),query_string=b"",headers=[],scheme="http",
                server=("test",80),client=("test",123),root_path=""),receive,send)
        with BACKFILL_WORKER_LOCK:
            asyncio.run(request())
        start=next(m for m in messages if m["type"]=="http.response.start")
        self.assertEqual([v for k,v in start["headers"] if k==b"content-type"],
                         [b"application/json; charset=utf-8"])
        body=b"".join(m.get("body",b"") for m in messages if m["type"]=="http.response.body")
        self.assertEqual(json.loads(body.decode("utf-8"))["message"],
                         "已請求安全保存目前區間；停止後移出舊佇列並啟動每日更新")

    def test_existing_review_may_remain_after_normal_round_completes(self):
        self.control.update(state="waiting_review",normal_round_complete=True,
                            plans=[dict(market_no=0,interval="day")])
        with patch("app.api.routes._run_scheduled_backfill_owned",return_value=("waiting_review","待查保留")), \
             patch("app.services.manual_daily_queue.archive_and_supersede",return_value=0):
            self.assertEqual(self.post()[0],202)
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["state"],"waiting_review")

    def test_worker_memory_and_connection_guards(self):
        self.app.state.capital_service.quote_ready=False
        self.assertEqual(self.post()[0],202)
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["state"],"waiting_connection")
        self.assertTrue(self.control.snapshot()["pause_requested"])
        self.app.state.capital_service.quote_ready=True
        with patch("app.api.routes.current_process_memory",return_value=ProcessMemory(5000,5000)):
            _resume_manual_daily_takeover(self.app.state.capital_service)
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["state"],"waiting_memory")
        self.assertTrue(self.control.snapshot()["pause_requested"])
        self.assertFalse(self.control.worker_active())
        self.assertFalse(BACKFILL_WORKER_LOCK.locked())

    def test_background_failure_is_reported_and_unlocks(self):
        with patch("app.api.routes._run_scheduled_backfill_owned",side_effect=RuntimeError("test failure")), \
             patch("app.services.manual_daily_queue.archive_and_supersede",return_value=0):
            self.assertEqual(self.post()[0],202)
        record=self.control.snapshot()["manual_daily_run"]
        self.assertEqual(record["state"],"error")
        self.assertIn("test failure",record["message"])
        self.assertFalse(self.control.worker_active())
        self.assertFalse(BACKFILL_WORKER_LOCK.locked())

    def test_active_worker_commits_before_old_queue_is_removed(self):
        day=date(2026,10,16)
        item=job(interval="day",start=day,end=day)
        repo=FakeRepository([item]); entered=threading.Event(); release=threading.Event()
        def fetch(*_args,**_kwargs):
            entered.set()
            if not release.wait(5): raise RuntimeError("test timeout")
            return [candle(day)]
        worker=BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch),repo,self.control)
        worker.memory_check=lambda:False
        def run_old():
            with BACKFILL_WORKER_LOCK:
                worker.run([plan(item)])
        thread=threading.Thread(target=run_old)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with patch("app.services.manual_daily_queue.archive_and_supersede",return_value=1) as archive, \
                 patch("app.api.routes._run_scheduled_backfill_owned",return_value=("completed","daily done")) as daily:
                status,body=self.post()
                self.assertEqual(status,202)
                self.assertTrue(self.control.snapshot()["pause_requested"])
                archive.assert_not_called(); daily.assert_not_called()
                # Repeating the request is idempotent while the old work stops.
                self.assertEqual(self.post()[1]["request_id"],body["request_id"])
                release.set(); thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(repo.rows),1)
                _resume_manual_daily_takeover(self.app.state.capital_service)
                archive.assert_called_once(); daily.assert_called_once()
                self.assertEqual(self.control.snapshot()["manual_daily_run"]["phase"],"done")
        finally:
            release.set(); thread.join(5)

    def test_failed_current_commit_does_not_remove_old_queue(self):
        day=date(2026,10,16)
        item=job(interval="day",start=day,end=day)
        repo=FakeRepository([item]); repo.fail_commit=True
        entered=threading.Event(); release=threading.Event()
        def fetch(*_args,**_kwargs):
            entered.set(); release.wait(5)
            return [candle(day)]
        worker=BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch),repo,self.control)
        worker.memory_check=lambda:False
        def run_old():
            with BACKFILL_WORKER_LOCK:
                worker.run([plan(item)])
        thread=threading.Thread(target=run_old); thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with patch("app.services.manual_daily_queue.archive_and_supersede") as archive, \
                 patch("app.api.routes._run_scheduled_backfill_owned") as daily:
                self.assertEqual(self.post()[0],202)
                release.set(); thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(self.control.snapshot()["pause_kind"],"database")
                _resume_manual_daily_takeover(self.app.state.capital_service)
                marker=self.control.snapshot()["manual_daily_run"]
                self.assertEqual(marker["phase"],"failed")
                self.assertIn("未移出舊佇列",marker["message"])
                self.assertTrue(self.control.snapshot()["plans"])
                archive.assert_not_called(); daily.assert_not_called()
        finally:
            release.set(); thread.join(5)

    def test_user_pause_cancels_pending_switch(self):
        with BACKFILL_WORKER_LOCK:
            self.assertEqual(self.post()[0],202)
            self.control.pause(True)
        with patch("app.services.manual_daily_queue.archive_and_supersede") as archive:
            _resume_manual_daily_takeover(self.app.state.capital_service)
        archive.assert_not_called()
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["phase"],"cancelled")

    def test_database_failure_keeps_old_scope_and_retries_same_request(self):
        old=[dict(market_no=0,interval="week",job_table="candle_backfill_jobs")]
        self.control.update(state="paused",plans=old,pause_requested=True)
        with patch("app.services.manual_daily_queue.archive_and_supersede",
                   side_effect=[MySQLRepositoryError("test database offline"),3]) as archive, \
             patch("app.api.routes._run_scheduled_backfill_owned",return_value=("completed","done")) as daily:
            status,body=self.post()
            self.assertEqual(status,202)
            failed=self.control.snapshot()
            self.assertEqual(failed["plans"],old)
            self.assertTrue(failed["pause_requested"])
            self.assertEqual(failed["manual_daily_run"]["state"],"waiting_database")
            self.assertEqual(failed["manual_daily_run"]["phase"],"parking")
            daily.assert_not_called()
            _resume_manual_daily_takeover(self.app.state.capital_service)
            self.assertEqual(archive.call_count,2)
            self.assertEqual(archive.call_args_list[0].args[2],body["request_id"])
            self.assertEqual(archive.call_args_list[1].args[2],body["request_id"])
            self.assertEqual(self.control.snapshot()["manual_daily_run"]["state"],"completed")
            daily.assert_called_once()

    def test_pause_during_parking_finishes_archive_without_starting_new_work(self):
        def park(*_args):
            self.control.pause(True)
            return 4
        with patch("app.services.manual_daily_queue.archive_and_supersede",side_effect=park), \
             patch("app.api.routes._run_scheduled_backfill_owned") as daily:
            self.assertEqual(self.post()[0],202)
        saved=self.control.snapshot()
        self.assertEqual(saved["manual_daily_run"]["phase"],"cancelled")
        self.assertTrue(saved["pause_requested"])
        self.assertEqual(saved["manual_daily_run"]["archived_jobs"],4)
        daily.assert_not_called()

    def test_ready_transition_recovers_after_api_restart_without_reparking(self):
        config=self.app.state.backfill_scheduler.config
        settings=dict(markets=list(config.markets),intervals=list(config.configured_intervals),
                      target_bars=config.target_bars,target_bars_by_interval=config.target_bars_by_interval,
                      refresh_bars_by_interval=config.refresh_bars_by_interval,
                      aggregate_week_month_from_day=config.aggregate_week_month_from_day)
        self.control.request_manual_daily("test-restart",settings)
        self.control.update_manual_daily("test-restart",{"waiting_stop"},phase="parking",state="parking")
        self.control.finish_manual_daily_parking("test-restart",2)
        recovery=BackfillRecovery(self.app.state.capital_service, _resume_saved_backfill, Mock())
        recovery.stop_event=Mock()
        recovery.stop_event.wait.side_effect=[False,True]
        with patch("app.services.manual_daily_queue.archive_and_supersede") as archive, \
             patch("app.api.routes._run_scheduled_backfill_owned",return_value=("completed","done")) as daily:
            recovery._run()
        archive.assert_not_called()
        daily.assert_called_once()
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["phase"],"done")

    def test_interrupted_instrument_sync_resumes_same_manual_options(self):
        pending=dict(run_id=42,markets=[0],intervals=["day"],target_bars_by_interval={"day":1200},
                     refresh_bars_by_interval={"day":10},aggregate_week_month_from_day=True,
                     force_completed_refresh=True,skip_day_integrity=True)
        self.control.update(state="paused",source="api",manual_run=True,pending_schedule=pending,
                            pause_requested=False)
        with patch("app.api.routes.memory_resume_ready",return_value=True), \
             patch("app.api.routes.run_scheduled_backfill") as scheduled:
            _resume_saved_backfill(self.app.state.capital_service,[],1200)
        self.assertTrue(scheduled.call_args.kwargs["manual"])
        self.assertTrue(scheduled.call_args.kwargs["force_completed_refresh"])
        self.assertTrue(scheduled.call_args.kwargs["skip_day_integrity"])

    def test_instrument_sync_pause_saves_manual_options_for_recovery(self):
        control=FakeControl()
        control.update(state="completed", plans=[])
        config=self.app.state.backfill_scheduler.config
        with patch("app.api.routes.BackfillControl",return_value=control), \
             patch("app.api.routes.MySQLCandleRepository") as repository, \
             patch("app.api.routes.BackfillMemoryGuard") as guard, \
             patch("app.api.routes.mark_memory_pause"), \
             patch("app.api.routes._sync_instrument_markets_unlocked") as sync:
            repository.return_value.start_daily_backfill_run.return_value=42
            guard.return_value.require.side_effect=BackfillMemoryPause("test limit")
            state,_ = run_scheduled_backfill(self.app.state.capital_service,config,manual=True,
                                             force_completed_refresh=True,skip_day_integrity=True)
        self.assertEqual(state,"paused")
        pending=control.snapshot()["pending_schedule"]
        self.assertEqual(pending["run_id"],42)
        self.assertTrue(pending["force_completed_refresh"])
        self.assertTrue(pending["skip_day_integrity"])
        sync.assert_not_called()

    def test_manual_pipeline_passes_refresh_options_into_every_group(self):
        control=FakeControl()
        control.update(state="completed", plans=[])
        original=self.app.state.backfill_scheduler.config
        config=replace(original,markets=(0,),intervals=original.configured_intervals,resume=True)
        work=[dict(interval=i,target_bars=config.target_bars_by_interval[i]) for i in config.intervals]
        result=dict(processed=7,completed=7,failed=0,insufficient_history=0,deferred=0)
        with patch("app.api.routes.BackfillControl",return_value=control), \
             patch("app.api.routes.MySQLCandleRepository") as repository, \
             patch("app.api.routes.BackfillMemoryGuard"), \
             patch("app.api.routes._sync_instrument_markets_unlocked",return_value=[]), \
             patch("app.api.routes._plan_stale_backfill",return_value=([],work,date(2026,10,16),"test")) as planning, \
             patch("app.api.routes._run_stale_backfill_unlocked",return_value=result) as runner:
            repository.return_value.start_daily_backfill_run.return_value=42
            state,_=run_scheduled_backfill(self.app.state.capital_service,config,manual=True,
                                           force_completed_refresh=True,skip_day_integrity=True)
        self.assertEqual(state,"completed")
        self.assertTrue(planning.call_args.kwargs["allow_non_trading_day"])
        self.assertTrue(planning.call_args.kwargs["aggregate_from_day"])
        self.assertTrue(all(p["force_completed_refresh"] and p["skip_day_integrity"] for p in runner.call_args.args[1]))
        self.assertEqual([p["refresh_bars"] for p in runner.call_args.args[1]], [10,1200,240,60,20,5,2])
        self.assertEqual(control.snapshot()["manual_daily_run"]["state"],"completed")

    def test_resumed_review_state_updates_daily_log_and_manual_status(self):
        self.control.update(state="waiting_backfill",source="api",manual_run=True,
            manual_daily_run={"id":"test","state":"paused"},
            plans=[dict(market_no=0,interval="day",batch_v2=True,run_id=42,
                        force_completed_refresh=True,skip_day_integrity=True)])
        result=dict(processed=1,completed=1,failed=0,insufficient_history=0,
                    pending_review=1,pending_review_symbols=1,pending_review_dates=10)
        with patch("app.api.routes.memory_resume_ready",return_value=True), \
             patch("app.api.routes.MySQLCandleRepository") as repository, \
             patch("app.api.routes.BackfillRunner") as runner:
            runner.return_value.run.return_value=result
            _resume_saved_backfill_owned(self.app.state.capital_service,self.control,[],1200)
        values=repository.return_value.update_daily_backfill_run.call_args.kwargs
        self.assertEqual(values["status"],"waiting_review")
        self.assertEqual(values["stage"],"waiting_review")
        self.assertIn("1 檔／10 個日期",values["message"])
        self.assertEqual(self.control.snapshot()["manual_daily_run"]["state"],"waiting_review")

    def test_manual_daily_does_not_trigger_holiday_integrity_audit(self):
        day=date(2026,10,16)
        item=job(interval="day",start=day,end=day)
        control=FakeControl()
        scope=dict(plan(item),skip_day_integrity=True)
        vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle(day)]))
        worker=BackfillRunner(vendor,FakeRepository([item]),control)
        with patch.object(worker,"memory_check",return_value=False), \
             patch("app.services.backfill_engine.integrity_schedule") as schedule:
            result=worker.run([scope])
        self.assertEqual(result["completed"],1)
        schedule.assert_not_called()
        self.assertFalse(control.snapshot()["day_integrity_scheduled"])


if __name__ == "__main__":
    unittest.main()
