"""Closed daily inputs, current aggregate snapshots, overwrite and recovery."""
from datetime import date, datetime, timedelta, timezone
import os
import unittest
from unittest.mock import Mock, patch

from app.schemas.models import Candle
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_policy import refresh_start
from app.services.backfill_scheduler import DailyBackfillConfig, DailyBackfillScheduler
from app.services.capital_history_connection import CapitalHistoryConnectionError
from app.services.candle_validation import CandleDataError
from app.services.daily_candle_aggregation import (
    aggregate_daily, aggregate_source, daily_aggregate_end, normalized_daily,
    COMPLETE_SOURCE, PARTIAL_SOURCE, MAX_VOLUME, TAIPEI,
)
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.taiwan_trading_calendar import trading_day_status
from app.services.trading_suspensions import SuspensionCalendar
from test_batch_candle_planning import batch_job
from test_daily_refresh import RefreshFake
from test_incremental_backfill import FakeControl, plan


TODAY = date(2026, 10, 20)


def bar(day, *, price=10, volume=100):
    return Candle(timestamp=datetime.combine(day, datetime.min.time(), tzinfo=TAIPEI),
                  open=price, high=price+2, low=price-1, close=price+1, volume=volume)


def daily_rows(first, last):
    return [bar(first+timedelta(days=i)) for i in range((last-first).days+1)
            if trading_day_status(first+timedelta(days=i))[0]]


class AggregationPolicyTests(unittest.TestCase):
    def test_weekly_ohlcv_includes_current_week_and_keeps_period_start_key(self):
        first, second = bar(date(2026,10,19)), bar(TODAY, price=15, volume=200)
        result = aggregate_daily([second, first, first], "week", date(2026,10,19), TODAY)
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row.timestamp.date(), date(2026,10,19))
        self.assertEqual((row.open,row.high,row.low,row.close,row.volume,row.source_bars), (10,17,9,16,300,2))

    def test_monthly_groups_september_and_october_without_combining_months(self):
        result = aggregate_daily([bar(date(2026,9,30)),bar(date(2026,10,1),price=20),bar(TODAY,price=30)],
                                 "month", date(2026,9,1), TODAY)
        self.assertEqual([row.timestamp.date() for row in result], [date(2026,9,1),date(2026,10,1)])
        self.assertEqual((result[1].open,result[1].close,result[1].volume), (20,31,200))

    def test_full_period_is_required_and_empty_source_does_not_fabricate_a_bar(self):
        with self.assertRaises(ValueError):
            aggregate_daily([bar(TODAY)], "month", date(2026,10,10), TODAY)
        self.assertEqual(aggregate_daily([], "month", date(2026,10,1), TODAY), [])

    def test_conflicting_daily_rows_abort_instead_of_choosing_arbitrarily(self):
        with self.assertRaisesRegex(ValueError, "衝突"):
            aggregate_daily([bar(TODAY),bar(TODAY,volume=1)], "week", date(2026,10,19), TODAY)

    def test_volume_is_exact_above_float_integer_precision_and_overflow_is_rejected(self):
        values = [bar(date(2026,10,19),volume=2**53+1),bar(TODAY,volume=2)]
        self.assertEqual(aggregate_daily(values,"week",date(2026,10,19),TODAY)[0].volume,2**53+3)
        with self.assertRaises(CandleDataError):
            aggregate_daily([bar(date(2026,10,19),volume=MAX_VOLUME),bar(TODAY,volume=1)],
                            "week",date(2026,10,19),TODAY)
        with self.assertRaises(CandleDataError):
            normalized_daily([bar(TODAY,volume=-1)],TODAY,TODAY)

    def test_incomplete_daily_bar_is_excluded_before_close_even_for_explicit_today(self):
        before = datetime(2026,10,20,11,tzinfo=TAIPEI)
        after = datetime(2026,10,20,18,30,tzinfo=TAIPEI)
        self.assertEqual(daily_aggregate_end(TODAY,now=before),date(2026,10,19))
        self.assertEqual(daily_aggregate_end(TODAY,now=after),TODAY)
        self.assertEqual(daily_aggregate_end(date(2026,10,25),now=after),TODAY)

    def test_partial_snapshot_never_promotes_merely_because_time_passed(self):
        later = datetime(2026,11,1,tzinfo=timezone.utc)
        self.assertEqual(aggregate_source("week",date(2026,10,19),TODAY,later),PARTIAL_SOURCE)
        self.assertEqual(aggregate_source("month",date(2026,10,1),TODAY,later),PARTIAL_SOURCE)
        self.assertEqual(aggregate_source("month",date(2026,9,1),date(2026,9,30),later),COMPLETE_SOURCE)

    @patch.dict(os.environ, {}, clear=True)
    def test_scheduler_rebuilds_all_intervals_on_tuesday_without_old_checkpoint_suppression(self):
        config = DailyBackfillConfig.from_env()
        scheduler = DailyBackfillScheduler(config, Mock())
        scheduler._load_checkpoint = lambda: {"last_successful_week:0":"2026-W42", "last_successful_month:0":"2026-09"}
        intervals, reason, periods = scheduler._select_intervals(TODAY)
        self.assertEqual(intervals, ("day","1m","5m","15m","60m","week","month"))
        self.assertIn("日 K 彙整",reason)
        self.assertEqual(periods,{})
        self.assertEqual(scheduler.snapshot()["aggregate_week_month_from_day"],True)
        self.assertEqual(config.intervals_for(TODAY)[0],intervals)

    @patch.dict(os.environ, {}, clear=True)
    def test_daily_week_month_targets_now_include_current_period(self):
        self.assertEqual(refresh_start("week",TODAY,5),date(2026,9,21))
        self.assertEqual(refresh_start("month",TODAY,2),date(2026,9,1))

    @patch.dict(os.environ, {}, clear=True)
    def test_api_derived_plans_end_today_while_legacy_history_keeps_completed_period(self):
        from app.api.routes import _plan_stale_backfill
        with patch("app.api.routes.BackfillControl"), patch("app.services.daily_candle_aggregation.daily_aggregate_end",return_value=TODAY):
            _, work, _, _ = _plan_stale_backfill([0],["week","month"],None,True,today=TODAY,
                                               aggregate_from_day=True,owns_worker_lock=True)
        self.assertTrue(all(row["end_date"]==TODAY and row["aggregate_from_day"] for row in work))


