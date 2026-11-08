import gc
import unittest
import weakref
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.schemas.models import Candle
from app.services.backfill_control import BackfillControl
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_policy import completed_end, period_start, select_range, request_interval, HistoryRequestLimiter, history_window, initial_start
from app.services.capital_history_connection import CapitalHistoryConnectionError
from app.services.capital_history_service import HistoryQuoteEvents, request_history_candles
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLSettings, MySQLRepositoryError
from app.services.quote_cache import QuoteCache
from app.services.process_memory import ProcessMemory


def candle(day=date(2026,10,1)):
    return Candle(timestamp=datetime.combine(day,datetime.min.time(),tzinfo=timezone.utc),open=10,high=12,low=9,close=11,volume=100)


def job(mode="incremental", interval="1m", start=date(2026,10,1), end=date(2026,10,15), job_id=1):
    return dict(id=job_id,market_no=0,symbol="2330",interval_code=interval,start_date=start,end_date=end,
                mode=mode,target_bars=1200,next_date=end if mode=="initialization" else start,empty_windows=0,
                status="pending",job_table="candle_backfill_jobs",candle_table="candles")


def plan(item):
    return dict(market_no=item["market_no"],interval=item["interval_code"],start_date=item["start_date"],
                end_date=item["end_date"],job_table=item["job_table"],candle_table=item["candle_table"])


class FakeControl:
    def __init__(self): self.state={"pause_requested":False}
    def snapshot(self): return dict(self.state)
    def update(self,**values): self.state.update(values); return self.snapshot()
    def pause(self,value): return self.update(pause_requested=value)
    @contextmanager
    def worker(self): yield


class FakeRepository:
    def __init__(self,items):
        self.items=items
        self.rows={}
        self.windows=[]
        self.finished=[]
        self.fail_commit=False
    def recover_incremental_jobs(self,_plan):
        for item in self.items:
            if item["status"]=="running": item["status"]="pending"
    def claim_incremental_job(self,_plan,owner):
        for item in self.items:
            if item["status"]=="pending":
                item.update(status="running",owner_token=owner)
                return item
    def stored_count(self,item): return len(self.rows)
    def commit_window(self,item,candles,next_date,empty):
        if self.fail_commit: raise MySQLRepositoryError("database offline before commit")
        self.rows.update({(item["symbol"],c.timestamp):c for c in candles})
        self.windows.append(next_date)
        item.update(next_date=next_date,empty_windows=empty)
        return len(self.rows)
    def finish_incremental_job(self,item,count,exhausted=False):
        status="insufficient_history" if item["mode"]=="initialization" and count<item["target_bars"] else "completed"
        item["status"]=status
        self.finished.append((item["id"],count,exhausted))
        return status
    def release_incremental_job(self,item,_reason): item["status"]="pending"
    def fail_incremental_job(self,item,_reason): item["status"]="failed"


class IncrementalPolicyTests(unittest.TestCase):
    def test_new_symbol_initializes_to_1200(self):
        mode,start=select_range("day",date(2026,10,14),0,None,None)
        self.assertEqual(mode,"initialization")
        self.assertLess(start,date(2022,1,1))

    def test_existing_history_only_overlaps_latest_stored_day(self):
        self.assertEqual(select_range("day",date(2026,10,14),1500,date(2026,10,12),None),
                         ("incremental",date(2026,10,12)))

    def test_exhausted_new_listing_does_not_initialize_again(self):
        state={"initialized":True,"verified_through":date(2026,10,12)}
        self.assertEqual(select_range("day",date(2026,10,14),3,date(2026,10,12),state),
                         ("incremental",date(2026,10,12)))

    def test_synced_period_is_skipped_unless_forced(self):
        state={"initialized":True,"verified_through":date(2026,10,14)}
        self.assertIsNone(select_range("day",date(2026,10,14),10,date(2026,10,14),state))
        self.assertIsNotNone(select_range("day",date(2026,10,14),10,date(2026,10,14),state,force=True))

    def test_week_and_month_include_today_when_today_is_final_trading_day(self):
        self.assertEqual(completed_end("week",date(2026,9,24)),date(2026,9,24))
        self.assertEqual(completed_end("month",date(2026,9,30)),date(2026,9,30))

    def test_unfinished_week_month_end_at_previous_completed_period(self):
        self.assertEqual(completed_end("week",date(2026,10,7)),date(2026,10,2))
        self.assertEqual(completed_end("month",date(2026,10,7)),date(2026,9,30))

    def test_overlap_uses_full_week_or_month(self):
        self.assertEqual(period_start("week",date(2026,9,24)),date(2026,9,21))
        self.assertEqual(period_start("month",date(2026,9,30)),date(2026,9,1))

    def test_long_week_month_queries_never_split_a_period(self):
        end=date(2026,9,30)
        for interval in ("week","month"):
            first=initial_start(interval,end)
            self.assertEqual(first,period_start(interval,first))
            for backward in (False,True):
                cursor=end if backward else first
                windows=[]
                while first <= cursor <= end:
                    start,finish=history_window(interval,first,end,cursor,backward,400)
                    self.assertLessEqual((finish-start).days+1,400)
                    self.assertEqual(start,period_start(interval,start))
                    if finish != end:
                        self.assertEqual(finish+timedelta(days=1),period_start(interval,finish+timedelta(days=1)))
                    windows.append((start,finish))
                    cursor=start-timedelta(days=1) if backward else finish+timedelta(days=1)
                windows.sort()
                self.assertEqual(windows[0][0],first)
                self.assertEqual(windows[-1][1],end)
                for left,right in zip(windows,windows[1:]):
                    self.assertEqual(left[1]+timedelta(days=1),right[0])

    def test_interval_validation_rejects_nan_infinity_and_too_small(self):
        for value in (float("nan"),float("inf"),0,-1,0.49):
            with self.assertRaises(ValueError): request_interval(value)
        self.assertEqual(request_interval(0.8),0.8)

    def test_rate_limit_is_per_request_not_per_bar(self):
        limiter=HistoryRequestLimiter()
        limiter.override=0.8
        with patch("app.services.backfill_policy.time.monotonic",side_effect=[10,10.2,10.3,10.9]),patch("app.services.backfill_policy.time.sleep") as sleep:
            with limiter.slot(): pass
            with limiter.slot(): pass
        self.assertAlmostEqual(sleep.call_args.args[0],0.7)


