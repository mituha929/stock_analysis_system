"""Day latest first, ALL normal intervals next, slow recovery/audits last."""
from datetime import date
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.api.routes import parse_refresh_intervals, run_scheduled_backfill
from app.services.backfill_engine import BackfillRunner
from app.services.backfill_scheduler import DailyBackfillConfig
from app.services.backfill_targets import prioritize_day
from app.services.candle_validation import CandleDataError
from scripts.backfill_market_candles import parse_intervals
from test_batch_candle_planning import BatchFake, batch_job
from test_incremental_backfill import FakeControl, candle, plan
from test_candle_data_failures import FailureFake

FIRST,END=date(2026,10,14),date(2026,10,15)


def scope(item,planned=True):
    return dict(plan(item),batch_v2=True,planned_on=date.today().isoformat() if planned else None,
                planner_version=3,as_of_date=END,fixed_range=True,
                target_bars=item['target_bars'],target_source='override',instrument_table='instruments')


class DayFirstPolicyTests(unittest.TestCase):
    def test_stable_partition_preserves_market_order_objects_and_other_intervals(self):
        work=[dict(interval=i,market_no=m) for i,m in [('1m',0),('day',1),('week',0),('day',0),('month',0)]]
        ordered=prioritize_day(work,key=lambda item:item['interval'])
        self.assertEqual([(p['interval'],p['market_no']) for p in ordered],
                         [('day',1),('day',0),('1m',0),('week',0),('month',0)])
        self.assertIs(ordered[0],work[1]); self.assertEqual(work[0]['interval'],'1m')

    def test_api_cli_and_scheduler_expose_day_first_without_adding_unselected_day(self):
        for parser in (parse_refresh_intervals,parse_intervals):
            self.assertEqual(parser('all')[0],'day')
            self.assertEqual(parser('week,day,1m'),['day','week','1m'])
            self.assertEqual(parser('week,month'),['week','month'])
        with patch.dict('os.environ',{},clear=True):
            config=DailyBackfillConfig.from_env()
        self.assertEqual(config.configured_intervals[0],'day')

    def test_scheduler_resumes_original_day_blocked_scope_without_replanning(self):
        control=Mock(); control.snapshot.return_value=dict(day_first=True,priority_phase='day',
            state='waiting_backfill',plans=[dict(interval='day')],pause_requested=False)
        control.worker.return_value=nullcontext()
        control.saved_plans.return_value=control.snapshot.return_value['plans']
        control.request_scheduled_priority.return_value=(control.snapshot.return_value,False)
        with patch('app.api.routes.BackfillControl',return_value=control), \
             patch('app.api.routes.MySQLCandleRepository') as repo, \
             patch('app.api.routes._run_stale_backfill_unlocked',return_value=dict(deferred=1)) as resume:
            config=DailyBackfillConfig.from_env()
            state,message=run_scheduled_backfill(SimpleNamespace(quote_ready=True),config)
        self.assertEqual(state,'waiting_backfill'); self.assertIn('待補齊留到最後',message)
        resume.assert_called_once_with(unittest.mock.ANY,control.saved_plans.return_value,1200,owns_worker_lock=True)
        repo.assert_not_called()


