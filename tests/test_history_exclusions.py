"""Known-period exclusions: query splitting, strict validation and atomic audit."""
from contextlib import nullcontext
from datetime import date, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.api.routes import historical_candles, sync_historical_candles
from app.services.backfill_engine import BackfillRunner
from app.services.capital_history_connection import CapitalHistoryError
from app.services.capital_history_service import HistoryQuoteEvents, request_history_candles
from app.services.candle_validation import CandleDataError
from app.services.history_exclusions import (
    allowed_history_ranges, excluded_label, exclusion_info, load_history_exclusions,
)
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError
from test_batch_candle_planning import BatchFake, batch_job
from test_incremental_backfill import FakeControl, candle, plan

FIRST, BAD, AFTER, LAST = (date(2017,11,27),date(2017,12,4),date(2017,12,11),date(2017,12,17))


class ExclusionPolicyTests(unittest.TestCase):
    def test_only_the_two_authorized_periods_are_configured(self):
        self.assertEqual([(r.symbol,r.interval,r.start,r.end) for r in load_history_exclusions()],
            [('TSLQ','month',date(2018,2,1),date(2018,2,28)),('TSLQ','week',BAD,date(2017,12,10))])

    def test_week_request_is_split_at_whole_period_boundaries(self):
        self.assertEqual(allowed_history_ranges('tslq','week',FIRST,LAST),
                         [(FIRST,date(2017,12,3)),(AFTER,LAST)])

    def test_month_request_never_requests_any_day_of_the_excluded_month(self):
        self.assertEqual(allowed_history_ranges('TSLQ','month',date(2018,1,1),date(2018,3,31)),
                         [(date(2018,1,1),date(2018,1,31)),(date(2018,3,1),date(2018,3,31))])

    def test_other_symbols_intervals_and_dates_are_untouched(self):
        for symbol,interval,first,last in [('2330','week',FIRST,LAST),('TSLQ','day',FIRST,LAST),
                                          ('TSLQ','week',AFTER,LAST),('TSLQ','month',FIRST,LAST)]:
            self.assertEqual(allowed_history_ranges(symbol,interval,first,last),[(first,last)])

    def test_partial_overlap_and_first_trading_day_alias(self):
        self.assertEqual(allowed_history_ranges('TSLQ','week',date(2017,12,6),LAST),[(AFTER,LAST)])
        self.assertEqual(allowed_history_ranges('TSLQ','week',BAD,date(2017,12,8)),[])
        self.assertTrue(excluded_label('TSLQ','week',date(2017,12,5)))
        self.assertTrue(excluded_label('TSLQ','month',date(2018,2,5)))

    def test_config_errors_do_not_silently_disable_exclusions(self):
        for data in ('{}','bad json',json.dumps([dict(id='b',symbol='*',interval='week',
                                                    period_start='2017-12-04',reason='bad')]),
                     json.dumps([dict(id='b',symbol='TSLQ',interval='week',
                                      period_start='2017-12-05',reason='not Monday')])):
            with self.subTest(data=data),patch('app.services.history_exclusions.POLICY_PATH') as path:
                load_history_exclusions.cache_clear(); path.read_text.return_value=data
                with self.assertRaises(CapitalHistoryError): load_history_exclusions()
        load_history_exclusions.cache_clear()


