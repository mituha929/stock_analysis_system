import unittest
from datetime import date,datetime,timedelta
from unittest.mock import Mock,patch

from app.api.routes import _load_backfill_status
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings


class BackfillAverageDurationTests(unittest.TestCase):
    def snapshot(self,rows):
        repo=MySQLCandleRepository(MySQLSettings("unused",3306,"test","test","test"))
        connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchall.side_effect=[[("candles",),("instruments",),("candle_backfill_jobs",)],[(0,len(rows))],rows]
        with patch.object(repo,"_connector"),patch.object(repo,"_connect_database",return_value=connection),patch.object(repo,"actual_data_through",return_value={}):
            result=repo.get_backfill_dashboard_status([0],["day"],"candles","candle_backfill_jobs","instruments")
        return result["items"][(0,"day")],cursor

    def row(self,number,state="completed",seconds=10,attempts=1):
        start=datetime(2026,10,15,10)
        return (0,"day",str(number),state,start+timedelta(minutes=1),None,number,
                start,start+timedelta(seconds=seconds) if seconds is not None else None,attempts)

    def test_average_uses_latest_completed_and_insufficient_jobs_only(self):
        item,cursor=self.snapshot([self.row(1,seconds=10),self.row(2,"insufficient_history",20),
            self.row(3,"pending",100),self.row(4,"running",100),self.row(5,"failed",100)])
        self.assertEqual(item["average_fetch_seconds"],15)
        self.assertEqual(item["average_fetch_sample_count"],2)
        self.assertEqual(item["tracked_symbols"],5)
        self.assertEqual(cursor.execute.call_count,3,"平均值不應新增全表查詢")
        self.assertIn("r.rn=1",cursor.execute.call_args.args[0])

    def test_missing_negative_and_resumed_durations_are_excluded(self):
        rows=[self.row(1,seconds=None),self.row(2,seconds=-10),self.row(3,seconds=20,attempts=2)]
        item,_=self.snapshot(rows)
        self.assertIsNone(item["average_fetch_seconds"])
        self.assertEqual(item["average_fetch_sample_count"],0)

    def test_zero_and_fractional_seconds_are_valid(self):
        item,_=self.snapshot([self.row(1,seconds=0),self.row(2,seconds=2.5)])
        self.assertEqual(item["average_fetch_seconds"],1.25)
        self.assertEqual(item["average_fetch_sample_count"],2)

    def test_groups_are_independent(self):
        repo=MySQLCandleRepository(MySQLSettings("unused",3306,"test","test","test"))
        connection=Mock(); cursor=connection.cursor.return_value
        other=list(self.row(2,seconds=30)); other[0]=1
        cursor.fetchall.side_effect=[[("candles",),("instruments",),("candle_backfill_jobs",)],[(0,1),(1,1)],[self.row(1,seconds=10),tuple(other)]]
        with patch.object(repo,"_connector"),patch.object(repo,"_connect_database",return_value=connection),patch.object(repo,"actual_data_through",return_value={}):
            items=repo.get_backfill_dashboard_status([0,1],["day"],"candles","candle_backfill_jobs","instruments")["items"]
        self.assertEqual(items[(0,"day")]["average_fetch_seconds"],10)
        self.assertEqual(items[(1,"day")]["average_fetch_seconds"],30)

    @patch("app.api.routes.MySQLCandleRepository")
    def test_status_api_exposes_average_and_sample_count(self,repository_type):
        raw,_=self.snapshot([self.row(1,seconds=12.5)])
        repository=repository_type.return_value
        repository.get_backfill_dashboard_status.return_value={"items":{(0,"day"):raw}}
        repository.list_daily_backfill_runs.return_value=[]
        items,_,_=_load_backfill_status([0],["day"])
        self.assertEqual(items[0].average_fetch_seconds,12.5)
        self.assertEqual(items[0].average_fetch_sample_count,1)

    def test_empty_scope_still_returns_no_average(self):
        item,_=self.snapshot([])
        self.assertIsNone(item["average_fetch_seconds"])
        self.assertEqual(item["average_fetch_sample_count"],0)

    def test_pending_phases_and_checked_dates_do_not_change_work_totals(self):
        rows=[(*self.row(1,"pending"),"latest",date(2026,10,14)),
              (*self.row(2,"pending"),"history",date(2026,10,15)),
              (*self.row(3,"pending"),"catchup",date(2026,10,13)),
              (*self.row(4,"pending"),None,None)]
        item,_=self.snapshot(rows)
        self.assertEqual(item["pending_phases"],dict(latest=1,history=1,catchup=1,unknown=1))
        self.assertEqual(item["job_counts"]["pending"],4)
        self.assertEqual(item["checked_through"],date(2026,10,13))
        self.assertEqual(item["checked_symbols"],3)