@patch.object(BackfillRunner,'memory_check',return_value=False)
class DayFirstWorkerTests(unittest.TestCase):
    def test_daily_latest_then_other_latest_then_all_history(self,_memory):
        day_a=batch_job('DAY_A',count=0,mode='initialization',needs_history=True,target_bars=2,
                        history_next=date(2026,10,13),history_floor=date(2026,10,1))
        day_b=batch_job('DAY_B',count=0,mode='initialization',needs_history=True,target_bars=2,
                        history_next=date(2026,10,13),history_floor=date(2026,10,1),market_no=5,id=2,
                        job_table='odd_lot_candle_backfill_jobs',candle_table='odd_lot_candle')
        minute=batch_job('MIN',interval='1m',count=0,id=3)
        repo=BatchFake([minute,day_a,day_b]); control=FakeControl()
        vendor=SimpleNamespace(fetch_historical_candles=lambda symbol,interval,first,last,**kw:[candle(first)])
        result=BackfillRunner(vendor,repo,control).run([scope(item) for item in (minute,day_a,day_b)])
        order=repo.order
        self.assertEqual(order[:2],[('DAY_A','latest'),('DAY_B','latest')])
        self.assertEqual(order[2],('MIN','latest'))
        self.assertTrue(all(phase=='history' for _,phase in order[3:]))
        self.assertEqual(result['completed'],3)
        self.assertEqual([p['interval'] for p in control.state['plans']],['day','day','1m'])

    def test_priority_daily_leaves_slow_jobs_for_after_normal_round(self,_memory):
        daily=batch_job('DAY',count=0,mode='initialization',needs_history=True,target_bars=2,
                        history_next=date(2026,10,13),history_floor=date(2026,10,1))
        repo=BatchFake([daily]); control=FakeControl()
        control.update(scheduled_daily_priority={'phase':'running'})
        vendor=SimpleNamespace(fetch_historical_candles=lambda *args,**kw:[candle(FIRST)])
        result=BackfillRunner(vendor,repo,control).run([scope(daily)])
        self.assertEqual(repo.order,[('DAY','latest')])
        self.assertEqual(result['deferred'],1)
        self.assertEqual(daily['phase'],'history')

    def test_deferred_day_does_not_block_other_planning_and_survives_restart(self,_memory):
        daily=batch_job('DAY',count=0)
        minute=batch_job('MIN',interval='1m',count=0,id=2)
        plans=[scope(minute,planned=False),scope(daily)]
        repo=BatchFake([minute,daily]); control=FakeControl(); calls=[]
        def fetch(symbol,interval,first,last,**kw):
            calls.append((symbol,interval)); return [candle(first)]
        vendor=SimpleNamespace(fetch_historical_candles=fetch)
        first_runner=BackfillRunner(vendor,repo,control); first_runner.job_window_budget=1
        with patch.dict('os.environ',{'CAPITAL_KLINE_MAX_DAYS_DAY':'1'}):
            result=first_runner.run(plans)
            self.assertEqual(result['deferred'],1)
            self.assertFalse(result['day_first_waiting'])
            self.assertIn('所選週期正常抓取輪已結束',result['pause_reason'])
            self.assertEqual(control.state['state'],'waiting_backfill')
            self.assertEqual(control.state['priority_phase'],'recovery')
            self.assertEqual(control.state['day_pending'],1)
            self.assertEqual(calls,[('DAY','day'),('MIN','1m')])
            self.assertEqual(minute['status'],'completed')
            self.assertEqual(daily['next_date'],END)
            control.update(resume_pending=True)
            result=BackfillRunner(vendor,repo,control).run(control.state['plans'])
        self.assertEqual(calls,[('DAY','day'),('MIN','1m'),('DAY','day')])
        self.assertEqual(result['completed'],2)

    def test_backed_off_daily_job_is_not_mistaken_for_complete(self,_memory):
        class BackedOffRepo(BatchFake):
            def claim_incremental_job(self,scoped,owner):
                return None if scoped['interval']=='day' else super().claim_incremental_job(scoped,owner)
        daily=batch_job('DAY'); minute=batch_job('MIN',interval='1m',id=2)
        repo=BackedOffRepo([daily,minute]); fetch=Mock(return_value=[candle(FIRST)]); control=FakeControl()
        result=BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch),repo,control).run([scope(minute),scope(daily)])
        self.assertGreater(fetch.call_count,0); self.assertEqual(result['deferred'],1)
        self.assertEqual(minute['status'],'completed'); self.assertEqual(daily['status'],'pending')
        self.assertEqual(control.state['day_pending'],1)

    def test_reorder_alone_does_not_reset_cumulative_resume_counts(self,_memory):
        daily=batch_job('DAY',status='completed'); minute=batch_job('MIN',interval='1m',id=2,status='completed')
        old=[scope(minute),scope(daily)]; control=FakeControl()
        control.update(state='waiting_backfill',plans=old,processed=7,completed=6,failed=1,insufficient_history=0)
        result=BackfillRunner(SimpleNamespace(fetch_historical_candles=Mock()),BatchFake([daily,minute]),control).run(old)
        self.assertEqual(result['processed'],7); self.assertEqual(result['completed'],6)
        self.assertEqual(result['failed'],1)

    def test_no_day_scope_does_not_fetch_day_or_change_phase_policy(self,_memory):
        minute=batch_job('MIN',interval='1m',count=0)
        week=batch_job('WEEK',interval='week',count=0,id=2)
        calls=[]
        vendor=SimpleNamespace(fetch_historical_candles=lambda s,i,f,l,**kw:(calls.append(i) or [candle(f)]))
        control=FakeControl(); result=BackfillRunner(vendor,BatchFake([minute,week]),control).run([scope(minute),scope(week)])
        self.assertEqual(calls,['1m','week']); self.assertFalse(control.state['day_first'])
        self.assertEqual(result['completed'],2)

    def test_fifth_failure_remains_terminal_instead_of_blocking_other_intervals_forever(self,_memory):
        daily=batch_job('BAD',start=FIRST,end=FIRST,count=0)
        minute=batch_job('MIN',interval='1m',start=FIRST,end=FIRST,id=2,count=0)
        repo=FailureFake([daily,minute]); repo.failures[daily['id']]=((FIRST,FIRST,FIRST),4)
        def fetch(symbol,interval,first,last,**kw):
            if symbol=='BAD': raise CandleDataError('negative volume')
            return [candle(first)]
        result=BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch),repo,FakeControl()).run([scope(minute),scope(daily)])
        self.assertEqual(daily['status'],'failed'); self.assertEqual(minute['status'],'completed')
        self.assertEqual(result['failed'],1); self.assertEqual(result['completed'],1)

    def test_max_jobs_never_admits_non_day_while_daily_is_unfinished(self,_memory):
        daily=batch_job('DAY',count=0); minute=batch_job('MIN',interval='1m',id=2,count=0)
        repo=BatchFake([daily,minute]); vendor=SimpleNamespace(fetch_historical_candles=lambda s,i,f,l,**kw:[candle(f)])
        result=BackfillRunner(vendor,repo,FakeControl(),max_jobs=1).run([scope(minute),scope(daily)])
        self.assertTrue(result['limited']); self.assertEqual(minute['status'],'pending')
        self.assertEqual(repo.order,[('DAY','latest')])


if __name__=='__main__':
    unittest.main()