class QueryTests(unittest.TestCase):
    def events(self):
        events=HistoryQuoteEvents(); events.OnConnection(3003,0)
        return events

    def request(self,events,quote,first=FIRST,last=LAST):
        with patch('app.services.capital_history_service.history_request_limiter.slot',return_value=nullcontext()):
            return request_history_candles(quote,Mock(),events,lambda:None,'TSLQ','week',first,last,1)

    def test_sdk_queries_avoid_period_and_spillover_is_filtered_before_volume_parse(self):
        events=self.events(); quote=Mock()
        def response(*args):
            # Even if SDK spills an excluded record into an adjacent request,
            # it is discarded by explicit policy, not by an automatic bad-row rule.
            events.OnNotifyKLineData('TSLQ','2017/12/04,10,12,9,11,-2070765642')
            stamp='2017/11/27' if args[4]=='20171127' else '2017/12/11'
            events.OnNotifyKLineData('TSLQ',stamp+',10,12,9,11,100')
            events.OnKLineComplete('##'); return 0
        quote.SKQuoteLib_RequestKLineAMByDate.side_effect=response
        rows=self.request(events,quote)
        self.assertEqual([r.timestamp.date() for r in rows],[FIRST,AFTER])
        self.assertEqual([(c.args[4],c.args[5]) for c in quote.SKQuoteLib_RequestKLineAMByDate.call_args_list],
                         [('20171127','20171203'),('20171211','20171217')])

    def test_fully_excluded_window_never_calls_the_sdk(self):
        quote=Mock(); events=self.events()
        self.assertEqual(self.request(events,quote,BAD,date(2017,12,10)),[])
        quote.SKQuoteLib_RequestKLineAMByDate.assert_not_called()

    def test_unknown_bad_date_still_aborts_entire_combined_response(self):
        events=self.events(); quote=Mock()
        def response(*args):
            stamp='2017/11/27' if args[4]=='20171127' else '2017/12/11'
            volume=100 if args[4]=='20171127' else -1
            events.OnNotifyKLineData('TSLQ',f'{stamp},10,12,9,11,{volume}')
            events.OnKLineComplete('##'); return 0
        quote.SKQuoteLib_RequestKLineAMByDate.side_effect=response
        with self.assertRaises(CandleDataError): self.request(events,quote)
        self.assertFalse(events.accepting_records); self.assertEqual(events.records,[])

    def test_unknown_interval_other_symbol_and_malformed_rows_are_not_skipped(self):
        for symbol,interval,raw,error_type in [
            ('2330','week','2017/12/04,10,12,9,11,-1',CandleDataError),
            ('TSLQ','day','2017/12/04,10,12,9,11,-1',CandleDataError),
            ('TSLQ','week','bad-date,10,12,9,11,-1',ValueError),
            ('TSLQ','week','2017/12/04,10,12,9,11',ValueError)]:
            events=self.events(); events.reset(symbol,interval); events.OnNotifyKLineData(symbol,raw)
            self.assertIsInstance(events.callback_error,error_type)


@patch.object(BackfillRunner,'memory_check',return_value=False)
class ExclusionWorkerTests(unittest.TestCase):
    def test_excluded_bar_is_not_written_or_counted_and_other_stock_continues(self,_memory):
        bad=batch_job('TSLQ',interval='week',start=FIRST,end=LAST,count=0,needs_history=False)
        good=batch_job('2330',interval='week',start=FIRST,end=LAST,count=0,needs_history=False); good['id']=2
        repo=BatchFake([bad,good]); control=FakeControl(); committed=[]; observed_exclusions=[]
        original=repo.commit_window
        def commit(item,rows,*args,**kwargs):
            committed.extend((item['symbol'],r.timestamp.date()) for r in rows)
            observed_exclusions.extend(control.state.get('history_exclusions',[]))
            return original(item,rows,*args,**kwargs)
        repo.commit_window=commit
        invalid=candle(BAD); invalid.volume=-1
        capital=SimpleNamespace(fetch_historical_candles=lambda symbol,*args,**kwargs:
            [candle(FIRST),invalid,candle(AFTER)] if symbol=='TSLQ' else [candle(BAD)])
        scope=dict(plan(bad),batch_v2=True,fixed_range=True,planned_on=date.today().isoformat(),
                   planner_version=3,target_bars=560,instrument_table='instruments',as_of_date=LAST)
        result=BackfillRunner(capital,repo,control).run([scope])
        self.assertEqual(result['failed'],0)
        self.assertEqual(bad['count'],2)
        self.assertNotIn(('TSLQ',BAD),committed)
        self.assertIn(('2330',BAD),committed)
        self.assertEqual(bad['next_date'],LAST+timedelta(days=1))
        self.assertEqual(observed_exclusions[0]['id'],'TSLQ-week-20171204')
        self.assertEqual(control.state['history_exclusions'],[])

    def test_excluded_only_is_not_evidence_of_missing_older_history(self,_memory):
        item=batch_job('TSLQ',interval='week',start=BAD,end=date(2017,12,10),count=0,
                       needs_history=False,empty_windows=2)
        repo=BatchFake([item]); item.update(status='running',owner_token='test')
        runner=BackfillRunner(SimpleNamespace(fetch_historical_candles=lambda *a,**kw:[]),repo,FakeControl())
        runner.run_job(item)
        self.assertEqual(item['empty_windows'],2)
        self.assertEqual(item['next_date'],AFTER)

    def test_excluded_only_does_not_trigger_preexisting_empty_window_cutoff(self,_memory):
        item=batch_job('TSLQ',interval='week',start=FIRST,end=date(2017,12,10),count=0,
                       phase='history',history_floor=FIRST,next_date=date(2017,12,10),empty_windows=3)
        repo=BatchFake([item]); item.update(status='running',owner_token='test')
        calls=[]
        def fetch(symbol,interval,first,last,**kwargs):
            calls.append((first,last))
            return [] if first==BAD else [candle(FIRST)]
        runner=BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch),repo,FakeControl())
        with patch.dict('os.environ',{'CAPITAL_KLINE_MAX_DAYS_WEEK':'7'}):
            runner.run_job(item)
        self.assertEqual(calls,[(BAD,date(2017,12,10)),(FIRST,date(2017,12,3))])
        self.assertEqual(item['count'],1)


