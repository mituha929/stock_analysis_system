"""No DLL/network/MySQL: actual ASGI flow and real shared worker file locks."""
import asyncio
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from app.api.routes import router, BACKFILL_WORKER_LOCK, run_scheduled_backfill
from app.services.backfill_control import BackfillControl, BackfillRecovery
from app.services.backfill_scheduler import DailyBackfillConfig
from test_backfill_api_control import asgi_request


class RegrabAutostartTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory(prefix="regrab-autostart-"))
        self.enterContext(patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}))
        self.control = BackfillControl()
        self.control.update(state="paused",pause_requested=True,plans=[])
        self.app = FastAPI(); self.app.include_router(router)
        self.app.state.capital_service = SimpleNamespace(quote_ready=True)
        self.repo = Mock()
        self.repo.plan_batch.side_effect = lambda *_args,**_kwargs: dict(queued_jobs=2,start_date=date(2020,1,1))
        self.enterContext(patch("app.api.routes.MySQLCandleRepository",return_value=self.repo))
        self.enterContext(patch("app.api.routes.BackfillMemoryGuard"))
        self.enterContext(patch("app.api.routes.memory_resume_ready",return_value=True))
        self.enterContext(patch("app.api.routes.trading_day_status",return_value=(False,"週末")))
        self.worker = self.enterContext(patch("app.api.routes._run_stale_backfill_unlocked"))

    def post(self,path="/api/v1/backfill/regrab-all"):
        return asyncio.run(asgi_request(self.app,"POST",path))

    def test_all_35_groups_prepare_before_fetch_and_one_lease_spans_both(self):
        locks = []
        def plan(*args,**kwargs):
            locks.append(self.control.worker_active())
            return dict(queued_jobs=2,start_date=kwargs["explicit_start"])
        self.repo.plan_batch.side_effect=plan
        def fetch(capital,plans,target,**kwargs):
            self.assertEqual(len(plans),35)
            self.assertEqual(self.repo.plan_batch.call_count,35)
            self.assertTrue(self.control.worker_active())
            self.assertTrue(BACKFILL_WORKER_LOCK.locked())
            self.assertTrue(kwargs["owns_worker_lock"])
            self.assertEqual({p["interval"]:p["target_bars"] for p in plans},
                dict.fromkeys(["1m","5m","15m","60m","day"],1200)|{"week":560,"month":100})
            self.assertTrue(all(p["fixed_range"] and not p["force"] for p in plans))
            self.control.update(state="completed",resume_pending=False)
        self.worker.side_effect=fetch
        status,body=self.post()
        self.assertEqual(status,202)
        self.assertEqual(locks,[True]*35)
        self.worker.assert_called_once()
        self.assertFalse(self.control.worker_active())
        self.assertFalse(BACKFILL_WORKER_LOCK.locked())
        self.assertTrue(self.control.snapshot()["preparation_complete"])
        self.assertTrue(self.control.snapshot()["manual_run"])
        self.assertIn("直接",body["message"])

    def test_other_worker_blocks_regrab_and_resume_without_mutating_state(self):
        before=self.control.snapshot()
        with self.control.worker():
            self.assertEqual(self.post()[0],409)
            self.assertEqual(self.post("/api/v1/backfill/resume")[0],409)
        self.assertEqual(self.control.snapshot(),before)
        self.repo.plan_batch.assert_not_called(); self.worker.assert_not_called()

    def test_same_process_planning_or_queued_start_blocks_regrab(self):
        with BACKFILL_WORKER_LOCK:
            self.assertEqual(self.post()[0],409)
        self.control.update(state="queued",pause_requested=False,resume_pending=True)
        self.assertEqual(self.post()[0],409)
        self.repo.plan_batch.assert_not_called()
        self.assertFalse(self.control.worker_active())
        self.assertFalse(BACKFILL_WORKER_LOCK.locked())

    def test_external_process_is_excluded_for_whole_lease(self):
        code = (
            "from app.services.backfill_control import BackfillControl; "
            "import sys; sys.exit(23 if BackfillControl().worker_active() else 0)"
        )
        cwd = Path(__file__).resolve().parents[1]
        with self.control.worker():
            locked = subprocess.run([sys.executable,"-B","-c",code],cwd=cwd,
                capture_output=True,text=True,timeout=15)
        released = subprocess.run([sys.executable,"-B","-c",code],cwd=cwd,
            capture_output=True,text=True,timeout=15)
        self.assertEqual(locked.returncode,23,locked.stderr)
        self.assertEqual(released.returncode,0,released.stderr)

    def test_competitors_cannot_start_between_last_prepare_and_fetch(self):
        from scripts import backfill_market_candles as cli
        observed = []
        def fetch(*args,**kwargs):
            with patch.object(cli,"load_dotenv"),patch("sys.argv",["backfill","--resume"]), \
                 patch.object(cli,"CapitalKLineSession") as login:
                cli_status = cli.main()
                login_called = login.called
            state,_=run_scheduled_backfill(self.app.state.capital_service,DailyBackfillConfig.from_env())
            observed.append((cli_status, login_called, state))
        self.worker.side_effect=fetch
        self.assertEqual(self.post()[0],202)
        self.assertEqual(observed, [(1, False, "queued")])
        self.assertEqual(self.control.snapshot()["scheduled_daily_priority"]["phase"], "waiting_stop")
        self.assertEqual(self.repo.plan_batch.call_count,35)

    def test_partial_failure_does_not_fetch_or_allow_resume_or_lose_old_plans(self):
        old=[dict(market_no=0,interval="day",end_date="2026-10-16")]
        self.control.update(plans=old)
        self.repo.plan_batch.side_effect=[dict(queued_jobs=2,start_date=date(2020,1,1)),RuntimeError("test prepare fault")]
        self.assertEqual(self.post()[0],202)
        state=self.control.snapshot()
        self.assertFalse(state["preparation_complete"])
        self.assertFalse(state["preparation_pending"])
        self.assertTrue(state["pause_requested"])
        self.assertIn("test prepare fault",state["pause_reason"])
        self.assertEqual(state["plans"],old)
        self.worker.assert_not_called()
        self.assertEqual(self.post("/api/v1/backfill/resume")[0],409)
        self.assertFalse(self.control.worker_active())
        # A new explicit regrab, unlike resume, may finish the missing scope.
        self.repo.plan_batch.side_effect=lambda *a,**k:dict(queued_jobs=2,start_date=date(2020,1,1))
        self.assertEqual(self.post()[0],202)
        self.worker.assert_called_once()

    def test_pause_during_prepare_never_gets_cleared_by_auto_start(self):
        def plan(*args,**kwargs):
            self.control.pause(True)
            return dict(queued_jobs=2,start_date=date(2020,1,1))
        self.repo.plan_batch.side_effect=plan
        self.assertEqual(self.post()[0],202)
        self.assertEqual(self.repo.plan_batch.call_count,1)
        self.assertTrue(self.control.snapshot()["pause_requested"])
        self.worker.assert_not_called()

    def test_pause_at_final_group_keeps_complete_plans_but_does_not_fetch(self):
        def plan(*args,**kwargs):
            if self.repo.plan_batch.call_count==35: self.control.pause(True)
            return dict(queued_jobs=2,start_date=date(2020,1,1))
        self.repo.plan_batch.side_effect=plan
        self.assertEqual(self.post()[0],202)
        self.assertTrue(self.control.snapshot()["preparation_complete"])
        self.assertTrue(self.control.snapshot()["pause_requested"])
        self.worker.assert_not_called()

    def test_memory_and_connection_guards_run_before_any_requeue(self):
        self.app.state.capital_service.quote_ready=False
        self.assertEqual(self.post()[0],503)
        self.app.state.capital_service.quote_ready=True
        with patch("app.api.routes.memory_resume_ready",return_value=False):
            self.assertEqual(self.post()[0],409)
        self.repo.plan_batch.assert_not_called()
        self.assertFalse(BACKFILL_WORKER_LOCK.locked())
        self.assertFalse(self.control.worker_active())

    def test_connection_loss_after_prepare_waits_and_retains_complete_plans(self):
        def plan(*args,**kwargs):
            if self.repo.plan_batch.call_count==35: self.app.state.capital_service.quote_ready=False
            return dict(queued_jobs=2,start_date=date(2020,1,1))
        self.repo.plan_batch.side_effect=plan
        self.assertEqual(self.post()[0],202)
        self.assertEqual(self.control.snapshot()["state"],"waiting_connection")
        self.assertTrue(self.control.snapshot()["preparation_complete"])
        self.assertFalse(self.control.snapshot()["pause_requested"])
        self.worker.assert_not_called()

    def test_manual_resume_on_weekend_passes_worker_lease_without_replanning(self):
        self.control.update(plans=[dict(market_no=0,interval="day",start_date="2026-10-01",end_date="2026-10-16")])
        status,body=self.post("/api/v1/backfill/resume")
        self.assertEqual(status,202)
        self.worker.assert_called_once()
        self.assertTrue(self.worker.call_args.kwargs["owns_worker_lock"])
        self.assertTrue(self.control.snapshot()["manual_run"])
        self.repo.plan_batch.assert_not_called()

    def test_recovery_only_manual_work_is_allowed_on_holiday(self):
        callback=Mock()
        recovery=BackfillRecovery(self.app.state.capital_service,callback,Mock())
        self.control.update(source="api",state="waiting_connection",pause_requested=False,
            plans=[dict(market_no=0,interval="day")],manual_run=True)
        def poll():
            recovery.stop_event=Mock()
            recovery.stop_event.wait.side_effect=[False,True]
            with patch("app.services.backfill_control.BackfillControl",return_value=self.control), \
                 patch("app.services.backfill_control.memory_resume_ready",return_value=True), \
                 patch("app.services.taiwan_trading_calendar.trading_day_status",return_value=(False,"週末")):
                recovery._run()
        poll(); callback.assert_called_once()
        callback.reset_mock(); self.control.update(manual_run=False)
        poll(); callback.assert_not_called()
        self.control.update(manual_run=True,preparation_complete=False)
        poll(); callback.assert_not_called()

    def test_interrupted_prepare_is_not_auto_resumed_after_startup(self):
        self.control.update(source="api",state="preparing",preparation_pending=True,pause_requested=False)
        recovery=BackfillRecovery(self.app.state.capital_service,Mock(),Mock())
        with patch("app.services.backfill_control.BackfillControl",return_value=self.control), \
             patch("app.services.backfill_control.threading.Thread"):
            recovery.start()
        state=self.control.snapshot()
        self.assertFalse(state["preparation_pending"])
        self.assertFalse(state["preparation_complete"])
        self.assertTrue(state["pause_requested"])

