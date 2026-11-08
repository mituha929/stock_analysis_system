"""Listing bounds, transfer fallback, old checkpoints and immutable old rows."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services.backfill_engine import BackfillRunner
from app.services.batch_candle_planning import choose_work
from app.services.day_candle_integrity import DayEvidence, assess_days
from app.services.day_candle_integrity_runner import DayIntegrityAudit
from app.services.daily_candle_aggregation import aggregate_daily
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.listing_history import (MASTER_COLUMNS, listing_policy, listing_floor, parse_listing_origins,
    parse_esb_listing_profiles, ESB_URL)
from app.services.stock_master_sources import TPEX_FIELDS
from test_incremental_backfill import FakeControl, FakeRepository, candle, job
from test_day_candle_integrity import MemoryAuditRepo, sessions

LISTED = date(2026, 10, 19)
END = date(2026, 10, 20)


def profile(**changes):
    return dict(dict(code='1234', market='TPEx', listing_date=LISTED, issuer_id='12345678',
                     security_type='STOCK', presence_status='present', date_status='provided',
                     source_url='https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O'), **changes)


def policy(**changes):
    return listing_policy(profile(**changes), [], today=END)


class ListingPolicyTests(unittest.TestCase):
    def test_known_otc_date_is_inclusive_and_does_not_adjust_target(self):
        self.assertEqual(policy()['history_start'], LISTED)
        work = choose_work('day', END, 0, None, None, {}, 1200, listing_boundary=LISTED)
        self.assertEqual(work['history_floor'], LISTED)
        self.assertTrue(work['needs_history'])

    def test_week_month_floor_keeps_the_listing_period(self):
        row = policy(listing_date=END)
        self.assertEqual(listing_floor(row, 'day'), END)
        self.assertEqual(listing_floor(row, 'week'), date(2026, 10, 19))
        self.assertEqual(listing_floor(row, 'month'), date(2026, 10, 1))

    def test_unknown_bad_future_or_unofficial_date_does_not_limit_history(self):
        for changes in ({'listing_date':None}, {'listing_date':END+timedelta(days=1)},
                        {'date_status':'missing'}, {'source_url':'https://twse.com.tw.evil.invalid/data'},
                        {'presence_status':'not_in_latest'}):
            with self.subTest(changes=changes): self.assertIsNone(policy(**changes))

    def test_twse_stock_requires_exact_official_origin_record(self):
        row = profile(market='TWSE')
        for origins in ({}, {('1234',END):'direct'}, {('1234',LISTED):'transfer'}):
            self.assertIsNone(listing_policy(row, [], origins=origins, today=END)['history_start'])
        self.assertEqual(listing_policy(row, [], origins={('1234',LISTED):'direct'}, today=END)['history_start'],LISTED)

    def test_etf_does_not_need_company_ipo_source(self):
        self.assertEqual(policy(market='TWSE', security_type='ETF')['history_start'], LISTED)

    def test_transfer_uses_known_same_issuer_otc_date_even_if_inactive(self):
        old = profile(listing_date=date(2015,1,1),presence_status='not_in_latest')
        result = listing_policy(profile(market='TWSE'), [old], today=END)
        self.assertEqual(result['history_start'], date(2015,1,1))
        self.assertEqual(result['reason'],'official_predecessor_listing')

    def test_reused_code_is_not_a_predecessor(self):
        old = profile(listing_date=date(2015,1,1),issuer_id='99999999')
        self.assertIsNone(listing_policy(profile(market='TWSE'), [old], today=END)['history_start'])

    def test_existing_older_history_overrides_official_listing_limit(self):
        result = listing_policy(profile(), [], older_history=True, today=END)
        self.assertIsNone(result['history_start'])
        self.assertEqual(result['listing_date'], LISTED)
        self.assertEqual(result['reason'], 'existing_history_precedes_listing')

    def test_origin_parser_distinguishes_transfer_reorganization_and_unknown_dates(self):
        rows = [dict(Code='1234',ApprovedListingDate='1151019',Note='櫃轉市'),
                dict(Code='1235',ApprovedListingDate='2026/10/19',Note='科技事業'),
                dict(Code='1236',ApprovedListingDate='1151019',Note='股份轉換'),
                dict(Code='1237',ApprovedListingDate='',Note='')]
        parsed = parse_listing_origins(rows)
        self.assertEqual(parsed[('1234',LISTED)],'transfer')
        self.assertEqual(parsed[('1235',LISTED)],'direct')
        self.assertEqual(parsed[('1236',LISTED)],'unknown')
        self.assertEqual(len(parsed),3)
        with self.assertRaises(ValueError): parse_listing_origins([dict(Code='1234')])
        with self.assertRaises(ValueError): parse_listing_origins([rows[0],dict(rows[0],Note='')])


class ListingReaderTests(unittest.TestCase):
    def repo(self, batches):
        repo = MySQLCandleRepository.__new__(MySQLCandleRepository)
        cursor = Mock(); cursor.fetchall.side_effect = batches
        @contextmanager
        def transaction(**kwargs): yield cursor
        repo.transaction = transaction
        return repo, cursor

    def test_missing_master_is_optional_and_never_creates_schema(self):
        repo, cursor = self.repo([[]])
        self.assertEqual(repo.load_listing_policies('candles',0),{})
        cursor.execute.assert_called_once()
        self.assertNotIn('CREATE',cursor.execute.call_args.args[0])

    def test_master_reader_limits_only_supported_markets_and_checks_existing_rows(self):
        repo, cursor = self.repo([[dict(COLUMN_NAME=c) for c in MASTER_COLUMNS], [profile()], []])
        result = repo.load_listing_policies('candles',1)
        self.assertEqual(result['1234']['history_start'], LISTED)
        self.assertIn('MIN(candle_time)',cursor.execute.call_args.args[0])
        self.assertIn('GROUP BY symbol,interval_code',cursor.execute.call_args.args[0])
        repo, cursor = self.repo([])
        with patch('app.services.listing_history.fetch_esb_listing_profiles', return_value=()):
            self.assertEqual(repo.load_listing_policies('candles',4),{})
        cursor.execute.assert_not_called()

    def test_esb_uses_official_date_without_requiring_stock_master(self):
        esb = dict(profile(market='ESB', source_url=ESB_URL), name='鑫詮科技')
        repo, q = self.repo([[dict(symbol='1234', name='鑫詮科技')], []])
        with patch('app.services.listing_history.fetch_esb_listing_profiles', return_value=(esb,)):
            selected = repo.load_listing_policies('candles', 4)['1234']
        self.assertEqual(selected['history_start'], LISTED)
        self.assertEqual(selected['reason'], 'official_esb_listing')
        self.assertTrue(all('stock_master' not in c.args[0] for c in q.execute.call_args_list))

    def test_esb_name_mismatch_is_not_guessed_and_existing_older_history_wins(self):
        esb = dict(profile(market='ESB', source_url=ESB_URL), name='官方名稱')
        for name, older, expected in [('其他名稱', [], 'absent'), ('官方名稱',
                [dict(symbol='1234', interval_code='day', earliest=datetime(2026,10,16))], None)]:
            repo, _ = self.repo([[dict(symbol='1234', name=name)], older])
            with patch('app.services.listing_history.fetch_esb_listing_profiles', return_value=(esb,)):
                result = repo.load_listing_policies('candles', 4)
            if expected == 'absent':
                self.assertEqual(result, {})
            else:
                self.assertIsNone(result['1234']['history_start'])


    def test_listing_week_month_labels_do_not_disable_limit_but_older_periods_do(self):
        columns = [dict(COLUMN_NAME=c) for c in MASTER_COLUMNS]
        row = profile(listing_date=END)
        starts = [dict(symbol='1234',interval_code='week',earliest=datetime(2026,10,19)),
                  dict(symbol='1234',interval_code='month',earliest=datetime(2026,10,1))]
        repo,_ = self.repo([columns,[row],starts])
        self.assertEqual(repo.load_listing_policies('candles',1)['1234']['history_start'],END)
        repo,_ = self.repo([columns,[row],starts+[dict(symbol='1234',interval_code='month',earliest=datetime(2026,9,1))]])
        self.assertIsNone(repo.load_listing_policies('candles',1)['1234']['history_start'])

    def test_odd_lot_uses_its_own_candle_table_and_corresponding_market(self):
        repo,q = self.repo([[dict(COLUMN_NAME=c) for c in MASTER_COLUMNS],[profile()],[]])
        self.assertEqual(repo.load_listing_policies('odd_lot_candle',6)['1234']['history_start'],LISTED)
        self.assertIn('`odd_lot_candle`',q.execute.call_args.args[0])

    def test_valid_otc_history_between_predecessor_and_twse_dates_keeps_earlier_limit(self):
        columns=[dict(COLUMN_NAME=c) for c in MASTER_COLUMNS]
        records=[profile(market='TWSE'),profile(listing_date=date(2026,10,15))]
        old=[dict(symbol='1234',interval_code='day',earliest=datetime(2026,10,16))]
        repo,_=self.repo([columns,records,old])
        self.assertEqual(repo.load_listing_policies('candles',0)['1234']['history_start'],date(2026,10,15))


class ESBSourceTests(unittest.TestCase):
    def row(self, **changes):
        row = dict.fromkeys(TPEX_FIELDS, '')
        row.update(SecuritiesCompanyCode='7954', CompanyAbbreviation='鑫詮科技',
                   Date='1151020', DateOfListing='20261019', DateOfIncorporation='19900101')
        row['UnifiedBusinessNo.'] = '12345678'
        row.update(changes)
        return row

    def test_only_admission_date_not_company_incorporation_date_is_used(self):
        row = parse_esb_listing_profiles([self.row()], END.isoformat())[0]
        self.assertEqual(row['listing_date'], LISTED)
        self.assertEqual(row['market'], 'ESB')
        self.assertEqual(row['source_url'], ESB_URL)

    def test_unknown_or_future_admission_is_not_a_historical_limit(self):
        self.assertEqual(parse_esb_listing_profiles([self.row(DateOfListing='')], END.isoformat()), ())
        self.assertEqual(parse_esb_listing_profiles([self.row(DateOfListing='20261021')], END.isoformat()), ())

    def test_changed_schema_duplicate_code_future_source_and_bad_dates_stop_batch(self):
        bad = self.row(); bad.pop('DateOfListing')
        for rows in ([bad], [self.row(), self.row()], [self.row(Date='1151021')],
                     [self.row(DateOfListing='20260230')], []):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    parse_esb_listing_profiles(rows, END.isoformat())


class PolicyFake(FakeRepository):
    def __init__(self, item, selected):
        super().__init__([item]); self.selected=selected
    def load_listing_policies(self, table, market): return {self.items[0]['symbol']:self.selected}


class ListingWorkerTests(unittest.TestCase):
    def execute(self,item,selected,fetch):
        repo=PolicyFake(item,selected); control=FakeControl()
        runner=BackfillRunner(SimpleNamespace(fetch_historical_candles=fetch),repo,control)
        runner.stage_check=lambda stage:False
        runner.run_job(item)
        return repo,control,runner

    def test_old_backward_checkpoint_stops_at_listing_and_keeps_actual_bars(self):
        item=job('initialization','day',date(2018,1,1),END)
        fetch=Mock(return_value=[candle(LISTED),candle(END)])
        repo,control,_=self.execute(item,policy(),fetch)
        self.assertEqual(fetch.call_args.args[2:],(LISTED,END))
        self.assertEqual(item['target_bars'],1200)
        self.assertEqual(item['status'],'insufficient_history')
        self.assertEqual(len(repo.rows),2)
        self.assertTrue(control.state['listing_history_shortfall'])

    def test_resumed_cursor_already_before_listing_never_requests_again(self):
        item=job('initialization','day',date(2018,1,1),END)
        item['next_date']=LISTED-timedelta(days=1)
        fetch=Mock()
        self.execute(item,policy(),fetch)
        fetch.assert_not_called()
        self.assertTrue(item['listing_boundary_reached'])

    def test_old_forward_checkpoint_is_clamped_without_rewinding(self):
        item=job('incremental','day',date(2018,1,1),END)
        fetch=Mock(return_value=[])
        self.execute(item,policy(),fetch)
        self.assertEqual(fetch.call_args.args[2:],(LISTED,END))

    def test_unknown_date_keeps_original_requested_range(self):
        item=job('incremental','day',LISTED-timedelta(days=3),END)
        fetch=Mock(return_value=[])
        self.execute(item,None,fetch)
        self.assertEqual(fetch.call_args.args[2],LISTED-timedelta(days=3))

    def test_multiple_empty_windows_still_stop_at_the_inclusive_listing_floor(self):
        first = date(2026,10,1)
        item=job('initialization','1m',date(2010,1,1),END)
        fetch=Mock(return_value=[])
        self.execute(item,policy(listing_date=first),fetch)
        self.assertEqual(fetch.call_args.args[2],first)
        self.assertTrue(all(call.args[2]>=first for call in fetch.call_args_list))
        self.assertTrue(item['listing_boundary_reached'])

    def test_transfer_fallback_continues_pre_twse_history(self):
        item=job('initialization','day',LISTED-timedelta(days=4),END)
        selected=listing_policy(profile(market='TWSE'),[],origins={('1234',LISTED):'transfer'},today=END)
        fetch=Mock(return_value=[candle(LISTED-timedelta(days=1))])
        repo,_,_=self.execute(item,selected,fetch)
        self.assertLess(fetch.call_args.args[2],LISTED)
        self.assertEqual(len(repo.rows),1)
        self.assertFalse(item['listing_boundary_reached'])

    def test_native_week_month_keeps_listing_period_bar_with_earlier_label(self):
        for interval,label in (('week',date(2026,10,19)),('month',date(2026,10,1))):
            with self.subTest(interval=interval):
                item=job('initialization',interval,date(2010,1,1),date(2026,10,30))
                fetch=Mock(return_value=[candle(label)])
                repo,_,_=self.execute(item,policy(listing_date=END),fetch)
                self.assertEqual(fetch.call_args.args[2],END)  # Query starts at actual listing; label is retained.
                self.assertEqual(len(repo.rows),1)

    def test_daily_derived_queries_only_from_listing_but_keeps_earlier_period_label(self):
        for interval,first in (('week',date(2026,10,19)),('month',date(2026,10,1))):
            vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle(END)]),history_checkpoint=Mock())
            repo=SimpleNamespace(commit_daily_source=Mock())
            runner=BackfillRunner(vendor,repo,FakeControl()); runner.stage_check=lambda stage:False
            item=dict(job(),listing_policy=policy(listing_date=END),interval_code=interval)
            result=runner.fetch_daily_aggregate_window(item,first,END)
            self.assertEqual(vendor.fetch_historical_candles.call_args.args[2],END)
            self.assertEqual(result[0].timestamp.date(),first)


class ListingWriteTests(unittest.TestCase):
    def test_protected_history_is_insert_only_and_regular_bars_still_upsert(self):
        q=Mock()
        MySQLCandleRepository._write_rows(q,'candles',dict(symbol='1234',interval_code='day',listing_preserve_before=LISTED),
                                         [candle(LISTED-timedelta(days=1)),candle(LISTED)],datetime.now(timezone.utc))
        self.assertEqual(q.executemany.call_count,2)
        old,new=q.executemany.call_args_list
        self.assertIn('ON DUPLICATE KEY UPDATE symbol=symbol',old.args[0])
        self.assertNotIn('IGNORE',old.args[0])
        self.assertIn('close_price=VALUES(close_price)',new.args[0])
        self.assertEqual(old.args[1][0][2].date(),LISTED-timedelta(days=1))
        self.assertEqual(new.args[1][0][2].date(),LISTED)

    def test_old_week_month_alias_is_never_overwritten_or_canonicalized(self):
        repo=MySQLCandleRepository.__new__(MySQLCandleRepository)
        for interval,label in (('week',date(2026,10,12)),('month',date(2026,9,1))):
            q=Mock(); q.fetchone.return_value=None
            q.fetchall.return_value=[(datetime.combine(label,datetime.min.time()),10,12,9,11,100,'capital_history',datetime(2026,10,20))]
            item=dict(symbol='1234',interval_code=interval,end_date=END,listing_preserve_before=END,candle_source='capital_day_partial')
            repo._write_daily_aggregates(q,'candles',item,[candle(label)],datetime.now(timezone.utc))
            q.executemany.assert_not_called()
            self.assertTrue(all(call.args[0].startswith('SELECT') for call in q.execute.call_args_list))

    def test_listing_period_itself_remains_refreshable_despite_earlier_label(self):
        repo=MySQLCandleRepository.__new__(MySQLCandleRepository)
        for interval,label in (('week',date(2026,10,19)),('month',date(2026,10,1))):
            q=Mock(); q.fetchone.return_value=None
            q.fetchall.return_value=[(datetime.combine(label,datetime.min.time()),10,12,9,11,100,'capital_day_partial',datetime(2026,10,20))]
            item=dict(symbol='1234',interval_code=interval,end_date=END,listing_preserve_before=END)
            rows=aggregate_daily([candle(END)],interval,label,END)
            repo._write_daily_aggregates(q,'candles',item,rows,datetime.now(timezone.utc))
            q.executemany.assert_called_once()
            self.assertIn('close_price=VALUES(close_price)',q.executemany.call_args.args[0])


class ListingAuditTests(unittest.TestCase):
    def test_verified_short_listing_history_is_not_claimed_to_be_1200(self):
        result=assess_days([LISTED,END],END,1200,date(2020,1,1),1,'1234',DayEvidence(),sessions(),listing=policy())
        self.assertEqual(result['status'],'complete_available_history')
        self.assertEqual(result['history_limit_reason'],'掛牌歷史不足')
        self.assertEqual(result['stored_bars'],2)
        self.assertEqual(result['target_bars'],1200)
        self.assertEqual(result['issues'],[])
        self.assertIsNone(result['first_trade'])

    def test_empty_listing_day_remains_a_real_gap(self):
        result=assess_days([END],END,1200,date(2020,1,1),1,'1234',DayEvidence(),sessions(),listing=policy())
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual([r['day'] for r in result['issues']],[LISTED])

    def test_existing_pre_listing_data_is_not_a_listing_evidence_conflict(self):
        result=assess_days([date(2026,10,16),LISTED,END],END,3,date(2020,1,1),1,'1234',DayEvidence(),sessions(),listing=policy())
        self.assertEqual(result['status'],'complete')
        self.assertIsNone(result['listing_boundary'])

    def test_old_holiday_audit_floor_and_gap_repairs_are_clamped(self):
        class Repo(MemoryAuditRepo):
            def load_listing_policies(self, table, market): return {'NEW':policy()}
            def commit_day_gap_window(self,*args,**kwargs):
                self.policy=kwargs['listing_policy']
                return super().commit_day_gap_window(*args)
        repo=Repo([END]); vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle(LISTED)]))
        runner=BackfillRunner(vendor,repo,FakeControl()); runner.stage_check=lambda stage:False
        scope=dict(market_no=1,interval='day',end_date=END,target_bars=1200,batch_v2=True,candle_table='candles')
        with patch('app.services.day_candle_integrity_runner.DayEvidence.load',return_value=DayEvidence()), \
             patch('app.services.day_candle_integrity_runner.DayCalendar',side_effect=lambda:sessions()):
            result=DayIntegrityAudit(runner).run([scope])
        self.assertEqual(vendor.fetch_historical_candles.call_args.args[2:],(LISTED,LISTED))
        self.assertEqual(result['blocked'],0)
        self.assertEqual(repo.saved['search_start'],LISTED)
        self.assertEqual(repo.saved['history_limit_reason'],'掛牌歷史不足')


if __name__ == '__main__':
    unittest.main()