class AuditTests(unittest.TestCase):
    def item(self):
        return dict(id=1,owner_token='owner',symbol='TSLQ',interval_code='week',mode='range',
                    candle_table='candles',job_table='candle_backfill_jobs',end_date=LAST,target_bars=560)

    def test_audit_and_cursor_share_transaction_with_no_fake_candle(self):
        repo=MySQLCandleRepository(); connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchone.return_value=('owner',); repo._history_exclusion_storage_ready=True
        with patch.object(repo,'_connect_database',return_value=connection),patch.object(repo,'_stored_count',return_value=2):
            repo.commit_window(self.item(),[],LAST+timedelta(days=1),0,window_start=FIRST,window_end=LAST)
        sql=[c.args for c in cursor.execute.call_args_list]
        audit=next(c for c in sql if c[0].startswith('INSERT INTO candle_backfill_skips'))
        self.assertEqual(audit[1][2:4],(BAD,date(2017,12,10)))
        self.assertIn('history_exclusion:TSLQ-week-20171204',audit[1][4])
        cursor.executemany.assert_not_called(); connection.commit.assert_called_once()

    def test_failure_after_audit_rolls_back_and_does_not_advance_cursor(self):
        repo=MySQLCandleRepository(); connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchone.return_value=('owner',); repo._history_exclusion_storage_ready=True
        def fail(sql,*args):
            if sql.startswith('UPDATE candle_backfill_progress SET next_date='): raise RuntimeError('write failed')
        cursor.execute.side_effect=fail; item=self.item()
        with patch.object(repo,'_connect_database',return_value=connection),self.assertRaises(MySQLRepositoryError):
            repo.commit_window(item,[],AFTER,0,window_start=FIRST,window_end=LAST)
        connection.rollback.assert_called_once(); connection.commit.assert_not_called()
        self.assertNotIn('next_date',item)

    def test_get_and_sync_responses_disclose_exclusions(self):
        capital=SimpleNamespace(fetch_historical_candles=lambda *args,**kwargs:[])
        response=historical_candles('TSLQ',interval='week',start_date=FIRST,end_date=LAST,capital=capital)
        self.assertEqual(response.excluded_periods,exclusion_info('TSLQ','week',FIRST,LAST))
        with patch('app.api.routes.MySQLCandleRepository') as repo:
            repo.return_value.upsert_candles.return_value=0
            result=sync_historical_candles('TSLQ',interval='week',table_name='candles',
                                          start_date=FIRST,end_date=LAST,capital=capital)
        self.assertEqual(result.fetched_count,0)
        self.assertEqual(result.excluded_periods[0]['id'],'TSLQ-week-20171204')


if __name__=='__main__':
    unittest.main()
