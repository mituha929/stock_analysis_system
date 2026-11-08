"""Regression: all normal intervals first; products and dates stay distinct."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services.backfill_engine import BackfillRunner
from app.services.empty_history_review import EmptyHistoryReviewRepository, pending_review_text
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.day_candle_integrity_runner import DayIntegrityAudit
from app.services.day_candle_integrity import DayEvidence
from test_batch_candle_planning import BatchFake, batch_job
from test_day_first_backfill import scope, FIRST, END
from test_incremental_backfill import FakeControl, candle


class MemoryCountCursor:
    """Translate ONLY MySQL's multi-column COUNT(DISTINCT) for SQLite tests.

    NULL concatenation matches MySQL's NULL exclusion. Parameterized scope
    predicates and grouping run as real SQL, not mocked numeric responses.
    """
    def __init__(self, db):
        self.db=db; self.calls=[]
    def execute(self, sql, values):
        self.calls.append((sql,values))
        translated=' '.join(sql.split())
        translated=translated.replace('COUNT(DISTINCT candle_table,market_no,symbol)',
            "COUNT(DISTINCT candle_table || '|' || market_no || '|' || symbol)")
        for predicate,tail in (("NOT IN('week','month')",'market_no,symbol,window_start'),
                               ("IN('week','month')",'market_no,symbol,interval_code,window_start')):
            original=f'COUNT(DISTINCT CASE WHEN interval_code {predicate} THEN candle_table END, {tail})'
            columns=" || '|' || ".join(tail.split(','))
            replacement=f"COUNT(DISTINCT (CASE WHEN interval_code {predicate} THEN candle_table END) || '|' || {columns})"
            translated=translated.replace(original,replacement)
        self.result=self.db.execute(translated.replace('%s','?'),tuple(values))
    def fetchone(self):
        return self.result.fetchone()


class MemoryCountRepository(EmptyHistoryReviewRepository):
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE candle_empty_history_reviews(candle_table TEXT,market_no INT,symbol TEXT,'
                        'interval_code TEXT,window_start TEXT,window_end TEXT,status TEXT)')
        self.q=MemoryCountCursor(self.db)
    @contextmanager
    def transaction(self):
        yield self.q
    def add(self, symbol='A',interval='day',day='2026-10-01',market=0,status='pending_review',table='candles'):
        self.db.execute('INSERT INTO candle_empty_history_reviews VALUES(?,?,?,?,?,?,?)',
                        (table,market,symbol,interval,day,day,status))


class GapCountTests(unittest.TestCase):
    def setUp(self):
        self.repo=MemoryCountRepository(); self.addCleanup(self.repo.db.close)
    def plans(self,intervals=('day',),market=0,**extra):
        return [dict(candle_table='candles',market_no=market,interval=i,**extra) for i in intervals]
    def test_one_security_ten_days_is_one_security_not_ten(self):
        for n in range(1,11): self.repo.add(day=f'2026-10-{n:02d}')
        self.assertEqual(self.repo.empty_review_summary(self.plans()),dict(records=10,symbols=1,dates=10,periods=0))
    def test_repeated_plan_or_interval_does_not_multiply_security_or_dates(self):
        for n in range(1,11):
            for interval in ('day','1m'): self.repo.add(interval=interval,day=f'2026-10-{n:02d}')
        plans=self.plans(('day','1m')); plans.append(dict(plans[0]))
        self.assertEqual(self.repo.empty_review_summary(plans),dict(records=20,symbols=1,dates=10,periods=0))
    def test_two_securities_share_calendar_date_but_have_two_missing_dates(self):
        for symbol in ('A','B'): self.repo.add(symbol=symbol)
        self.assertEqual(self.repo.empty_review_summary(self.plans()),dict(records=2,symbols=2,dates=2,periods=0))
    def test_market_is_part_of_security_identity(self):
        self.repo.add(market=0); self.repo.add(market=5)
        self.assertEqual(self.repo.empty_review_summary(self.plans(market=0)+self.plans(market=5))['symbols'],2)
    def test_week_month_are_periods_not_invented_calendar_days(self):
        for interval in ('day','week','month'): self.repo.add(interval=interval)
        self.assertEqual(self.repo.empty_review_summary(self.plans(('day','week','month'))),
                         dict(records=3,symbols=1,dates=1,periods=2))
    def test_resolved_or_other_scope_is_not_counted(self):
        self.repo.add(status='resolved'); self.repo.add(market=1); self.repo.add(symbol='B')
        self.assertEqual(self.repo.empty_review_summary(self.plans(only_symbols=['A']))['records'],0)
    def test_empty_scope_is_zero_and_never_queries_all_products(self):
        self.repo.add()
        self.assertEqual(self.repo.empty_review_summary(self.plans(only_symbols=[])),dict(records=0,symbols=0,dates=0,periods=0))
        self.assertEqual(self.repo.q.calls,[])
    def test_overlapping_symbol_scopes_are_unioned_not_double_counted(self):
        self.repo.add(symbol='A'); self.repo.add(symbol='B'); self.repo.add(symbol='C')
        self.assertEqual(self.repo.empty_review_summary(self.plans(only_symbols=['A','B'])+
            self.plans(only_symbols=['B']))['symbols'],2)
    def test_message_does_not_call_ten_gap_records_ten_stocks(self):
        result=dict(pending_review=10,pending_review_symbols=1,pending_review_dates=10,pending_review_periods=0)
        self.assertEqual(pending_review_text(result),'待查 1 檔／10 個日期')
        self.assertNotIn('10 檔',pending_review_text(dict(pending_review=10)))
    def test_resume_log_keeps_product_and_date_units(self):
        from app.api.routes import _run_stale_backfill_unlocked
        result=dict(processed=1,completed=1,insufficient_history=0,failed=0,deferred=0,
                    pending_review=10,pending_review_symbols=1,pending_review_dates=10,pending_review_periods=0)
        plans=[dict(batch_v2=True,run_id=123)]
        with patch('app.api.routes.BackfillRunner') as runner, patch('app.api.routes.MySQLCandleRepository') as repo:
            runner.return_value.run.return_value=result
            _run_stale_backfill_unlocked(SimpleNamespace(),plans,1200,owns_worker_lock=True)
        log=repo.return_value.update_daily_backfill_run.call_args.kwargs
        self.assertIn('本輪 K 線抓取已結束',log['message'])
        self.assertIn('待查 1 檔／10 個日期',log['message'])
        self.assertNotIn('10 檔',log['message'])
    def test_report_counts_cover_all_pages_not_just_current_item(self):
        repo=MySQLCandleRepository(); connection=Mock(); q=connection.cursor.return_value
        q.fetchone.side_effect=[{'n':1},{'n':10},dict(records=10,symbols=1,dates=10,periods=0)]
        q.fetchall.return_value=[dict(symbol='A',status='pending_review',observation_days=[],evidence={})]
        with patch.object(repo,'_connect_database',return_value=connection):
            page=repo.empty_history_report([0],limit=1)
        self.assertEqual((len(page['items']),page['counts']['symbols'],page['counts']['dates']),(1,1,10))
        self.assertTrue(all(call.args[0].lstrip().startswith('SELECT') for call in q.execute.call_args_list))


@patch.object(BackfillRunner,'memory_check',return_value=False)
class RecoveryLastTests(unittest.TestCase):
    def test_each_other_interval_latest_precedes_daily_history(self,_memory):
        daily=batch_job('DAY',count=0,mode='initialization',needs_history=True,target_bars=2,
                        history_next=date(2026,10,13),history_floor=date(2026,10,1))
        items=[daily]+[batch_job(i,interval=i,id=n+2,count=0) for n,i in enumerate(('1m','5m','15m','60m','week','month'))]
        repo=BatchFake(items); runner=BackfillRunner(SimpleNamespace(fetch_historical_candles=lambda s,i,f,l,**kw:[candle(f)]),repo,FakeControl())
        runner.run([scope(item) for item in reversed(items)])
        first_history=next(n for n,(_,phase) in enumerate(repo.order) if phase=='history')
        self.assertEqual(first_history,7)
        self.assertTrue(all(phase=='latest' for _,phase in repo.order[:7]))
        self.assertEqual(repo.order[0],('DAY','latest'))
    def test_existing_daily_catchup_is_after_other_normal_requests(self,_memory):
        daily=batch_job('DAY',phase='catchup'); minute=batch_job('MIN',interval='1m',id=2)
        repo=BatchFake([daily,minute])
        runner=BackfillRunner(SimpleNamespace(fetch_historical_candles=lambda s,i,f,l,**kw:[candle(f)]),repo,FakeControl())
        runner.run([scope(daily),scope(minute)])
        self.assertEqual(repo.order[0],('MIN','latest'))
        self.assertIn(('DAY','catchup'),repo.order[1:])
    def test_no_recovery_budget_still_fetches_other_normal_intervals(self,_memory):
        daily=batch_job('DAY',phase='history',mode='initialization',needs_history=True,count=0)
        minute=batch_job('MIN',interval='1m',id=2)
        repo=BatchFake([daily,minute]); control=FakeControl()
        runner=BackfillRunner(SimpleNamespace(fetch_historical_candles=lambda s,i,f,l,**kw:[candle(f)]),repo,control)
        runner.recovery_budget=0
        result=runner.run([scope(daily),scope(minute)])
        self.assertEqual(minute['status'],'completed'); self.assertEqual(daily['status'],'pending')
        self.assertEqual(result['deferred'],1); self.assertTrue(control.state['normal_round_complete'])
    def test_holiday_audit_is_last_and_cannot_block_already_fetched_intervals(self,_memory):
        class AuditableRepo(BatchFake):
            def ensure_day_integrity_storage(self): pass
        daily=batch_job('DAY'); minute=batch_job('MIN',interval='1m',id=2)
        repo=AuditableRepo([daily,minute]); control=FakeControl()
        runner=BackfillRunner(SimpleNamespace(fetch_historical_candles=lambda s,i,f,l,**kw:[candle(f)]),repo,control)
        def audit(_plans):
            self.assertEqual(repo.order,[('DAY','latest'),('MIN','latest')])
            self.assertTrue(control.state['normal_round_complete'])
            return dict(blocked=1,summary=dict(blocked_symbols=1,unresolved_symbols=1,unresolved_dates=10),
                        retry_at=datetime.utcnow()+timedelta(seconds=900))
        with patch('app.services.backfill_engine.integrity_schedule',return_value=(True,'假日')), \
             patch('app.services.day_candle_integrity_runner.DayIntegrityAudit') as audit_class:
            audit_class.return_value.run.side_effect=audit
            result=runner.run([scope(daily),scope(minute)])
        self.assertEqual(result['audit_pending_symbols'],1)
        self.assertEqual(control.state['state'],'waiting_backfill')
        self.assertIn('待查 1 檔／10 個日期',result['pause_reason'])
        self.assertEqual(minute['status'],'completed')
    def test_audit_product_count_is_not_gap_count(self,_memory):
        from test_day_candle_integrity import MemoryAuditRepo, sessions
        repo=MemoryAuditRepo([]); repo.due_day_gap=lambda *args:None
        runner=BackfillRunner(SimpleNamespace(),repo,FakeControl())
        plans=[dict(market_no=0,interval='day',batch_v2=True,candle_table='candles',target_bars=1200,end_date=END)]
        with patch('app.services.day_candle_integrity_runner.DayEvidence.load',return_value=DayEvidence()), \
             patch('app.services.day_candle_integrity_runner.DayCalendar',side_effect=sessions), \
             patch.object(DayIntegrityAudit,'assess',return_value=dict(status='unresolved',unresolved_count=10)):
            result=DayIntegrityAudit(runner).run(plans)
        self.assertEqual(result['summary']['unresolved_symbols'],1)
        self.assertEqual(result['summary']['unresolved_dates'],10)


if __name__=='__main__': unittest.main()
