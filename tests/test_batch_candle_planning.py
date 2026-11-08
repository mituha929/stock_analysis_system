import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services.batch_candle_planning import choose_work
from app.services.backfill_engine import BackfillRunner
from app.services.trading_suspensions import SuspensionCalendar, Halt
from app.services.capital_history_connection import CapitalHistoryConnectionError
from app.services.mysql_instrument_repository import MySQLRepositoryError
from test_incremental_backfill import FakeControl, FakeRepository, job, plan, candle


class BatchPolicyTests(unittest.TestCase):
    def test_short_existing_history_fetches_newest_then_only_before_earliest(self):
        work=choose_work("60m",date(2026,10,14),1180,date(2025,10,23),date(2026,10,12),{},1200)
        self.assertEqual(work["next_date"],date(2026,10,12))
        self.assertEqual(work["history_next"],date(2025,10,22))
        self.assertTrue(work["needs_history"])

    def test_no_trade_but_checked_stock_does_not_drag_query_start_back(self):
        work=choose_work("day",date(2026,10,15),1200,date(2020,1,1),date(2026,8,1),
                         {"initialized":True,"checked_through":date(2026,10,14)},1200)
        self.assertEqual(work["next_date"],date(2026,10,14))

    def test_new_listing_exhausted_history_not_reinitialized_daily(self):
        work=choose_work("day",date(2026,10,15),8,date(2026,10,1),date(2026,10,14),
                         {"initialized":True,"verified_through":date(2026,10,14)},1200)
        self.assertFalse(work["needs_history"])

    def test_already_checked_latest_still_can_have_separate_history_queue(self):
        work=choose_work("day",date(2026,10,15),100,date(2026,1,1),date(2026,10,14),
                         {"checked_through":date(2026,10,15)},1200)
        self.assertEqual(work["phase"],"history")
        self.assertEqual(work["next_date"],date(2025,12,31))

    def test_initialization_target_all_intervals_stays_1200(self):
        for interval in ("1m","5m","15m","60m","day","week","month"):
            work=choose_work(interval,date(2026,9,30),0,None,None,{},1200)
            self.assertTrue(work["needs_history"])
            self.assertEqual(work["phase"],"latest")
            self.assertEqual(work["next_date"],date(2026,9,1) if interval=="month" else date(2026,9,28) if interval=="week" else date(2026,9,30))


class BatchFake(FakeRepository):
    def __init__(self, items):
        super().__init__(items)
        self.order=[]; self.checks={}; self.api_checks={}; self.skips=[]; self.planned=[]
    def plan_batch(self, market, interval, end, target, *_args, **_kwargs):
        self.planned.append((market,interval,end))
        return dict(start_date=end,end_date=end,queued_jobs=1,oldest_checked_through=None)
    def claim_incremental_job(self, scoped, owner):
        for item in self.items:
            if item["market_no"] != scoped["market_no"] or item["interval_code"] != scoped["interval"]: continue
            phase=item["phase"]
            if scoped.get("queue_phase")=="latest" and phase!="latest": continue
            if scoped.get("queue_phase") in {"catchup","history"} and phase!=scoped["queue_phase"]: continue
            if scoped.get("queue_phase")=="recovery" and phase=="latest": continue
            if item.get("last_round")==owner: continue
            if scoped.get("allowed_ids") is not None and item["id"] not in scoped["allowed_ids"]: continue
            if item["status"]=="pending":
                item.update(status="running",owner_token=owner)
                return item
    def stored_count(self, item): return item.get("count",1200)
    def commit_window(self,item,candles,next_date,empty,**metadata):
        if self.fail_commit: return super().commit_window(item,candles,next_date,empty)
        self.order.append((item["symbol"],item["phase"]))
        item.update(next_date=next_date,empty_windows=empty)
        if item["phase"]!="history":
            self.checks[item["symbol"]]=next_date-timedelta(days=1)
            if not metadata.get("skip_source"): self.api_checks[item["symbol"]]=self.checks[item["symbol"]]
        if metadata.get("skip_source"): self.skips.append(metadata)
        item["count"]=item.get("count",1200)+len(candles)
        return item["count"]
    def batch_latest_finished(self,item,count):
        if not item["needs_history"] or count>=item["target_bars"]: return False
        item.update(phase="history",status="pending",next_date=item["history_next"])
        return True
    def defer_batch_job(self,item,reason,delay_seconds):
        item.update(status="pending",last_round=item["owner_token"],phase="catchup" if item["phase"]=="latest" else item["phase"])
    def batch_pending(self,plans):
        # Like this fake's claim method, scope by market/interval/table. The
        # older fake jobs deliberately ignore calendar replanning/end dates.
        scopes={(p['market_no'],p['interval'],p['job_table']) for p in plans}
        return sum(item["status"]=="pending" and (item['market_no'],item['interval_code'],item['job_table']) in scopes for item in self.items),None


