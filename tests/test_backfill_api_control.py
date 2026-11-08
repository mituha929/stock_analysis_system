import asyncio
import threading
import unittest
import json
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch, Mock

from fastapi import FastAPI
from app.api.routes import router
from app.services.backfill_control import BackfillControl
from app.services.backfill_scheduler import DailyBackfillConfig, DailyBackfillScheduler


async def asgi_request(app,method,url):
    path,_,query=url.partition("?")
    messages=[]
    async def receive(): return {"type":"http.request","body":b"","more_body":False}
    async def send(message): messages.append(message)
    scope=dict(type="http",asgi={"version":"3.0"},http_version="1.1",method=method,
        path=path,raw_path=path.encode(),query_string=query.encode(),headers=[],scheme="http",
        server=("test",80),client=("test",123),root_path="")
    await app(scope,receive,send)
    status=next(m["status"] for m in messages if m["type"]=="http.response.start")
    body=b"".join(m.get("body",b"") for m in messages if m["type"]=="http.response.body")
    return status,json.loads(body)


class BackfillApiControlTests(unittest.TestCase):
    def test_day_integrity_report_is_read_only_scoped_and_paginated(self):
        with patch('app.api.routes.MySQLCandleRepository') as repository:
            repository.return_value.day_integrity_report.return_value = dict(storage_ready=True,summary=[],total=1,
                items=[dict(symbol='2330',candle_date='2026-10-19',status='unresolved')],assessments=[],assessment_total=1)
            status,body=asyncio.run(asgi_request(self.app(),'GET','/api/v1/backfill/day-integrity?markets=0,6&limit=50&offset=100'))
            self.assertEqual(status,200)
            repository.return_value.day_integrity_report.assert_called_once_with([0,6],limit=50,offset=100,include_resolved=False)
            self.assertEqual(body['items'][0]['candle_date'],'2026-10-19')
            repository.return_value.ensure_day_integrity_storage.assert_not_called()

    def test_day_integrity_report_rejects_unbounded_pages_and_unknown_market(self):
        for query in ('limit=1001','offset=-1','markets=99'):
            with patch('app.api.routes.MySQLCandleRepository') as repository:
                status,_=asyncio.run(asgi_request(self.app(),'GET','/api/v1/backfill/day-integrity?'+query))
                self.assertIn(status,(400,422))
                repository.assert_not_called()

    def setUp(self):
        # These control scenarios are trading-day tests, independent of the
        # calendar date on which the suite happens to be run.
        day=patch("app.api.routes.trading_day_status",return_value=(True,"交易日"))
        day.start(); self.addCleanup(day.stop)

    def app(self):
        app=FastAPI()
        app.include_router(router)
        app.state.capital_dll_async_lock=asyncio.Lock()
        app.state.capital_service=SimpleNamespace(quote_ready=True,
            fetch_historical_candles=lambda *_args,**_kwargs:[])
        return app

    def test_pause_and_resume_preserve_saved_scope_without_login(self):
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl()
            control.update(state="paused",source="cli",plans=[dict(market_no=0,interval="day",
                start_date="2026-10-01",end_date="2026-10-14",job_table="candle_backfill_jobs",candle_table="candles")])
            app=self.app()
            with patch("app.api.routes._resume_saved_backfill") as worker:
                self.assertEqual(asyncio.run(asgi_request(app,"POST","/api/v1/backfill/pause"))[0],200)
                self.assertTrue(control.snapshot()["pause_requested"])
                self.assertEqual(asyncio.run(asgi_request(app,"POST","/api/v1/backfill/resume"))[0],202)
                self.assertFalse(control.snapshot()["pause_requested"])
                self.assertEqual(worker.call_args.args[1][0]["end_date"],date(2026,10,14))

    def test_background_batch_does_not_hold_http_dll_queue_until_end(self):
        started=threading.Event(); release=threading.Event(); fetched=threading.Event()
        app=self.app()
        def batch(*_args):
            started.set(); release.wait(5)
        app.state.capital_service.fetch_historical_candles=lambda *_args,**_kwargs: (fetched.set() or [])
        async def exercise():
            first=asyncio.create_task(asgi_request(app,"POST","/api/v1/backfill/refresh-stale?markets=0&intervals=day"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,2))
                status,_data=await asyncio.wait_for(asgi_request(app,"GET","/api/v1/history/candles/2330?start_date=2026-10-01&end_date=2026-10-02"),2)
                self.assertEqual(status,200)
                self.assertTrue(fetched.is_set(),"背景整批工作不應持有 HTTP 操作鎖")
            finally:
                release.set()
                await first
        with patch("app.api.routes._plan_stale_backfill",return_value=([],[{}],date(2026,10,14),"交易日")),patch("app.api.routes._run_stale_backfill",side_effect=batch):
            asyncio.run(exercise())

    def test_control_remains_available_without_mysql(self):
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            BackfillControl().update(state="paused",pause_kind="database",pause_reason="MySQL offline")
            with patch("app.api.routes.MySQLCandleRepository",side_effect=AssertionError("must not need MySQL")):
                status,body=asyncio.run(asgi_request(self.app(),"GET","/api/v1/backfill/control"))
            self.assertEqual(status,200)
            self.assertEqual(body["pause_kind"],"database")
            self.assertFalse(body["worker_active"])

    def test_resume_without_local_plan_queues_selected_scope_once(self):
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            app=self.app()
            with patch("app.api.routes._resume_saved_backfill") as worker:
                status,body=asyncio.run(asgi_request(app,"POST","/api/v1/backfill/resume?markets=1&intervals=day&target_bars=1200"))
                self.assertEqual(status,202)
                self.assertIn("重建",body["message"])
                self.assertEqual(body["control"]["resume_scope"],{"markets":[1],"intervals":["day"]})
                asyncio.run(asgi_request(app,"POST","/api/v1/backfill/resume?markets=1&intervals=day"))
                worker.assert_called_once()

    def test_resume_without_quote_connection_explains_waiting(self):
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            app=self.app(); app.state.capital_service.quote_ready=False
            with patch("app.api.routes._resume_saved_backfill") as worker:
                status,body=asyncio.run(asgi_request(app,"POST","/api/v1/backfill/resume?markets=0&intervals=day"))
                self.assertEqual(status,202)
                self.assertEqual(body["control"]["state"],"waiting_connection")
                worker.assert_not_called()

    def test_status_response_does_not_wait_for_database_snapshot(self):
        from app.services.backfill_status_cache import BackfillStatusCache
        started=threading.Event(); release=threading.Event()
        app=self.app(); app.state.backfill_status_cache=BackfillStatusCache()
        def slow_load(*_args):
            started.set(); release.wait(3)
            return [],[],0.5
        with patch("app.api.routes._load_backfill_status",side_effect=slow_load),patch("app.api.routes.backfill_control_status",return_value={"control_version":2}):
            try:
                status,body=asyncio.run(asgi_request(app,"GET","/api/v1/backfill/status?markets=0&intervals=day"))
                self.assertEqual(status,200)
                self.assertTrue(body["database_refreshing"])
                self.assertTrue(started.wait(1))
                self.assertFalse(release.is_set(),"回應不應等待慢速資料庫")
            finally:
                release.set()
                app.state.backfill_status_cache.pending.join()

    def test_holiday_never_catches_up_week_month_until_next_trading_day(self):
        with patch.dict("os.environ",{},clear=True):
            scheduler=DailyBackfillScheduler(DailyBackfillConfig.from_env(),lambda _config:("completed","ok"))
        scheduler._load_checkpoint=lambda:{}
        self.assertEqual(scheduler._select_intervals(date(2026,10,9)),((),"台股休市日",{}))

    def test_changing_market_scope_does_not_skip_its_weekly_checkpoint(self):
        with patch.dict("os.environ",{"CAPITAL_DAILY_AGGREGATE_FROM_DAY":"false"},clear=True):
            scheduler=DailyBackfillScheduler(DailyBackfillConfig.from_env(),lambda _config:("completed","ok"))
        scheduler._load_checkpoint=lambda:{"last_successful_week":"2026-W40","last_successful_week:0":"2026-W40"}
        intervals,_reason,periods=scheduler._select_intervals(date(2026,10,7))
        self.assertIn("week",intervals)
        self.assertEqual(periods["last_successful_week:1"],"2026-W40")


