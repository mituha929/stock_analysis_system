import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime
from unittest.mock import Mock,patch

from app.services.backfill_status_cache import BackfillStatusCache
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings
from app.services.backfill_control import BackfillControl
from app.api.routes import _resume_saved_backfill


class StatusCacheTests(unittest.TestCase):
    def setUp(self):
        day=patch("app.api.routes.trading_day_status",return_value=(True,"交易日"))
        day.start(); self.addCleanup(day.stop)

    def test_simultaneous_readers_share_one_loader_and_return_copies(self):
        cache=BackfillStatusCache()
        entered=threading.Event(); release=threading.Event()
        calls=[]
        def load():
            calls.append(1); entered.set(); release.wait(2)
            return {"count":7}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures=[executor.submit(cache.get,"key",load) for _ in range(3)]
            self.assertTrue(entered.wait(1)); release.set()
            values=[future.result(timeout=3)[0] for future in futures]
        self.assertEqual(len(calls),1)
        values[0]["count"]=0
        self.assertEqual(values[1]["count"],7)

    def test_peek_is_nonblocking_bounded_and_reports_errors(self):
        cache=BackfillStatusCache(max_entries=2)
        release=threading.Event()
        loader=Mock(side_effect=lambda: (release.wait(2) and {"value":1}))
        self.assertEqual(cache.peek("key",loader)[:3],(None,0.0,True))
        cache.peek("key",loader)
        release.set(); cache.pending.join()
        self.assertEqual(loader.call_count,1)
        self.assertEqual(cache.peek("key",loader)[0],{"value":1})
        bad=Mock(side_effect=RuntimeError("test database offline"))
        cache.peek("bad",bad); cache.pending.join()
        value,_,loading,error=cache.peek("bad",bad)
        self.assertIsNone(value); self.assertFalse(loading)
        self.assertEqual(error,"test database offline")
        bad.assert_called_once()
        self.assertIsInstance(cache.errors["bad"][1],str)

    def test_completed_entries_do_not_grow_without_bound(self):
        cache=BackfillStatusCache(max_entries=2)
        for index in range(10):
            cache.peek(index,lambda:index); cache.pending.join()
        self.assertEqual(len(cache.entries),2)

    def test_actual_dates_use_one_grouped_index_query(self):
        repo=MySQLCandleRepository(MySQLSettings("unused",3306,"test","test","test"))
        connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchall.side_effect=[[(0,"2330"),(1,"6488")],
            [("2330","day",datetime(2026,10,15)),("6488","day",datetime(2026,10,14)),("9999","day",datetime(2026,1,1))]]
        with patch.object(repo,"_connector"),patch.object(repo,"_connect_database",return_value=connection):
            dates=repo.actual_data_through([0,1],["day"],"candles","instruments")
        self.assertEqual(dates[(0,"day")],date(2026,10,15))
        self.assertEqual(dates[(1,"day")],date(2026,10,14))
        self.assertEqual(cursor.execute.call_count,2)
        self.assertIn("GROUP BY symbol,interval_code",cursor.execute.call_args.args[0])
        self.assertNotIn("SELECT i.market_no,MIN((SELECT",cursor.execute.call_args.args[0])

    def test_background_resume_rebuilds_missing_plan(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl()
            control.request_resume({"markets":[0],"intervals":["day"]},1200)
            plan={"market_no":0,"interval":"day","end_date":date(2026,10,14)}
            with patch("app.api.routes._plan_stale_backfill",return_value=([],[plan],date(2026,10,14),"交易日")) as planner,patch("app.api.routes._run_stale_backfill_unlocked") as worker:
                _resume_saved_backfill(Mock(),[],1200)
            planner.assert_called_once_with([0],["day"],1200,True,
                owns_worker_lock=True,allow_non_trading_day=False)
            worker.assert_called_once()
            self.assertIsNone(control.snapshot()["resume_scope"])

    def test_concurrent_resume_requests_have_one_ticket(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            control=BackfillControl(Path(folder)/"control.sqlite3")
            control.snapshot()
            with ThreadPoolExecutor(max_workers=2) as executor:
                results=list(executor.map(lambda _:control.request_resume({"markets":[0],"intervals":["day"]},1200),range(2)))
            self.assertEqual(sum(created for _,created in results),1)
            self.assertEqual(results[0][0]["resume_token"],results[1][0]["resume_token"])

    def test_recovery_keeps_a_resume_request_without_saved_plans(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from app.services.backfill_control import BackfillRecovery
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            BackfillControl().request_resume({"markets":[0],"intervals":["day"]},1200)
            callback=Mock()
            recovery=BackfillRecovery(SimpleNamespace(quote_ready=True),callback,Mock())
            recovery.stop_event=Mock()
            recovery.stop_event.wait.side_effect=[False,True]
            with patch("app.services.taiwan_trading_calendar.trading_day_status",return_value=(True,"交易日")):
                recovery._run()
            callback.assert_called_once()
            self.assertEqual(callback.call_args.args[1],[])

    def test_background_resume_rechecks_holiday_before_planning(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl()
            control.request_resume({"markets":[0],"intervals":["day"]},1200)
            with patch("app.api.routes.trading_day_status",return_value=(False,"休市日")),patch("app.api.routes._plan_stale_backfill") as planner:
                _resume_saved_backfill(Mock(),[],1200)
            planner.assert_not_called()
            self.assertEqual(control.snapshot()["state"],"waiting_trading_day")
            self.assertIsNotNone(control.snapshot()["resume_scope"])

    def test_planning_database_failure_is_visible_and_keeps_scope(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from app.services.mysql_instrument_repository import MySQLRepositoryError
        with TemporaryDirectory() as folder,patch.dict("os.environ",{"CAPITAL_BACKFILL_CONTROL_DB":str(Path(folder)/"control.sqlite3")}):
            control=BackfillControl()
            control.request_resume({"markets":[0],"intervals":["day"]},1200)
            with patch("app.api.routes.trading_day_status",return_value=(True,"交易日")),patch("app.api.routes._plan_stale_backfill",side_effect=MySQLRepositoryError("test offline")):
                _resume_saved_backfill(Mock(),[],1200)
            state=control.snapshot()
            self.assertEqual(state["state"],"paused")
            self.assertEqual(state["pause_kind"],"database")
            self.assertIn("test offline",state["pause_reason"])
            self.assertIsNotNone(state["resume_scope"])