@patch.object(BackfillRunner,"memory_check",return_value=False)
class IncrementalWorkerTests(unittest.TestCase):
    def execute(self,item,capital,repository=None,control=None):
        repository=repository or FakeRepository([item])
        control=control or FakeControl()
        result=BackfillRunner(capital,repository,control).run([plan(item)])
        return result,repository,control

    def test_connection_error_pauses_without_failing_remaining_jobs(self,_memory):
        item=job()
        capital=SimpleNamespace(fetch_historical_candles=Mock(side_effect=CapitalHistoryConnectionError("offline")))
        result,repo,_=self.execute(item,capital)
        self.assertTrue(result["paused"])
        self.assertEqual(result["failed"],0)
        self.assertEqual(item["status"],"pending")
        self.assertEqual(repo.finished,[])

    def test_resume_starts_after_last_committed_window(self,_memory):
        item=job()
        repo=FakeRepository([item])
        control=FakeControl()
        fetch=Mock(side_effect=[[candle()],CapitalHistoryConnectionError("offline")])
        result,_,_=self.execute(item,SimpleNamespace(fetch_historical_candles=fetch),repo,control)
        self.assertTrue(result["paused"])
        self.assertEqual(item["next_date"],date(2026,10,8))
        fetch=Mock(return_value=[])
        result,_,_=self.execute(item,SimpleNamespace(fetch_historical_candles=fetch),repo,control)
        self.assertEqual(fetch.call_args_list[0].args[2],date(2026,10,8))
        self.assertEqual(result["completed"],1)
        self.assertEqual(len(repo.rows),1)

    def test_database_failure_does_not_advance_cursor(self,_memory):
        item=job()
        repo=FakeRepository([item]); repo.fail_commit=True
        result,_,_=self.execute(item,SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle()])),repo)
        self.assertEqual(result["pause_kind"],"database")
        self.assertEqual(item["next_date"],date(2026,10,1))
        self.assertEqual(repo.rows,{})

    def test_pause_waits_until_received_window_is_saved(self,_memory):
        item=job(); control=FakeControl()
        def fetch(*_args,**_kwargs):
            control.pause(True)
            return [candle()]
        result,repo,_=self.execute(item,SimpleNamespace(fetch_historical_candles=fetch),control=control)
        self.assertEqual(result["pause_kind"],"manual")
        self.assertEqual(len(repo.rows),1)
        self.assertEqual(item["next_date"],date(2026,10,8))

    def test_incremental_empty_response_is_completed_not_failed(self,_memory):
        item=job(interval="day")
        result,repo,_=self.execute(item,SimpleNamespace(fetch_historical_candles=Mock(return_value=[])))
        self.assertEqual(result["completed"],1)
        self.assertEqual(result["failed"],0)

    def test_initialization_empty_history_stops_after_bounded_probe(self,_memory):
        item=job("initialization","month",date(1900,1,1),date(2026,9,30))
        fetch=Mock(return_value=[])
        result,repo,_=self.execute(item,SimpleNamespace(fetch_historical_candles=fetch))
        self.assertEqual(fetch.call_count,3)
        self.assertEqual(result["insufficient_history"],1)
        self.assertTrue(repo.finished[0][2])

    def test_future_vendor_candle_is_not_saved(self,_memory):
        item=job(interval="day",end=date(2026,10,2))
        result,repo,_=self.execute(item,SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle(date(2026,10,15))])))
        self.assertEqual(repo.rows,{})
        self.assertEqual(result["completed"],1)

    def test_interrupted_running_job_recovers_its_existing_cursor(self,_memory):
        item=job(); item.update(status="running",next_date=date(2026,10,8))
        fetch=Mock(return_value=[])
        result,_,_=self.execute(item,SimpleNamespace(fetch_historical_candles=fetch))
        self.assertEqual(fetch.call_args_list[0].args[2],date(2026,10,8))
        self.assertEqual(result["completed"],1)

    def test_restart_resume_keeps_previous_failure_counts(self,_memory):
        item=job(interval="day")
        control=FakeControl()
        control.update(state="waiting_connection",plans=[plan(item)],processed=3,completed=2,
                       insufficient_history=0,failed=1)
        result,_,control=self.execute(item,SimpleNamespace(fetch_historical_candles=Mock(return_value=[])),control=control)
        self.assertEqual(result["processed"],4)
        self.assertEqual(result["completed"],3)
        self.assertEqual(result["failed"],1)
        self.assertEqual(control.snapshot()["state"],"error")