class BackfillCliControlTests(unittest.TestCase):
    def test_cli_pause_does_not_open_capital_or_mysql(self):
        from scripts import backfill_market_candles as cli
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            with patch.object(cli,"load_dotenv"),patch.object(cli,"CapitalKLineSession") as session,patch("sys.argv",["backfill","--pause"]):
                self.assertEqual(cli.main(),0)
            self.assertTrue(BackfillControl().snapshot()["pause_requested"])
            session.assert_not_called()

    def test_cli_resume_preserves_scope_and_initialization_target(self):
        from scripts import backfill_market_candles as cli
        scope=dict(market_no=6,interval="day",start_date="2026-10-01",end_date="2026-10-14",
                   job_table="odd_lot_candle_backfill_jobs",candle_table="odd_lot_candle",
                   instrument_table="odd_lot_instruments")
        with TemporaryDirectory() as folder,patch.dict("os.environ",{
            "CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3"),
            "CAPITAL_USER_ID":"test-only", "CAPITAL_PASSWORD":"test-only", "CAPITAL_AUTHORITY":"0",
        }):
            control=BackfillControl()
            control.update(state="paused",source="cli",pause_requested=True,plans=[scope],target_bars=1200)
            lock=Mock(); lock.hold.return_value=nullcontext()
            with patch.object(cli,"load_dotenv"),patch.object(cli,"RecyclingKLineSession"),patch("sys.argv",["backfill","--resume"]),patch("app.services.capital_process_lock.get_capital_process_lock",return_value=lock),patch("app.services.taiwan_trading_calendar.trading_day_status",return_value=(True,"交易日")),patch("app.services.backfill_engine.BackfillRunner") as runner:
                runner.return_value.run.return_value=dict(processed=1,completed=1,insufficient_history=0,failed=0)
                self.assertEqual(cli.main(),0)
            plans,target=runner.return_value.run.call_args.args
            self.assertEqual(target,1200)
            self.assertEqual(plans[0]["market_no"],6)
            self.assertEqual(plans[0]["end_date"],date(2026,10,14))
            self.assertEqual(plans[0]["candle_table"],"odd_lot_candle")
            self.assertFalse(control.snapshot()["pause_requested"])
