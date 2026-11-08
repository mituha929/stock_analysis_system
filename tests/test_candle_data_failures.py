"""Volume validation and the exact five-consecutive-failures boundary."""
from datetime import date, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services.backfill_engine import BackfillRunner
from app.services.backfill_control import BackfillRecovery
from app.services.capital_history_service import parse_kline_record, HistoryQuoteEvents
from app.services.candle_validation import CandleDataError, MAX_CANDLE_VOLUME, parse_volume, validate_candle_volumes
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError
from test_batch_candle_planning import BatchFake, batch_job
from test_incremental_backfill import FakeControl, candle, plan


class VolumeValidationTests(unittest.TestCase):
    def test_exact_large_integers_never_go_through_float(self):
        for value in (0,2**53+1,MAX_CANDLE_VOLUME):
            row=parse_kline_record(f'2017/12/04,10,12,9,11,{value}')
            self.assertEqual(row.volume,value)
        self.assertEqual(parse_volume('1234.000'),1234)
        self.assertEqual(parse_volume('1e3'),1000)

    def test_invalid_values_are_data_errors_with_original_text(self):
        for raw in ('-2070765642',str(MAX_CANDLE_VOLUME+1),'1.5','NaN','Infinity','1e100000','bad',''):
            with self.subTest(raw=raw),self.assertRaises(CandleDataError) as caught:
                parse_kline_record(f'2017/12/04,10,12,9,11,{raw}')
            self.assertEqual(caught.exception.details['raw_value'],raw)
            self.assertIn('2017/12/04',caught.exception.details['raw_record'])

    def test_bad_row_37_aborts_before_any_insert(self):
        rows=[candle() for _ in range(37)]; rows[36].volume=-2070765642
        cursor=Mock()
        with self.assertRaises(CandleDataError) as caught:
            MySQLCandleRepository._write_rows(cursor,'candles',dict(symbol='TSLQ',interval_code='week'),rows,None)
        self.assertEqual(caught.exception.details['batch_row'],37)
        cursor.executemany.assert_not_called()

    def test_direct_upsert_rejects_before_connecting_to_mysql(self):
        repo=MySQLCandleRepository(); bad=candle(); bad.volume=-1
        with patch.object(repo,'_connect_database') as connect,self.assertRaises(CandleDataError):
            repo.upsert_candles('TSLQ','week',[bad])
        connect.assert_not_called()

    def test_callback_keeps_failure_even_if_later_rows_are_valid(self):
        events=HistoryQuoteEvents(); events.reset()
        events.OnNotifyKLineData('TSLQ','2017/12/04,10,12,9,11,-2070765642')
        events.OnNotifyKLineData('TSLQ','2017/12/11,10,12,9,11,100')
        self.assertIsInstance(events.callback_error,CandleDataError)
        self.assertEqual(events.callback_error.details['raw_value'],'-2070765642')

    def test_transaction_rolls_back_without_disguising_data_error_as_database_outage(self):
        repo=MySQLCandleRepository(); connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchone.return_value=('owner',)
        item=batch_job(); item['owner_token']='owner'
        row=candle(); row.volume=-1
        with patch.object(repo,'_connect_database',return_value=connection),self.assertRaises(CandleDataError):
            repo.commit_window(item,[row],item['next_date']+timedelta(days=1),0)
        connection.rollback.assert_called_once(); connection.commit.assert_not_called()
        cursor.executemany.assert_not_called()

    def test_recovery_respects_data_retry_time_and_manual_pause(self):
        for due,manual,expected in (('2099-01-01T00:00:00+00:00',False,0),
                                    ('2000-01-01T00:00:00+00:00',False,1),
                                    ('2000-01-01T00:00:00+00:00',True,0)):
            with self.subTest(due=due,manual=manual):
                control=Mock(); control.snapshot.return_value=dict(source='api',state='paused',pause_kind='data',
                    pause_requested=manual,next_retry_at=due,manual_run=True,plans=[{}])
                control.worker_active.return_value=False; control.saved_plans.return_value=[{}]
                callback=Mock(); recovery=BackfillRecovery(SimpleNamespace(quote_ready=True),callback,Mock())
                recovery.stop_event=Mock(); recovery.stop_event.wait.side_effect=[False,True]
                with patch('app.services.backfill_control.BackfillControl',return_value=control), \
                     patch('app.services.backfill_control.memory_resume_ready',return_value=True):
                    recovery._run()
                self.assertEqual(callback.call_count,expected)


