import os
import asyncio
import unittest
from datetime import date,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch

from fastapi import HTTPException,FastAPI
from app.api.routes import router
from test_backfill_api_control import asgi_request
from app.api.routes import prepare_board_lot_backfill,prepare_odd_lot_backfill,run_scheduled_backfill
from app.services.backfill_control import BackfillControl
from app.services.backfill_policy import initial_start
from app.services.backfill_scheduler import DailyBackfillConfig
from app.services.backfill_engine import BackfillRunner
from app.services.trading_suspensions import SuspensionCalendar
from app.services.mysql_instrument_repository import MySQLRepositoryError
from test_batch_candle_planning import batch_job,BatchFake
from test_incremental_backfill import plan,FakeControl


class PrepareAllTests(unittest.TestCase):
    def test_http_query_uses_singular_interval_and_parses_force(self):
        app=FastAPI(); app.include_router(router)
        response=dict(force=True,plan_saved=True,market_nos=[0,1,4],instrument_table="instruments",
            candle_table="candles",job_table="candle_backfill_jobs",interval="week",
            start_date="2010-01-01",end_date="2026-10-16",target_bars=560,eligible_symbols=3,
            created_jobs=3,resumed_jobs=0,storage={},message="prepared")
        with patch("app.api.routes._prepare_backfill",return_value=response) as helper:
            for kind,markets in (("board-lot",[0,1,4]),("odd-lot",[5,6])):
                status,body=asyncio.run(asgi_request(app,"POST",f"/api/v1/{kind}/backfill/prepare?interval=week&force=true"))
                self.assertEqual(status,200)
                self.assertTrue(body["force"] and body["plan_saved"])
                self.assertEqual(helper.call_args.args[0],markets)
                self.assertEqual(helper.call_args.args[4:],("week",None,None,None,True))

    def repository(self,interval="week"):
        repo=Mock()
        repo.plan_batch.return_value=dict(queued_jobs=2,start_date=date(2010,1,1),end_date=date(2026,10,16))
        repo.plan_incremental.return_value=dict(queued_jobs=0,start_date=date(2026,10,16))
        repo.get_candle_storage_summary.return_value=dict(eligible_symbols=6,by_interval={interval:{}})
        return repo

    @patch.dict(os.environ,{},clear=True)
    def test_both_endpoints_force_all_markets_and_save_paused_scope(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            repo=self.repository()
            with patch("app.api.routes.MySQLCandleRepository",return_value=repo):
                board=prepare_board_lot_backfill("week",None,None,date(2026,10,16),True)
                odd=prepare_odd_lot_backfill("week",None,None,date(2026,10,16),True)
            self.assertTrue(board.force and board.plan_saved and odd.force and odd.plan_saved)
            self.assertEqual([call.args[0] for call in repo.plan_batch.call_args_list],[0,1,4,5,6])
            for call in repo.plan_batch.call_args_list:
                self.assertEqual(call.args[3],560)
                self.assertTrue(call.kwargs["force"])
                self.assertEqual(call.kwargs["explicit_start"],initial_start("week",date(2026,10,16),560))
            self.assertEqual(repo.plan_batch.call_args_list[-1].args[4:7],("odd_lot_candle","odd_lot_candle_backfill_jobs","odd_lot_instruments"))
            state=BackfillControl().snapshot()
            self.assertEqual(state["state"],"prepared")
            self.assertTrue(state["pause_requested"])
            self.assertEqual(len(state["plans"]),5)
            self.assertTrue(all(p["fixed_range"] and not p["force"] for p in state["plans"]))
            repo.plan_incremental.assert_not_called()

    def test_repeated_prepare_replaces_group_not_duplicates_and_preserves_memory_gate(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl(); control.update(pause_kind="memory",state="paused",memory_retry_after="2026-10-17T12:00:00+00:00")
            item=dict(market_no=0,interval="day",target_bars=1200)
            control.save_prepared_plans([item]); control.save_prepared_plans([item])
            state=control.snapshot()
            self.assertEqual(len(state["plans"]),1)
            self.assertEqual(state["pause_kind"],"memory")
            self.assertEqual(state["memory_retry_after"],"2026-10-17T12:00:00+00:00")
            self.assertTrue(state["pause_requested"])

    def test_prepare_while_worker_active_rejected_before_mysql(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl(); control.update(plans=[dict(market_no=0,interval="day")])
            repo=self.repository()
            with control.worker(),patch("app.api.routes.MySQLCandleRepository",return_value=repo):
                with self.assertRaises(HTTPException) as caught:
                    prepare_board_lot_backfill("week",None,None,date(2026,10,16),True)
            self.assertEqual(caught.exception.status_code,409)
            repo.plan_batch.assert_not_called()
            self.assertEqual(control.snapshot()["plans"],[dict(market_no=0,interval="day")])

    def test_failed_prepare_remains_paused_and_does_not_start_fetching(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            repo=self.repository(); repo.plan_batch.side_effect=MySQLRepositoryError("test database failure")
            with patch("app.api.routes.MySQLCandleRepository",return_value=repo):
                with self.assertRaises(HTTPException) as caught:
                    prepare_board_lot_backfill("week",None,None,date(2026,10,16),True)
            self.assertEqual(caught.exception.status_code,503)
            self.assertTrue(BackfillControl().snapshot()["pause_requested"])

    @patch.dict(os.environ,{},clear=True)
    def test_default_prepare_remains_incremental_without_force(self):
        repo=self.repository()
        with patch("app.api.routes.MySQLCandleRepository",return_value=repo):
            result=prepare_board_lot_backfill("week",None,None,date(2026,10,16))
        self.assertFalse(result.force or result.plan_saved)
        repo.plan_batch.assert_not_called()
        self.assertEqual(repo.plan_incremental.call_count,3)

    def test_fixed_range_resume_next_day_does_not_replan_or_reset_checkpoint(self):
        item=batch_job(next_date=date(2026,10,15))
        scope=dict(plan(item),as_of_date=item["end_date"],fixed_range=True,batch_v2=True,
            target_bars=1200,target_source="override",planner_version=3,
            planned_on=(date.today()-timedelta(days=1)).isoformat(),instrument_table="instruments")
        repo=BatchFake([item]); vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[]))
        runner=BackfillRunner(vendor,repo,FakeControl()); runner.suspensions=SuspensionCalendar(); runner.suspension_day=date.today()
        with patch.object(runner,"memory_check",return_value=False):
            result=runner.run([scope])
        self.assertEqual(repo.planned,[])
        self.assertEqual(vendor.fetch_historical_candles.call_args.args[2],date(2026,10,15))
        self.assertEqual(result["completed"],1)

    def test_daily_schedule_preserves_unfinished_full_requery_before_switch(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl(); control.update(state="waiting_backfill",plans=[dict(fixed_range=True)])
            from app.api.routes import BACKFILL_WORKER_LOCK
            with BACKFILL_WORKER_LOCK, patch("app.api.routes.MySQLCandleRepository") as repo:
                state,_=run_scheduled_backfill(SimpleNamespace(quote_ready=True),DailyBackfillConfig.from_env())
            self.assertEqual(state,"queued"); repo.assert_not_called()
            self.assertEqual(control.snapshot()["plans"],[dict(fixed_range=True)])