class AggregateFake(RefreshFake):
    def __init__(self, items, events):
        super().__init__(items)
        self.events = events
        self.daily = {}
        self.aggregates = {}
        self.fail_source = False

    def commit_daily_source(self, item, candles):
        if self.fail_source:
            raise MySQLRepositoryError("source rollback")
        self.daily.update({bar.timestamp.date():bar for bar in candles})
        self.events.append("source_commit")

    def commit_window(self, item, candles, next_date, empty, **kwargs):
        count = super().commit_window(item,candles,next_date,empty,**kwargs)
        self.aggregates.update({row.timestamp.date():row for row in candles})
        self.events.append("aggregate_commit")
        return count


class Vendor:
    cache = None
    def __init__(self, records, events):
        self.records, self.events = records, events
        self.pending = False
        self.calls = []
        self.on_fetch = None

    def fetch_historical_candles(self, symbol, interval, first, last, **kwargs):
        assert interval == "day" and kwargs["cache_result"] is False
        assert not self.pending, "Previous native response must be committed/acknowledged first"
        self.calls.append((first,last)); self.events.append("fetch")
        if self.on_fetch:
            self.on_fetch(self)
        self.pending = True
        return [row for row in self.records if first <= row.timestamp.date() <= last]

    def history_checkpoint(self, committed):
        assert self.pending
        self.pending = False
        self.events.append("ack_commit" if committed else "ack_rollback")