class FailureFake(BatchFake):
    def __init__(self,items):
        super().__init__(items)
        self.failures={}
        self.error_storage_down=False

    def record_data_failure(self,item,first,last,error):
        if self.error_storage_down:
            raise MySQLRepositoryError('failure ledger offline')
        key=(item['next_date'],first,last)
        previous=self.failures.get(item['id'])
        count=previous[1]+1 if previous and previous[0]==key else 1
        self.failures[item['id']]=(key,count)
        item.update(status='failed' if count>=5 else 'pending',owner_token=None)
        return dict(count=count,failed=count>=5,reason=f'data {count}/5',details=error.details,
                    retry_at='2099-01-01T00:00:00+00:00')

    def commit_window(self,item,candles,next_date,empty,**kwargs):
        result=super().commit_window(item,candles,next_date,empty,**kwargs)
        self.failures.pop(item['id'],None)
        return result


@patch.object(BackfillRunner,'memory_check',return_value=False)
class FiveFailureWorkerTests(unittest.TestCase):
    def setup_case(self):
        bad=batch_job('BAD',interval='day',start=date(2026,10,14),end=date(2026,10,14),count=337,attempt_count=135)
        good=batch_job('GOOD',interval='day',start=date(2026,10,14),end=date(2026,10,14)); good['id']=2
        repo=FailureFake([bad,good]); control=FakeControl()
        calls=[]
        def fetch(symbol,*args,**kwargs):
            calls.append(symbol)
            row=candle(date(2026,10,14))
            if symbol=='BAD': row.volume=-1
            return [row]
        return bad,good,repo,control,SimpleNamespace(fetch_historical_candles=fetch),calls

    def execute(self,repo,control,capital):
        scope=dict(plan(repo.items[0]),batch_v2=True,planned_on=date.today().isoformat(),planner_version=3,
                   target_bars=1200,instrument_table='instruments')
        return BackfillRunner(capital,repo,control).run([scope])

    def test_only_fifth_failure_is_failed_then_other_stock_runs(self,_memory):
        bad,good,repo,control,capital,calls=self.setup_case()
        original=bad['next_date']
        for count in range(1,5):
            result=self.execute(repo,control,capital)
            self.assertEqual(result['pause_kind'],'data')
            self.assertEqual(result['failed'],0)
            self.assertEqual(repo.failures[bad['id']][1],count)
            self.assertEqual(bad['status'],'pending')
            self.assertEqual(bad['next_date'],original)
            self.assertEqual(good['status'],'pending')
            self.assertFalse(control.state.get('pause_requested'))
        result=self.execute(repo,control,capital)
        self.assertEqual(bad['status'],'failed')
        self.assertEqual(bad['count'],337)
        self.assertEqual(bad['next_date'],original)
        self.assertEqual(good['status'],'completed')
        self.assertEqual((result['failed'],result['completed']),(1,1))
        self.assertEqual(calls,['BAD']*5+['GOOD'])

    def test_total_attempt_count_is_not_used_as_failure_count(self,_memory):
        bad,_good,repo,control,capital,_calls=self.setup_case()
        self.execute(repo,control,capital)
        self.assertEqual(bad['attempt_count'],135)
        self.assertEqual(repo.failures[bad['id']][1],1)
        self.assertEqual(bad['status'],'pending')

    def test_successful_window_clears_the_failure_counter(self,_memory):
        bad,_good,repo,control,capital,_calls=self.setup_case()
        self.execute(repo,control,capital); self.execute(repo,control,capital)
        self.assertEqual(repo.failures[bad['id']][1],2)
        capital.fetch_historical_candles=lambda *args,**kwargs:[candle(date(2026,10,14))]
        result=self.execute(repo,control,capital)
        self.assertEqual(result['failed'],0)
        self.assertNotIn(bad['id'],repo.failures)
        self.assertEqual(bad['status'],'completed')

    def test_changed_window_has_its_own_count(self,_memory):
        bad,_good,repo,control,capital,_calls=self.setup_case()
        self.execute(repo,control,capital)
        bad['next_date']-=timedelta(days=1)
        # Direct record exercises a genuinely different requested interval.
        record=repo.record_data_failure(bad,bad['next_date'],bad['next_date'],CandleDataError('bad'))
        self.assertEqual(record['count'],1)

    def test_failure_ledger_outage_is_database_pause_not_stock_failure(self,_memory):
        bad,_good,repo,control,capital,_calls=self.setup_case(); repo.error_storage_down=True
        result=self.execute(repo,control,capital)
        self.assertEqual(result['pause_kind'],'database')
        self.assertEqual(result['failed'],0)
        self.assertEqual(repo.failures,{})
        self.assertNotEqual(bad['status'],'failed')


if __name__=='__main__':
    unittest.main()