class PersistenceAndMemoryTests(unittest.TestCase):
    def test_memory_limit_pauses_before_claiming_or_querying(self):
        item=job()
        repository=FakeRepository([item])
        control=FakeControl()
        capital=SimpleNamespace(fetch_historical_candles=Mock(),cache=Mock())
        with patch("app.services.backfill_engine.current_process_memory",return_value=ProcessMemory(5000,5000)):
            result=BackfillRunner(capital,repository,control).run([plan(item)])
        self.assertEqual(result["pause_kind"],"memory")
        self.assertEqual(item["status"],"pending")
        capital.fetch_historical_candles.assert_not_called()
        capital.cache.prune_historical.assert_called()

    def test_control_survives_restart_and_keeps_only_small_progress_metadata(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/"control.sqlite3"
            control=BackfillControl(path)
            control.pause(True)
            control.update(plans=[{"start_date":date(2026,10,1),"end_date":date(2026,10,2)}])
            other=BackfillControl(path)
            self.assertTrue(other.snapshot()["pause_requested"])
            self.assertEqual(other.saved_plans()[0]["end_date"],date(2026,10,2))

    def test_worker_lock_prevents_second_worker(self):
        from app.services.capital_process_lock import CapitalProcessLockError
        with TemporaryDirectory() as folder:
            control=BackfillControl(Path(folder)/"control.sqlite3")
            with control.worker():
                self.assertTrue(control.worker_active())
                with self.assertRaises(CapitalProcessLockError):
                    with control.worker(): pass
            self.assertFalse(control.worker_active())

    def test_commit_window_rolls_back_rows_and_cursor_together(self):
        repo=MySQLCandleRepository(MySQLSettings("unused",3306,"test","test","test"))
        connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchone.return_value=("owner",)
        cursor.executemany.side_effect=RuntimeError("write failed")
        item=job(); item["owner_token"]="owner"
        with patch.object(repo,"_connector"),patch.object(repo,"_connect_database",return_value=connection):
            with self.assertRaises(MySQLRepositoryError):
                repo.commit_window(item,[candle()],date(2026,10,8),0)
        self.assertEqual(item["next_date"],date(2026,10,1))
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_wrong_owner_cannot_write_candles(self):
        repo=MySQLCandleRepository(MySQLSettings("unused",3306,"test","test","test"))
        connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchone.return_value=("other-owner",)
        item=job(); item["owner_token"]="owner"
        with patch.object(repo,"_connector"),patch.object(repo,"_connect_database",return_value=connection):
            with self.assertRaises(MySQLRepositoryError): repo.commit_window(item,[candle()],date(2026,10,8),0)
        cursor.executemany.assert_not_called()

    def test_expired_unread_cache_is_actively_pruned(self):
        cache=QuoteCache(historical_ttl_seconds=1)
        with patch("app.services.quote_cache.monotonic",return_value=10):
            cache.set_historical_candles("2330","day",[candle()])
        with patch("app.services.quote_cache.monotonic",return_value=12):
            self.assertEqual(cache.prune_historical(),1)
        self.assertEqual(len(cache._historical_candles),0)

    def test_received_rows_are_released_after_return(self):
        events=HistoryQuoteEvents(); events.connected.set()
        item=candle(); reference=weakref.ref(item)
        quote=Mock()
        def receive(*_args):
            events.records.append(item)
            events.completed.set()
            return 0
        quote.SKQuoteLib_RequestKLineAMByDate.side_effect=receive
        with patch("app.services.backfill_policy.time.sleep"):
            rows=request_history_candles(quote,Mock(),events,lambda:None,"2330","day",date(2026,10,1),date(2026,10,1),1)
        self.assertEqual(len(rows),1)
        self.assertEqual(events.records,[])
        rows.clear(); del item; gc.collect()
        self.assertIsNone(reference())