@patch.object(BackfillRunner,"memory_check",return_value=False)
class AggregateWorkerTests(unittest.TestCase):
    def execute(self, interval="week", target=5, *, repo=None, vendor=None, control=None):
        item = batch_job(interval=interval,start=date(2020,1,1),end=TODAY,mode="day_aggregate",
                         target_bars=target,next_date=TODAY,received_count=0)
        events = []
        repo = repo or AggregateFake([item],events)
        vendor = vendor or Vendor(daily_rows(date(2026,9,1),TODAY),repo.events)
        control = control or FakeControl()
        item = repo.items[0]
        scope = dict(plan(item),batch_v2=True,planned_on=date.today().isoformat(),planner_version=4,
                     as_of_date=TODAY,instrument_table="instruments",aggregate_from_day=True,
                     target_bars=560 if interval=="week" else 100,refresh_bars=item["target_bars"])
        runner = BackfillRunner(vendor,repo,control)
        runner.suspensions = SuspensionCalendar(); runner.suspension_day = date.today()
        with patch("app.services.backfill_engine.daily_aggregate_end",return_value=TODAY):
            result = runner.run([scope])
        return result,repo,vendor,control

    def test_weekly_current_period_overwrites_from_fresh_daily_not_native_weekly(self,_memory):
        result,repo,vendor,_control = self.execute()
        self.assertEqual(result["completed"],1)
        self.assertEqual(vendor.calls,[(date(2026,9,21),TODAY)])
        self.assertEqual(len(repo.aggregates),5)
        self.assertEqual(repo.aggregates[date(2026,10,19)].source_bars,2)
        self.assertEqual(repo.events,["fetch","source_commit","ack_commit","aggregate_commit"])

    def test_month_needs_more_than_ten_source_days_and_includes_october(self,_memory):
        result,repo,vendor,_control = self.execute("month",2)
        self.assertEqual(result["completed"],1)
        self.assertEqual(vendor.calls,[(date(2026,9,1),TODAY)])
        self.assertGreater(len(repo.daily),10)
        self.assertEqual(set(repo.aggregates),{date(2026,9,1),date(2026,10,1)})

    @patch.dict(os.environ,{"CAPITAL_KLINE_MAX_DAYS_DAY":"1"})
    def test_small_daily_windows_commit_before_next_query_and_do_not_double_ack(self,_memory):
        result,repo,vendor,_control = self.execute(target=1)
        self.assertEqual(result["completed"],1)
        self.assertEqual(vendor.calls,[(date(2026,10,19),date(2026,10,19)),(TODAY,TODAY)])
        self.assertEqual(repo.events,["fetch","source_commit","ack_commit","fetch","source_commit","ack_commit","aggregate_commit"])

    def test_repeated_execution_replaces_values_instead_of_adding_volume(self,_memory):
        _,repo,vendor,control = self.execute(target=1)
        key = date(2026,10,19)
        item = repo.items[0]
        for corrected in (False,True):
            if corrected:
                vendor.records = [bar(date(2026,10,19)),bar(TODAY,price=20,volume=7)]
            item.update(status="pending",received_count=0,next_date=TODAY)
            repo.events.clear()
            self.execute(target=1,repo=repo,vendor=vendor,control=control)
            self.assertEqual(len(repo.aggregates),1)
            self.assertEqual(repo.aggregates[key].volume,107 if corrected else 200)
        self.assertEqual(repo.aggregates[key].close,21)

    def test_source_rollback_releases_pin_without_advancing_aggregate_cursor(self,_memory):
        item = batch_job(interval="week",mode="day_aggregate",end=TODAY,next_date=TODAY,target_bars=1,received_count=0)
        repo = AggregateFake([item],[]); repo.fail_source=True
        result,repo,_vendor,_control = self.execute(target=1,repo=repo)
        self.assertEqual(result["pause_kind"],"database")
        self.assertEqual(repo.aggregates,{})
        self.assertEqual(item["next_date"],TODAY)
        self.assertEqual(repo.events,["fetch","ack_rollback"])

    def test_aggregate_rollback_preserves_old_values_and_source_rows(self,_memory):
        item = batch_job(interval="week",mode="day_aggregate",end=TODAY,next_date=TODAY,target_bars=1,received_count=0)
        repo = AggregateFake([item],[]); repo.fail_commit=True
        old = bar(date(2026,10,19),volume=999); repo.aggregates[date(2026,10,19)]=old
        result,repo,_vendor,_control = self.execute(target=1,repo=repo)
        self.assertEqual(result["pause_kind"],"database")
        self.assertEqual(repo.aggregates[date(2026,10,19)].volume,999)
        self.assertEqual(len(repo.daily),2)
        self.assertEqual(item["received_count"],0)
        self.assertEqual(item["next_date"],TODAY)

    @patch.dict(os.environ,{"CAPITAL_KLINE_MAX_DAYS_DAY":"1"})
    def test_connection_loss_after_source_commit_keeps_old_aggregate_and_cursor(self,_memory):
        events=[]; vendor=Vendor(daily_rows(date(2026,10,19),TODAY),events)
        def fail_second(current):
            if len(current.calls)==2: raise CapitalHistoryConnectionError("offline source")
        vendor.on_fetch=fail_second
        result,repo,_vendor,_control=self.execute(target=1,vendor=vendor)
        self.assertEqual(result["pause_kind"],"connection")
        self.assertEqual(len(repo.daily),1)
        self.assertEqual(repo.aggregates,{})
        self.assertEqual(repo.items[0]["next_date"],TODAY)

    @patch.dict(os.environ,{"CAPITAL_KLINE_MAX_DAYS_DAY":"1"})
    def test_manual_pause_saves_source_but_does_not_write_half_a_period(self,_memory):
        control=FakeControl(); vendor=Vendor(daily_rows(date(2026,10,19),TODAY),[])
        vendor.on_fetch=lambda _vendor:control.pause(True)
        result,repo,_vendor,_control=self.execute(target=1,vendor=vendor,control=control)
        self.assertEqual(result["pause_kind"],"manual")
        self.assertEqual(len(repo.daily),1)
        self.assertEqual(repo.aggregates,{})
        self.assertEqual(repo.items[0]["next_date"],TODAY)

    def test_memory_pause_commits_source_and_keeps_aggregate_cursor(self,_memory):
        with patch.object(BackfillRunner,"stage_check",side_effect=lambda stage:stage=="after_daily_source_before_write"):
            result,repo,vendor,_control=self.execute(target=1)
        self.assertEqual(result["pause_kind"],"memory")
        self.assertEqual(len(repo.daily),2)
        self.assertEqual(repo.aggregates,{})
        self.assertEqual(repo.items[0]["next_date"],TODAY)
        self.assertFalse(vendor.pending)
        self.assertEqual(repo.events,["fetch","source_commit","ack_commit"])


