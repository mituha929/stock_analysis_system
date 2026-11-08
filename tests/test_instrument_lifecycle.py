"""Lifetimes and positive evidence: fake SQL/quotes; no real network or login."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services.backfill_engine import BackfillRunner
from app.services.day_candle_integrity import DayEvidence, assess_days
from app.services.empty_history_review import missing_periods
from app.services.instrument_lifecycle import (
    ISSUE_FIELDS, ISSUES_URL, DELIST_URL, active_bounds, blocked_reason,
    parse_bond_issues, parse_bond_delist, InstrumentLifecycleRepository,
)
from app.services.incremental_candle_repository import MySQLCandleRepository
from app.services.trade_activity import classify_missing
from test_incremental_backfill import FakeControl, FakeRepository, job, candle
from test_day_candle_integrity import sessions

NOW = date(2026, 10, 26)
LISTED = date(2026, 10, 20)


def issue(**changes):
    row = dict.fromkeys(ISSUE_FIELDS, '')
    row.update(Date='20261026', BondCode='17272', BondType='5', ListingStatus='2',
               ShortName='中華化二', ListingDate='20261020', MaturityDate='20311020',
               IssueDate='20261001', IssueAmount='1000000000', OutstandingAmount='1000000000')
    row.update(changes)
    return row


def policy(**changes):
    return dict(dict(history_start=LISTED, listing_date=LISTED, maturity_date=date(2031,10,20),
                termination_date=None, trading_status='unknown', reason='official_bond_listing',
                source=ISSUES_URL), **changes)


class SourceTests(unittest.TestCase):
    def test_actual_listing_not_issue_or_option_date_is_used(self):
        row = parse_bond_issues([issue(PutOptionDate='20291020')], NOW)['17272']
        self.assertEqual(row['listing_date'], LISTED)
        self.assertEqual(row['maturity_date'], date(2031,10,20))
        self.assertEqual(row['trading_status'], 'unknown')

    def test_missing_date_is_null_and_future_listing_retained_as_future(self):
        row = parse_bond_issues([issue(ListingDate='', MaturityDate='')], NOW)['17272']
        self.assertIsNone(row['listing_date']); self.assertIsNone(row['maturity_date'])
        row = parse_bond_issues([issue(ListingDate='20261029')], NOW)['17272']
        self.assertEqual(row['listing_date'], date(2026,10,29))

    def test_private_or_blank_bond_is_not_guessed_from_issuer_code(self):
        self.assertEqual(parse_bond_issues([issue(BondCode=''),issue(BondCode='YI31AA',ListingStatus='5')],NOW),{})

    def test_schema_drift_duplicate_and_invalid_date_fail_whole_batch(self):
        missing = issue(); missing.pop('ListingDate')
        for rows in ([missing], [issue(),issue()], [issue(MaturityDate='20310230')],
                     [issue(Date='20261027')], [issue(OutstandingAmount='NaN')],
                     [issue(OutstandingAmount='-1')], [issue(ListingDate='20321020')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_bond_issues(rows,NOW)

    def test_partial_conversion_and_redemption_are_not_termination(self):
        for text in ('公司債轉換', '公司債買回註銷', '贖回條款', '停止轉換'):
            row=parse_bond_issues([issue(OutstandingChangeDescription=text,OutstandingChangeDate='20261020')],NOW)['17272']
            self.assertEqual(row['trading_status'],'unknown')

    def test_zero_requires_explicit_dated_cause_not_just_empty_or_zero_balance(self):
        for text, expected in (('公司債轉換','fully_converted'),('公司債買回註銷','redeemed'),('','unknown')):
            row=parse_bond_issues([issue(OutstandingAmount='0',OutstandingChangeDescription=text,OutstandingChangeDate='20261020')],NOW)['17272']
            self.assertEqual(row['trading_status'],expected)
        row=parse_bond_issues([issue(OutstandingAmount='0',OutstandingChangeDescription='公司債轉換')],NOW)['17272']
        self.assertEqual(row['trading_status'],'unknown')
        for description in ('停止轉換','轉換條款','調整轉換價格','贖回權條款'):
            row=parse_bond_issues([issue(OutstandingAmount='0',OutstandingChangeDescription=description,OutstandingChangeDate='20261020')],NOW)['17272']
            self.assertEqual(row['trading_status'],'unknown')

    def test_delist_is_first_nontrading_date_not_last_trading_or_announcement_date(self):
        payload=dict(stat='ok',tables=[dict(fields=['代碼','簡稱','下櫃日期'],data=[['17272','中華化二','115/10/27']],totalCount=1)])
        result=parse_bond_delist(payload,NOW)['17272']
        self.assertEqual(result['termination_date'],date(2026,10,27))
        self.assertIsNone(blocked_reason(result,NOW))
        self.assertEqual(blocked_reason(result,date(2026,10,27)),'terminated')
        payload['tables'][0]['totalCount']=2
        with self.assertRaises(ValueError): parse_bond_delist(payload,NOW)


class LifetimeTests(unittest.TestCase):
    def test_listing_and_maturity_inclusive_termination_exclusive(self):
        p=policy(maturity_date=date(2026,10,22))
        self.assertIsNone(blocked_reason(p,date(2026,10,22)))
        self.assertEqual(blocked_reason(p,date(2026,10,23)),'matured')
        self.assertEqual(active_bounds(p,date(2020,1,1),NOW),(LISTED,date(2026,10,22)))
        self.assertEqual(active_bounds(dict(p,termination_date=date(2026,10,21)),LISTED,NOW),(LISTED,LISTED))
        self.assertIsNone(active_bounds(p,date(2017,1,1),date(2018,1,1)))

    def test_unknown_is_not_inactive_and_future_is_not_historical(self):
        self.assertIsNone(blocked_reason(None,NOW))
        self.assertEqual(blocked_reason(policy(history_start=date(2026,10,29)),NOW),'not_yet_listed')

    def test_full_conversion_or_redemption_without_end_date_stops_now_but_not_guessed_gap_dates(self):
        for state in ('fully_converted','redeemed'):
            p=policy(trading_status=state)
            self.assertEqual(blocked_reason(p,NOW),state)
            self.assertEqual(active_bounds(p,LISTED,NOW),(LISTED,NOW))

    def test_prelisting_periods_never_enter_observation_but_listing_month_remains(self):
        self.assertEqual(list(missing_periods('day',date(2026,10,1),date(2026,10,19),policy())),[])
        self.assertEqual(list(missing_periods('month',date(2026,9,1),date(2026,10,22),policy())),
                         [(date(2026,10,1),date(2026,10,31))])

    def test_matured_dates_not_observed(self):
        p=policy(maturity_date=LISTED)
        self.assertEqual(list(missing_periods('day',date(2026,10,21),date(2026,10,22),p)),[])


class WorkerRepo(FakeRepository):
    def __init__(self,item,p): super().__init__([item]); self.policy=p; self.stops=[]
    def load_listing_policies(self,*_args): return {self.items[0]['symbol']:self.policy}
    def stop_lifecycle_job(self,item,reason): item['status']='superseded'; self.stops.append(reason)


class WorkerTests(unittest.TestCase):
    def execute(self,p,interval='day',cursor=None):
        item=job('initialization',interval,date(2017,1,1),date(2026,10,22))
        if cursor: item['next_date']=cursor
        repo=WorkerRepo(item,p); vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle(LISTED)]))
        runner=BackfillRunner(vendor,repo,FakeControl()); runner.stage_check=lambda _:False
        runner.run_job(item)
        return item,repo,vendor,runner

    def test_matured_old_saved_job_never_queries_or_fakes_complete(self):
        item,repo,vendor,runner=self.execute(policy(maturity_date=date(2020,1,1)))
        vendor.fetch_historical_candles.assert_not_called()
        self.assertEqual(item['status'],'superseded'); self.assertEqual(repo.stops,['matured'])
        self.assertEqual(item['next_date'],date(2026,10,22)); self.assertEqual(runner.result['completed'],0)

    def test_early_exit_before_maturity_and_full_conversion_stop(self):
        for p in (policy(termination_date=LISTED),policy(trading_status='fully_converted'),policy(trading_status='redeemed')):
            _,repo,vendor,_=self.execute(p)
            vendor.fetch_historical_candles.assert_not_called(); self.assertEqual(len(repo.stops),1)

    def test_existing_cursor_before_listing_finishes_without_empty_wait(self):
        item,repo,vendor,_=self.execute(policy(),cursor=date(2018,1,1))
        vendor.fetch_historical_candles.assert_not_called()
        self.assertEqual(item['status'],'insufficient_history')
        self.assertTrue(item['listing_boundary_reached'])

    def test_week_month_queries_clamped_but_bar_label_is_retained(self):
        for interval,label in (('week',date(2026,10,19)),('month',date(2026,10,1))):
            item=job('initialization',interval,date(2017,1,1),date(2026,10,22))
            repo=WorkerRepo(item,policy()); vendor=SimpleNamespace(fetch_historical_candles=Mock(return_value=[candle(label)]))
            runner=BackfillRunner(vendor,repo,FakeControl()); runner.stage_check=lambda _:False
            runner.run_job(item)
            self.assertEqual(vendor.fetch_historical_candles.call_args.args[2],LISTED)
            self.assertEqual(len(repo.rows),1)


class EvidenceTests(unittest.TestCase):
    def test_observer_itself_refuses_prelisting_even_if_called_directly(self):
        repo=MySQLCandleRepository.__new__(MySQLCandleRepository); q=Mock()
        result=repo._observe_empty(q,dict(listing_policy=policy()),date(2017,1,1),date(2017,1,2),datetime.now(timezone.utc))
        q.execute.assert_not_called()
        self.assertEqual(result['days'],[])
        self.assertEqual(result['classification'],'outside_instrument_lifetime')

    def test_prelisting_positive_legacy_bar_not_used_as_evidence_inside_listing_month(self):
        repo=MySQLCandleRepository.__new__(MySQLCandleRepository); q=Mock(); q.fetchone.return_value=None
        item=dict(candle_table='candles',market_no=1,symbol='17272',interval_code='month',listing_policy=policy(),
                  trade_activity={date(2026,10,1):'legacy_positive'})
        with patch('app.services.empty_history_review.updated_observation',return_value=([],False)), \
             patch('app.services.empty_history_review.next_observation_at',return_value=None):
            result=repo._observe_empty(q,item,date(2026,10,1),date(2026,10,31),datetime.now(timezone.utc))
        self.assertFalse(result['trade_confirmed'])

    def test_before_listing_asof_is_not_reported_as_complete(self):
        result=assess_days([],date(2026,10,19),1200,date(2017,1,1),1,'17272',DayEvidence(),sessions(),listing=policy())
        self.assertEqual(result['status'],'not_listed_as_of'); self.assertEqual(result['issues'],[])

    def test_positive_evidence_only_in_exact_missing_date_range(self):
        activity={LISTED:'positive_in_same_market_stream'}
        self.assertEqual(classify_missing(activity,LISTED,LISTED)['classification'],'api_missing_confirmed')
        self.assertEqual(classify_missing(activity,date(2026,10,21),date(2026,10,22))['classification'],'trade_activity_unconfirmed')
        self.assertFalse(classify_missing({},LISTED,LISTED)['trade_confirmed'])

    def test_daily_assessment_distinguishes_unknown_and_proven_trade(self):
        result=assess_days([],date(2026,10,22),1200,date(2017,1,1),1,'17272',DayEvidence(),sessions(),
                           listing=policy(),activity={LISTED:'stored_positive_volume:candles'})
        self.assertEqual(result['search_start'],LISTED)
        self.assertEqual(result['api_missing_dates'],1)
        self.assertEqual(result['activity_unconfirmed_dates'],2)
        self.assertEqual(result['issues'][0]['reason'],'api_missing_confirmed')

    def test_post_maturity_and_prelisting_not_gaps_existing_quotes_retained(self):
        result=assess_days([date(2017,1,1),LISTED],date(2026,10,22),1200,date(2017,1,1),1,'17272',
                          DayEvidence(),sessions(),listing=policy(maturity_date=LISTED))
        self.assertEqual(result['issues'],[])
        self.assertEqual(result['stored_bars'],2)
        self.assertEqual(result['search_start'],LISTED)

    def test_trade_query_does_not_use_aggregate_volume_or_other_market_stream(self):
        repo=MySQLCandleRepository.__new__(MySQLCandleRepository); q=Mock(); q.fetchall.return_value=[(LISTED,)]
        result=repo.trade_activity_dates('odd_lot_candle','17272',LISTED,NOW,cursor=q)
        sql=q.execute.call_args.args[0]
        self.assertIn('`odd_lot_candle`',sql); self.assertIn('volume>0',sql)
        self.assertNotIn("'week'",sql); self.assertNotIn("'month'",sql)
        self.assertIn(LISTED,result)


class PersistenceTests(unittest.TestCase):
    def repository(self):
        repo=MySQLCandleRepository.__new__(MySQLCandleRepository); q=Mock(); q.fetchone.return_value=(1,)
        q.fetchall.return_value=[('17272',)]
        @contextmanager
        def transaction(**_kwargs): yield q
        repo.transaction=transaction
        return repo,q

    def test_sync_is_additive_and_does_not_clear_unknown_missing_old_fields(self):
        repo,q=self.repository(); rows=tuple(parse_bond_issues([issue()],NOW).values())
        with patch('app.services.instrument_lifecycle.fetch_bond_lifecycles',return_value=rows):
            repo.refresh_lifecycle(); repo.refresh_lifecycle()
        self.assertEqual(q.executemany.call_count,1)
        sql=q.executemany.call_args.args[0]
        self.assertIn('COALESCE(VALUES(maturity_date),maturity_date)',sql)
        self.assertIn('COALESCE(VALUES(termination_date),termination_date)',sql)
        self.assertNotIn('candles',sql)

    def test_reconcile_only_fully_outside_period_no_cursor_or_quote_write(self):
        repo,q=self.repository()
        repo.reconcile_lifecycle_reviews('candles',1,{'17272':policy()})
        sql=' '.join(c.args[0] for c in q.executemany.call_args_list)
        self.assertIn('window_end<%s',sql)
        self.assertIn("status='confirmed_no_bar'",sql)
        all_sql=' '.join(c.args[0] for c in q.execute.call_args_list+q.executemany.call_args_list)
        self.assertNotIn('next_date=',all_sql); self.assertNotIn('DELETE',all_sql)
        self.assertIn('NOT EXISTS',all_sql)


if __name__=='__main__': unittest.main()
