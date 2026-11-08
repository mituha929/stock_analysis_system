"""Offline policy, transaction, checkpoint, calendar and read-only API tests."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import itertools
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from app.api.routes import router
from app.services.backfill_engine import BackfillRunner
from app.services.empty_history_review import (
    EmptyHistoryReviewRepository, empty_review_days, updated_observation,
    observation_session, next_observation_at, TAIPEI,
    missing_periods,
)
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.taiwan_trading_calendar import (
    trading_day_status, query_trading_bounds, stock_calendar_snapshot, TaiwanTradingCalendarError,
)
from app.services.day_candle_integrity import DayCalendar, integrity_schedule
from test_batch_candle_planning import BatchFake, batch_job
from test_incremental_backfill import FakeControl, candle
from test_day_first_backfill import scope
from test_backfill_api_control import asgi_request


def at(day, hour=15):
    return datetime(2026, 10, day, hour, tzinfo=TAIPEI)


class ObservationTests(unittest.TestCase):
    def test_shifted_query_windows_keep_same_date_key_and_observations(self):
        repo=MySQLCandleRepository(); q=Mock(); store={}; selected=[None]
        def execute(sql,params):
            if sql.startswith('SELECT observation_days'):
                selected[0]=store.get(tuple(params))
            elif sql.startswith('INSERT INTO candle_empty_history_reviews'):
                store[tuple(params[:6])]=(params[7],params[6])
        q.execute.side_effect=execute
        q.fetchone.side_effect=lambda:selected[0]
        item=dict(candle_table='candles',market_no=0,symbol='EMPTY',interval_code='day')
        for first,last,now in ((date(2026,10,14),date(2026,10,15),at(19)),
                               (date(2026,10,15),date(2026,10,16),at(20))):
            for a,b in missing_periods('day',first,last):
                repo._observe_empty(q,item,a,b,now)
        key=('candles',0,'EMPTY','day',date(2026,10,15),date(2026,10,15))
        self.assertEqual(json.loads(store[key][0]),['2026-10-19','2026-10-20'])
        self.assertEqual(len(store),3)

    def test_week_month_keys_are_whole_stable_period_not_request_end(self):
        self.assertEqual(list(missing_periods('week',date(2026,10,19),date(2026,10,22))),
                         [(date(2026,10,19),date(2026,10,25))])
        self.assertEqual(list(missing_periods('month',date(2026,10,1),date(2026,10,22))),
                         [(date(2026,10,1),date(2026,10,31))])

    def test_default_and_supported_thresholds(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(empty_review_days(), 5)
        for value in ('3','4','5'):
            with patch.dict('os.environ', {'CAPITAL_EMPTY_REVIEW_TRADING_DAYS':value}):
                self.assertEqual(empty_review_days(), int(value))
        for value in ('0','2','6','nan','3.5'):
            with patch.dict('os.environ', {'CAPITAL_EMPTY_REVIEW_TRADING_DAYS':value}), self.assertRaises(ValueError):
                empty_review_days()

    def test_same_day_retries_never_count_twice(self):
        days, review = updated_observation([], at(19), date(2017,12,4))
        for _ in range(10):
            days, review = updated_observation(json.dumps(days), at(19,23), date(2017,12,4))
        self.assertEqual(days, ['2026-10-19'])
        self.assertFalse(review)

    def test_exact_fifth_distinct_trading_day_not_calendar_age(self):
        days = []
        for index, day in enumerate((16,19,20,21,22), 1):
            days, review = updated_observation(days, at(day), date(2017,12,4))
            self.assertEqual(len(days), index)
            self.assertEqual(review, index == 5)

    def test_weekend_official_holiday_and_emergency_closure_never_count(self):
        for day in (23,24,25,26):
            self.assertIsNone(observation_session(at(day), date(2017,12,4)))
        self.assertEqual(updated_observation([], at(24), date(2017,12,4)), ([],False))

    def test_future_target_and_unfinished_session_never_count(self):
        self.assertIsNone(observation_session(at(22,10), date(2026,10,21)))
        self.assertIsNone(observation_session(at(22), date(2026,10,27)))

    def test_unverified_observation_day_cannot_increase_count(self):
        with patch('app.services.day_candle_integrity.DayCalendar.status',return_value=(None,'unverified')):
            self.assertEqual(updated_observation([],at(22),date(2017,1,1)),([],False))

    def test_next_attempt_skips_weekend_and_two_confirmed_closures(self):
        self.assertEqual(next_observation_at(at(22)), datetime(2026,10,27,6,35))

    def test_taipei_day_not_utc_day_is_observation_key(self):
        stamp = datetime(2026,10,19,15,59,tzinfo=timezone.utc)
        self.assertEqual(updated_observation([], stamp, date(2017,1,1))[0], ['2026-10-19'])


class RuntimeCalendarTests(unittest.TestCase):
    def test_confirmed_typhoon_closure_shared_by_all_stock_flows(self):
        self.assertFalse(trading_day_status(date(2026,10,23))[0])
        self.assertFalse(DayCalendar().status(date(2026,10,23))[0])

    def test_historical_makeup_saturday_is_open_not_silently_deleted(self):
        for day in (date(2017,2,18), date(2017,6,3), date(2017,9,30), date(2018,3,31), date(2018,12,22)):
            self.assertTrue(trading_day_status(day)[0])
            self.assertTrue(DayCalendar().status(day)[0])
        self.assertFalse(trading_day_status(date(2019,2,23))[0])

    def test_missing_historical_saturday_calendar_is_unknown_not_closed(self):
        with patch.dict('os.environ', {'CAPITAL_STOCK_HOLIDAY_FILE':'data/nonexistent-unit-holidays.json'}):
            self.assertTrue(trading_day_status(date(2018,3,31))[0])
            self.assertIsNone(DayCalendar({'years':{}},overrides=[]).status(date(2018,3,31))[0])

    def test_partial_city_closure_does_not_create_full_market_closure(self):
        self.assertTrue(trading_day_status(date(2018,7,10))[0])

    def test_wholly_closed_window_not_queried(self):
        self.assertIsNone(query_trading_bounds('day',date(2026,10,23),date(2026,10,26)))

    def test_closed_edges_trimmed_but_bulk_query_not_exploded(self):
        self.assertEqual(query_trading_bounds('day',date(2026,10,23),date(2026,10,28)),
                         (date(2026,10,27),date(2026,10,28)))
        self.assertEqual(query_trading_bounds('day',date(2026,10,22),date(2026,10,28)),
                         (date(2026,10,22),date(2026,10,28)))

    def test_week_and_month_keep_period_labels_on_closed_start(self):
        for interval, first, last in (('week',date(2026,10,26),date(2026,10,30)),
                                      ('month',date(2026,8,1),date(2026,8,31))):
            self.assertEqual(query_trading_bounds(interval,first,last),(first,last))

    def test_modified_snapshot_reloaded_and_invalid_snapshot_rejected(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'calendar.json'
            data = dict(version=1,markets=['TWSE','TPEx','ESB'],range_start='2026-10-01',range_end='2026-10-31',
                        records=[],annual_coverage=[dict(year=2026,status='api')])
            path.write_text(json.dumps(data),encoding='utf-8')
            with patch.dict('os.environ',{'CAPITAL_STOCK_HOLIDAY_FILE':str(path)}):
                self.assertTrue(trading_day_status(date(2026,10,22))[0])
                data['records'] = [dict(date='2026-10-22',is_closed=True,category='manual_override')]
                path.write_text(json.dumps(data),encoding='utf-8')
                self.assertFalse(trading_day_status(date(2026,10,22))[0])
                path.write_text('{broken',encoding='utf-8')
                with self.assertRaises(TaiwanTradingCalendarError):
                    stock_calendar_snapshot()


class SQLReviewTests(unittest.TestCase):
    def case(self, old=None):
        repo = MySQLCandleRepository()
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.return_value = []
        item = batch_job()
        item.update(status='running',owner_token='owner')
        cursor.fetchone.side_effect = itertools.chain([('owner',item['next_date'],'running')],itertools.repeat(old))
        return repo, connection, cursor, item

    def test_atomic_observation_releases_owner_without_advancing_or_writing_candles(self):
        repo, connection, cursor, item = self.case()
        before = deepcopy(item)
        with patch.object(repo,'_connect_database',return_value=connection):
            result = repo.defer_empty_window(item,date(2026,10,14),date(2026,10,15),now=at(19))
        self.assertEqual(result['days'],['2026-10-19'])
        self.assertEqual(item['next_date'],before['next_date'])
        sql = '\n'.join(c.args[0] for c in cursor.execute.call_args_list)
        self.assertNotIn('SET next_date',sql)
        self.assertNotIn('candle_check_state',sql)
        self.assertNotIn('candle_sync_state',sql)
        self.assertNotIn('INSERT INTO `candles`',sql)
        connection.commit.assert_called_once()

    def test_fifth_day_uses_review_not_completed_failed_or_insufficient(self):
        previous = (json.dumps(['2026-10-16','2026-10-19','2026-10-20','2026-10-21']),'observing')
        repo, connection, cursor, item = self.case(previous)
        with patch.object(repo,'_connect_database',return_value=connection):
            result = repo.defer_empty_window(item,date(2017,12,4),date(2017,12,4),now=at(22))
        self.assertTrue(result['pending_review'])
        self.assertFalse(result['no_trade_confirmed'])
        update = [c for c in cursor.execute.call_args_list if c.args[0].startswith('UPDATE `')][0]
        self.assertEqual(update.args[1][0],'waiting_review')

    def test_stale_owner_does_not_record_observation(self):
        repo, connection, cursor, item = self.case()
        cursor.fetchone.side_effect = [('someone-else',item['next_date'],'running')]
        with patch.object(repo,'_connect_database',return_value=connection), self.assertRaises(MySQLRepositoryError):
            repo.defer_empty_window(item,date(2026,10,14),date(2026,10,15),now=at(19))
        self.assertEqual(cursor.execute.call_count,1)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_ledger_write_failure_rolls_back_owner_and_observation(self):
        repo, connection, cursor, item = self.case()
        def fail(sql,*args):
            if sql.startswith('UPDATE `'):
                raise RuntimeError('injected write failure')
        cursor.execute.side_effect = fail
        with patch.object(repo,'_connect_database',return_value=connection), self.assertRaises(MySQLRepositoryError):
            repo.defer_empty_window(item,date(2026,10,14),date(2026,10,15),now=at(19))
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_existing_observation_reused_after_process_restart(self):
        repo, connection, cursor, item = self.case((json.dumps(['2026-10-19']),'observing'))
        with patch.object(repo,'_connect_database',return_value=connection):
            result = repo.defer_empty_window(item,date(2026,10,14),date(2026,10,15),now=at(20))
        self.assertEqual(result['days'],['2026-10-19','2026-10-20'])

    def test_errors_update_diagnostics_only_not_observation_days(self):
        repo, connection, cursor, item = self.case()
        with patch.object(repo,'_connect_database',return_value=connection):
            repo.record_empty_query_error(item,date(2026,10,14),date(2026,10,15),RuntimeError('timeout'))
        sql = cursor.execute.call_args.args[0]
        self.assertIn('error_count=error_count+1',sql)
        self.assertNotIn('observation_days',sql)

    def test_partial_success_resolves_only_present_date_not_other_gaps_of_same_job(self):
        repo,connection,cursor,item=self.case()
        repo.resolve_empty_rows(cursor,item,[candle(date(2026,10,19))],at(20))
        updates=[c for c in cursor.execute.call_args_list if c.args[0].startswith('UPDATE')]
        self.assertEqual(len(updates),1)
        self.assertIn('window_start IN',updates[0].args[0])
        self.assertEqual(updates[0].args[1][-1],date(2026,10,19))
        self.assertNotIn('checkpoint=',updates[0].args[0])

    def test_read_report_before_migration_does_not_create_tables(self):
        repo, connection, cursor, item = self.case()
        cursor.fetchone.side_effect = None
        cursor.fetchone.return_value = {'n':0}
        with patch.object(repo,'_connect_database',return_value=connection):
            result = repo.empty_history_report([0,1])
        self.assertFalse(result['storage_ready'])
        self.assertTrue(all(c.args[0].startswith('SELECT') for c in cursor.execute.call_args_list))

    def test_nonbatch_claim_honors_observation_backoff_and_review_never_claimed(self):
        repo, connection, cursor, item = self.case()
        repo._empty_review_ready = True
        cursor.fetchone.side_effect = None
        cursor.fetchone.return_value = None
        with patch.object(repo,'_connect_database',return_value=connection):
            repo.claim_incremental_job(dict(job_table=item['job_table'],market_no=0,interval='day',
                end_date=item['end_date']), 'owner')
        sql = cursor.execute.call_args.args[0]
        self.assertIn("j.status='pending'",sql)
        self.assertIn("er.next_retry_at>UTC_TIMESTAMP(6)",sql)

    def test_expanded_day_reply_observes_only_missing_gap_not_other_present_day(self):
        repo, connection, cursor, item = self.case()
        repo._empty_review_ready=True
        missing = date(2026,10,19)
        cursor.fetchone.side_effect=[('owner',),None]
        cursor.fetchall.return_value=[(missing,)]
        with patch.object(repo,'_connect_database',return_value=connection), \
             patch.object(repo,'_write_rows'), \
             patch('app.services.empty_history_review.updated_observation',return_value=(['2026-10-16','2026-10-19','2026-10-20'],True)):
            repo.commit_day_gap_window('candles',0,'EMPTY',date(2026,10,16),date(2026,10,20),
                                       [candle(date(2026,10,20))],'owner',900)
        inserts = [c for c in cursor.execute.call_args_list if c.args[0].startswith('INSERT INTO candle_empty_history_reviews')]
        self.assertEqual(len(inserts),1)
        self.assertEqual(inserts[0].args[1][4:7],(missing,missing,'pending_review'))
        connection.commit.assert_called_once()

    def test_manual_retry_preserves_cursor_counts_and_does_not_certify_no_trade(self):
        repo, connection, cursor, item = self.case()
        cursor.fetchone.side_effect=[dict(id=1,market_no=0,symbol='EMPTY',interval_code='day',
            window_start=date(2026,10,14),window_end=date(2026,10,15),candle_table='candles'),{'n':1}]
        with patch.object(repo,'_connect_database',return_value=connection):
            result = repo.request_empty_review_retry(1,'已人工檢閱，要求再查一次')
        self.assertEqual(result['status'],'retry_requested')
        self.assertFalse(result['no_trade_confirmed'])
        sql='\n'.join(c.args[0] for c in cursor.execute.call_args_list)
        self.assertNotIn('SET next_date',sql)
        self.assertNotIn('received_count=',sql)
        self.assertNotIn("status='completed'",sql)
        self.assertNotIn('observation_days=',sql)

    def test_unknown_manual_review_id_rolls_back_without_changing_work(self):
        repo, connection, cursor, item = self.case()
        cursor.fetchone.side_effect=None; cursor.fetchone.return_value=None
        with patch.object(repo,'_connect_database',return_value=connection), self.assertRaises(MySQLRepositoryError):
            repo.request_empty_review_retry(999,'retry')
        self.assertEqual(cursor.execute.call_count,1)
        connection.rollback.assert_called_once()


class ReviewFake(BatchFake):
    """Fake persistence implements real observation policy, never real MySQL."""
    def __init__(self,items):
        super().__init__(items)
        self.days = []
        self.now = at(19)
        self.review = False
        self.observed = []
    def empty_review_overlap(self,job,first,last):
        return dict(id=1,window_start=date(2026,10,14),window_end=date(2026,10,15)) if self.review and job['symbol']=='EMPTY' else None
    def defer_empty_window(self,job,first,last):
        self.days,self.review = updated_observation(self.days,self.now,last)
        self.observed.append((first,last))
        job.update(status='waiting_review' if self.review else 'pending',last_round=job['owner_token'],phase='catchup')
        return dict(pending_review=self.review,reason='waiting evidence',days=self.days)
    def empty_review_count(self,plans):
        return int(self.review)
    def hold_empty_review(self,job,record):
        job['status']='waiting_review'
        return dict(pending_review=True,reason='overlap retained')


@patch.object(BackfillRunner,'memory_check',return_value=False)
class WorkerReviewTests(unittest.TestCase):
    def case(self):
        item = batch_job('EMPTY',start=date(2026,10,14),end=date(2026,10,15))
        other = batch_job('GOOD',interval='1m'); other['id']=2
        repo = ReviewFake([item,other])
        class Vendor:
            def __init__(self):
                self.fetch_historical_candles=Mock(side_effect=lambda s,i,f,l,**kw: [] if s=='EMPTY' else [candle(f)])
                self.checkpoint=Mock()
            def history_checkpoint(self,committed):
                self.checkpoint(committed)
        vendor = Vendor()
        return item,other,repo,vendor,FakeControl()

    def test_five_day_wait_keeps_cursor_then_releases_other_period_not_false_complete(self,_memory):
        item,other,repo,vendor,control = self.case()
        original = item['next_date']
        for day in (16,19,20,21,22):
            repo.now = at(day)
            result = BackfillRunner(vendor,repo,control).run([scope(item),scope(other)])
            self.assertEqual(item['next_date'], original)
        self.assertEqual(item['status'],'waiting_review')
        self.assertEqual(other['status'],'completed')
        self.assertEqual(result['completed'],1)
        self.assertEqual(result['pending_review'],1)
        self.assertEqual(control.state['state'],'waiting_review')
        self.assertFalse(repo.checks.get('EMPTY'))
        self.assertFalse(repo.rows)

    def test_other_symbols_continue_even_before_threshold(self,_memory):
        item,other,repo,vendor,control = self.case()
        other['interval_code']='day'
        BackfillRunner(vendor,repo,control).run([scope(item)])
        self.assertEqual(item['status'],'pending')
        self.assertEqual(other['status'],'completed')

    def test_review_overlap_of_new_plan_never_requeries(self,_memory):
        item,other,repo,vendor,control = self.case()
        repo.review = True
        BackfillRunner(vendor,repo,control).run([scope(item)])
        vendor.fetch_historical_candles.assert_not_called()
        self.assertEqual(item['status'],'waiting_review')

    def test_source_error_never_becomes_valid_empty(self,_memory):
        from app.services.capital_history_connection import CapitalHistoryConnectionError
        item,other,repo,vendor,control = self.case()
        vendor.fetch_historical_candles.side_effect=CapitalHistoryConnectionError('offline')
        result = BackfillRunner(vendor,repo,control).run([scope(item)])
        self.assertEqual(result['pause_kind'],'connection')
        self.assertEqual(repo.days,[])

    def test_negative_volume_remains_data_error_not_valid_empty(self,_memory):
        item,other,repo,vendor,control=self.case()
        row=candle(date(2026,10,14)); row.volume=-1
        vendor.fetch_historical_candles.side_effect=None
        vendor.fetch_historical_candles.return_value=[row]
        repo.record_data_failure=Mock(return_value=dict(count=1,failed=False,reason='bad volume',details={},
                                                      retry_at='2099-01-01T00:00:00+00:00'))
        result=BackfillRunner(vendor,repo,control).run([scope(item)])
        self.assertEqual(result['pause_kind'],'data')
        self.assertEqual(repo.days,[])
        repo.record_data_failure.assert_called_once()

    def test_empty_observation_commit_acknowledges_native_pin(self,_memory):
        item,other,repo,vendor,control = self.case()
        BackfillRunner(vendor,repo,control).run([scope(item)])
        vendor.checkpoint.assert_called_once_with(True)

    def test_closure_only_window_never_counts_empty_or_calls_vendor(self,_memory):
        item,other,repo,vendor,control = self.case()
        item.update(start_date=date(2026,10,23),end_date=date(2026,10,26),next_date=date(2026,10,23))
        runner = BackfillRunner(vendor,repo,control)
        runner.run_job(item)
        vendor.fetch_historical_candles.assert_not_called()
        self.assertEqual(repo.days,[])

    def test_newer_refresh_can_save_new_bars_before_reaching_old_review(self,_memory):
        item,other,repo,vendor,control = self.case()
        repo.review=True
        item.update(mode='refresh',target_bars=2,count=0,next_date=date(2026,10,22),end_date=date(2026,10,22))
        vendor.fetch_historical_candles.side_effect=lambda s,i,f,l,**kw:[candle(date(2026,10,22))]
        BackfillRunner(vendor,repo,control).run_job(item)
        self.assertEqual(vendor.fetch_historical_candles.call_args.args[2],date(2026,10,16))
        self.assertEqual(item['status'],'waiting_review')
        self.assertEqual(item['count'],1)
        self.assertEqual(item['next_date'],date(2026,10,15))

    def test_invalid_calendar_pauses_instead_of_failing_security(self,_memory):
        item,other,repo,vendor,control=self.case()
        with patch('app.services.backfill_engine.query_trading_bounds',side_effect=TaiwanTradingCalendarError('bad snapshot')):
            result=BackfillRunner(vendor,repo,control).run_job(item)
        self.assertEqual(result['pause_kind'],'calendar')
        self.assertNotEqual(item['status'],'failed')
        vendor.fetch_historical_candles.assert_not_called()


class ReviewApiTests(unittest.TestCase):
    def test_endpoint_is_readonly_and_bounded(self):
        app = FastAPI(); app.include_router(router)
        with patch('app.api.routes.MySQLCandleRepository') as repo:
            repo.return_value.empty_history_report.return_value = dict(storage_ready=True,total=0,items=[])
            code,body = asyncio.run(asgi_request(app,'GET','/api/v1/backfill/empty-reviews?markets=0,1&limit=20&offset=40'))
            self.assertEqual(code,200)
            repo.return_value.empty_history_report.assert_called_once_with([0,1],limit=20,offset=40,include_resolved=False)
            repo.return_value.ensure_empty_review_storage.assert_not_called()
        for query in ('limit=1001','offset=-1','markets=99'):
            with patch('app.api.routes.MySQLCandleRepository') as repo:
                code,_ = asyncio.run(asgi_request(app,'GET','/api/v1/backfill/empty-reviews?'+query))
                self.assertIn(code,(400,422))
                repo.assert_not_called()