class AggregateRepositoryTests(unittest.TestCase):
    def test_newer_snapshot_cannot_be_overwritten_by_older_paused_work(self):
        repo=MySQLCandleRepository(); cursor=Mock()
        cursor.fetchone.return_value=(TODAY,)
        candle=aggregate_daily([bar(date(2026,10,19))],"week",date(2026,10,19),date(2026,10,19))[0]
        repo._write_daily_aggregates(cursor,"candles",dict(symbol="2330",interval_code="week",end_date=date(2026,10,19)),
                                     [candle],datetime(2026,10,20,tzinfo=timezone.utc))
        cursor.executemany.assert_not_called()

    def test_same_asof_recalculation_overwrites_all_ohlcv_and_records_partial(self):
        repo=MySQLCandleRepository(); cursor=Mock(); cursor.fetchone.return_value=(TODAY,)
        cursor.fetchall.return_value=[]
        candle=aggregate_daily([bar(TODAY)],"week",date(2026,10,19),TODAY)[0]
        repo._write_daily_aggregates(cursor,"candles",dict(symbol="2330",interval_code="week",end_date=TODAY),
                                     [candle],datetime(2026,10,20,tzinfo=timezone.utc))
        sql,values=cursor.executemany.call_args.args
        self.assertIn("ON DUPLICATE KEY UPDATE",sql)
        for column in ("open_price","high_price","low_price","close_price","volume","source","fetched_at"):
            self.assertIn(f"{column}=VALUES({column})",sql)
        self.assertEqual(values[0][-2],PARTIAL_SOURCE)
        self.assertEqual(cursor.execute.call_args.args[1][4:7],(TODAY,False,1))

    def test_partial_rows_are_excluded_from_completed_analysis_and_initialization(self):
        repo=MySQLCandleRepository(); cursor=Mock(); cursor.fetchmany.return_value=[]
        repo.aggregate_extents(cursor,"candles","week",["2330"],TODAY)
        self.assertIn("source<>'capital_day_partial'",cursor.execute.call_args.args[0])
        connection=Mock(); connection.cursor.return_value=cursor; cursor.fetchall.return_value=[]
        with patch.object(repo,"_connect_database",return_value=connection):
            repo.get_candles_with_metadata("2330","week")
        self.assertIn("source<>'capital_day_partial'",cursor.execute.call_args.args[0])


if __name__=="__main__":
    unittest.main()