def batch_job(symbol="2330", interval="day", start=date(2026,10,14), end=date(2026,10,15), **options):
    item=job(interval=interval,start=start,end=end)
    item.update(symbol=symbol,phase="latest",needs_history=False,history_floor=date(2020,1,1),history_next=date(2025,1,1))
    item.update(options)
    return item


@patch.object(BackfillRunner,"memory_check",return_value=False)
class BatchWorkerTests(unittest.TestCase):
    def execute(self, items, vendor=None, plans=None, **options):
        repo=BatchFake(items); control=FakeControl()
        runner=BackfillRunner(vendor or SimpleNamespace(fetch_historical_candles=Mock(return_value=[])),repo,control)
        runner.suspensions=SuspensionCalendar(); runner.suspension_day=date.today()
        for key,value in options.items(): setattr(runner,key,value)
        scope=plans or [dict(plan(items[0]),batch_v2=True,planned_on=date.today().isoformat(),target_bars=1200,instrument_table="instruments")]
        result=runner.run(scope)
        return result,repo,control

    def test_all_groups_latest_pass_precedes_any_initialization(self,_memory):
        first=batch_job(count=0,mode="initialization",needs_history=True)
        second=batch_job("2317"); second["market_no"]=1
        plans=[dict(plan(item),batch_v2=True,planned_on=date.today().isoformat(),target_bars=1200,instrument_table="instruments") for item in (first,second)]
        _,repo,_=self.execute([first,second],plans=plans)
        self.assertEqual(repo.order[:2],[("2330","latest"),("2317","latest")])
        self.assertIn(("2330","history"),repo.order[2:])

    def test_long_unknown_gap_yields_and_keeps_checkpoint_not_failed(self,_memory):
        first=batch_job(interval="1m",start=date(2026,7,1))
        second=batch_job("2317",interval="1m"); second["id"]=2
        result,repo,control=self.execute([first,second],job_window_budget=2)
        self.assertEqual(first["next_date"],date(2026,7,15))
        self.assertEqual(first["phase"],"catchup")
        self.assertEqual(result["failed"],0)
        self.assertEqual(second["status"],"completed")
        self.assertEqual(control.state["state"],"waiting_backfill")

    def test_valid_empty_is_checked_even_when_latest_candle_does_not_move(self,_memory):
        result,repo,_=self.execute([batch_job()])
        self.assertEqual(repo.checks["2330"],date(2026,10,15))
        self.assertEqual(repo.api_checks["2330"],date(2026,10,15))
        self.assertEqual(result["completed"],1)

    def test_halt_evidence_skip_does_not_fake_api_verification(self,_memory):
        cal=SuspensionCalendar([Halt(0,"2330",date(2026,10,14),date(2026,10,15),"official",date.today())])
        vendor=SimpleNamespace(fetch_historical_candles=Mock())
        _,repo,_=self.execute([batch_job()],vendor,suspensions=cal)
        vendor.fetch_historical_candles.assert_not_called()
        self.assertEqual(repo.api_checks,{})
        self.assertEqual(repo.skips[0]["skip_source"],"official")

    def test_only_before_earliest_queried_in_history_phase(self,_memory):
        item=batch_job(count=1180,mode="initialization",needs_history=True,history_next=date(2025,1,1))
        fetch=Mock(return_value=[])
        self.execute([item],SimpleNamespace(fetch_historical_candles=fetch))
        self.assertLessEqual(fetch.call_args_list[1].args[3],date(2025,1,1))

    def test_actual_group_planning_is_lazy_and_once_on_same_day_resume(self,_memory):
        item=batch_job()
        scope=dict(plan(item),batch_v2=True,planned_on=None,target_bars=1200,instrument_table="instruments")
        _,repo,_=self.execute([item],plans=[scope])
        self.assertEqual(repo.planned[0][0:2],(0,"day"))
        self.assertEqual(scope["planned_on"],date.today().isoformat())
        self.assertFalse(scope["force"])
        self.assertEqual(scope["planner_version"],3)

    def test_catchup_in_later_market_precedes_earlier_market_history(self,_memory):
        older=batch_job(phase="history",needs_history=True,mode="initialization",count=10,
                        next_date=date(2025,1,1),history_next=date(2025,1,1))
        recent=batch_job("3085",phase="catchup"); recent.update(market_no=6,id=2)
        scopes=[dict(plan(item),batch_v2=True,planned_on=date.today().isoformat(),planner_version=3,
                     target_bars=1200,instrument_table="instruments") for item in (older,recent)]
        _,repo,_=self.execute([older,recent],plans=scopes)
        self.assertEqual(repo.order[0],("3085","catchup"))

    def test_empty_history_defers_with_cursor_instead_of_claiming_no_older_data(self,_memory):
        item=batch_job(phase="history",needs_history=True,mode="initialization",count=1185,
                       next_date=date(2025,10,22),history_next=date(2025,10,22),history_floor=date(2020,1,1))
        with patch.dict('os.environ', {'CAPITAL_DAY_INTEGRITY_ENABLED': 'false'}):
            result,repo,_=self.execute([item],job_window_budget=10)
        self.assertEqual(result["insufficient_history"],0)
        self.assertEqual(item["status"],"pending")
        self.assertLess(item["next_date"],date(2025,10,22))
        self.assertEqual(item["empty_windows"],3)

    def test_connection_failure_does_not_advance_checked_date(self,_memory):
        item=batch_job()
        vendor=SimpleNamespace(fetch_historical_candles=Mock(side_effect=CapitalHistoryConnectionError("offline")))
        result,repo,_=self.execute([item],vendor)
        self.assertEqual(result["pause_kind"],"connection")
        self.assertEqual(repo.checks,{})
        self.assertEqual(item["next_date"],date(2026,10,14))

    def test_global_recovery_budget_preserves_waiting_jobs(self,_memory):
        first=batch_job(count=1,needs_history=True,mode="initialization")
        second=batch_job("2317",count=1,needs_history=True,mode="initialization"); second["id"]=2
        result,repo,_=self.execute([first,second],recovery_budget=1)
        self.assertEqual(sum(phase=="history" for _,phase in repo.order),1)
        self.assertEqual(result["deferred"],2)
        self.assertEqual(result["failed"],0)

    def test_max_jobs_limits_admitted_stocks_not_only_finished_history(self,_memory):
        first=batch_job(count=1,needs_history=True,mode="initialization")
        second=batch_job("2317"); second["id"]=2
        result,repo,_=self.execute([first,second],max_jobs=1)
        self.assertTrue(result["limited"])
        self.assertNotIn(("2317","latest"),repo.order)
        self.assertEqual(second["status"],"pending")

    def test_only_latest_checked_target_complete_is_fully_skipped(self,_memory):
        self.assertIsNone(choose_work("day",date(2026,10,15),1200,date(2020,1,1),date(2026,10,1),
                                     {"checked_through":date(2026,10,15)},1200))
