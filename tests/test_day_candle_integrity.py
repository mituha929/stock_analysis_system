from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services.day_candle_integrity import DayCalendar, DayEvidence, assess_days, expanded_day_window
from app.services.day_candle_integrity_runner import DayIntegrityAudit
from app.services.backfill_engine import BackfillRunner
from app.services.candle_validation import CandleDataError
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.services.trading_suspensions import SuspensionCalendar
from test_incremental_backfill import FakeControl, candle

END = date(2026, 10, 20)
SOURCE = 'https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule'


def sessions():
    return DayCalendar({'source': SOURCE, 'years': {'2026': []}}, overrides=[])


def proof(symbol='NEW', kind='first_trade', first=date(2026, 10, 19), last=None):
    return dict(market_no=0, symbol=symbol, kind=kind, start_date=str(first), end_date=str(last or first),
                source='https://www.twse.com.tw/approved-announcement', reason='人工逐筆核對首次交易公告',
                reviewed=True, reviewed_on=str(END))


class DailyIntegrityPolicyTests(unittest.TestCase):
    def assess(self, days, goal=3, **kwargs):
        return assess_days(days, END, goal, date(2026, 10, 16), 0, 'NEW',
                           kwargs.get('evidence', DayEvidence()), kwargs.get('sessions', sessions()))

    def test_exact_count_does_not_hide_an_interior_missing_day(self):
        result=self.assess([date(2026,10,15),date(2026,10,16),END])
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual([i['day'] for i in result['issues']],[date(2026,10,19)])

    def test_trailing_missing_latest_day_is_listed(self):
        result=self.assess([date(2026,10,15),date(2026,10,16),date(2026,10,19)])
        self.assertIn(END,[i['day'] for i in result['issues']])

    def test_weekends_do_not_become_missing_candles(self):
        result=self.assess([date(2026,10,16),date(2026,10,19),END])
        self.assertEqual(result['status'],'complete')

    def test_future_rows_and_duplicate_dates_do_not_meet_goal(self):
        result=self.assess([END,END,END+timedelta(days=1)])
        self.assertEqual(result['stored_bars'],1)
        self.assertNotEqual(result['status'],'complete')

    def test_first_stored_date_is_never_treated_as_listing_proof(self):
        result=self.assess([date(2026,10,19),END],goal=1200)
        self.assertNotEqual(result['status'],'complete_available_history')
        self.assertIsNone(result['first_trade'])

    def test_verified_first_ever_trade_can_explain_short_history_but_not_claim_1200(self):
        result=self.assess([date(2026,10,19),END],goal=1200,evidence=DayEvidence([proof()]))
        self.assertEqual(result['status'],'complete_available_history')
        self.assertEqual(result['stored_bars'],2)
        self.assertEqual(result['issues'],[])

    def test_listing_evidence_conflicting_with_existing_older_bar_is_blocked(self):
        result=self.assess([date(2026,10,16),date(2026,10,19),END],evidence=DayEvidence([proof()]))
        self.assertEqual(result['status'],'evidence_conflict')

    def test_unverified_calendar_cannot_certify_coverage(self):
        result=self.assess([date(2026,10,16),date(2026,10,19),END],
                           sessions=DayCalendar({'years':{'2026':[]}},overrides=[]))
        self.assertEqual(result['status'],'calendar_unverified')

    def test_no_trade_requires_reviewed_official_evidence_not_an_empty_reply(self):
        evidence=DayEvidence([proof(kind='no_trade',first=date(2026,10,19))])
        result=self.assess([date(2026,10,15),date(2026,10,16),END],evidence=evidence)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['confirmed'][0]['reason'],'official_no_trade')
        self.assertEqual(result['confirmed'][0]['day'],date(2026,10,19))

    def test_historical_split_halt_explains_only_the_exact_security_and_market(self):
        source = 'https://www.twse.com.tw/official-split-notice.pdf'
        def record(market):
            return dict(market_no=market, symbol='0050', kind='no_trade', start_date='2025-06-11',
                        end_date='2025-06-17', source=source, reason='核對完整分割停牌期間',
                        reviewed=True, reviewed_on=date.today().isoformat())
        present = [date(2025,6,10), date(2025,6,18)]
        for market, records, expected in ((0, [record(0)], 0), (5, [record(0)], 5),
                                          (5, [record(0),record(5)], 0), (0, [record(5)], 5)):
            result = assess_days(present, date(2025,6,18), 2, date(2025,6,10), market, '0050',
                                 DayEvidence(records), DayCalendar({'source':SOURCE, 'years':{'2025':[]}}, overrides=[]))
            self.assertEqual(result['unresolved_count'], expected)
            if expected == 0:
                self.assertEqual(len(result['confirmed']), 5)
                self.assertEqual(result['stored_bars'], 2)
                self.assertEqual(result['status'], 'complete')

    def test_odd_lot_requires_its_own_no_trade_evidence(self):
        result=assess_days([date(2026,10,19),END],END,1200,date(2026,10,16),5,'NEW',
                           DayEvidence([proof()]),sessions())
        self.assertEqual(result['status'],'complete_available_history')
        result=assess_days([date(2026,10,15),date(2026,10,16),END],END,3,date(2026,10,16),5,'NEW',
                           DayEvidence([proof(kind='no_trade',first=date(2026,10,19))]),sessions())
        self.assertEqual(result['status'],'unresolved')

    def test_bar_on_official_closed_day_is_a_conflict_not_a_completeness_pass(self):
        closed=DayCalendar({'source':SOURCE,'years':{'2026':['2026-10-19']}},overrides=[])
        result=self.assess([date(2026,10,16),date(2026,10,19),END],sessions=closed)
        self.assertEqual(result['status'],'calendar_conflict')
        self.assertEqual(result['calendar_conflicts'],[date(2026,10,19)])

    def test_invalid_unreviewed_nonofficial_and_conflicting_proof_rejected(self):
        for changes in ({'reviewed':False},{'source':'https://twse.com.tw.evil.invalid/proof'},
                        {'source':'http://www.twse.com.tw/proof'},{'symbol':'*'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                DayEvidence([dict(proof(),**changes)])
        with self.assertRaises(ValueError):
            DayEvidence([proof(),proof(first=date(2026,10,18))])

    def test_windows_expand_without_replaying_already_counted_dates_and_respect_cap(self):
        cursor=END; floor=date(2021,1,1); widths=[]
        for empty in range(5):
            first,last=expanded_day_window(cursor,floor,empty,60)
            widths.append((last-first).days+1)
            self.assertEqual(last,cursor)
            cursor=first-timedelta(days=1)
        self.assertEqual(widths,[14,30,60,60,60])
        self.assertEqual(expanded_day_window(END,END,5,60),(END,END))


class MemoryAuditRepo:
    """Same audit boundary/atomicity contract as the SQL mixin, no SQL/DLL."""
    def __init__(self, days):
        self.days=set(days); self.saved=None; self.gap=None; self.commits=[]; self.fail=False; self.errors=[]
    def ensure_day_integrity_storage(self): pass
    def day_integrity_symbols(self, plan): return ['NEW']
    def day_integrity_dates(self,*args): return sorted(self.days)[-args[-1]:]
    def previous_day_assessment(self,*args):
        return None if self.saved is None else dict(as_of_date=self.saved['as_of'],target_bars=self.saved['target_bars'],
                                                   search_start=self.saved['search_start'],status=self.saved['status'])
    def save_day_assessment(self,table,market,symbol,result,present,owner):
        self.saved=deepcopy(result)
        if result['issues']:
            old=self.gap
            self.gap=dict(result['issues'][-1],candle_date=result['issues'][-1]['day'],empty_windows=old['empty_windows'] if old else 0)
        else: self.gap=None
    def due_day_gap(self,*args): return self.gap
    def commit_day_gap_window(self,table,market,symbol,first,last,rows,owner,retry):
        if self.fail: raise MySQLRepositoryError('atomic rollback')
        self.days.update(row.timestamp.date() for row in rows)
        self.commits.append((first,last,len(rows)))
        if self.gap: self.gap['empty_windows']+=1
    def finish_verified_short_day_jobs(self,*args): pass
    def record_day_gap_data_error(self,*args): self.errors.append(args)


class DailyIntegrityRunnerTests(unittest.TestCase):
    def run_audit(self,repo,rows=None,error=None):
        class Vendor:
            def __init__(self):
                self.fetch=Mock(return_value=rows or [],side_effect=error)
                self.checkpoint=Mock()
            def fetch_historical_candles(self,*args,**kwargs): return self.fetch(*args,**kwargs)
            def history_checkpoint(self,committed): self.checkpoint(committed)
        vendor=Vendor()
        control=FakeControl(); runner=BackfillRunner(vendor,repo,control)
        runner.suspensions=SuspensionCalendar(); runner.stage_check=lambda stage:False
        plan=dict(market_no=0,interval='day',end_date=END,target_bars=3,batch_v2=True,candle_table='candles')
        with patch('app.services.day_candle_integrity_runner.DayEvidence.load',return_value=DayEvidence()), \
             patch('app.services.day_candle_integrity_runner.DayCalendar',side_effect=lambda:sessions()), \
             patch('app.services.day_candle_integrity_runner.initial_start',return_value=date(2026,10,16)):
            result=DayIntegrityAudit(runner).run([plan])
        return result,vendor,control

    def test_targeted_missing_day_is_written_and_reaudited_before_passing(self):
        repo=MemoryAuditRepo([date(2026,10,15),date(2026,10,16),END])
        result,vendor,_=self.run_audit(repo,[candle(date(2026,10,19))])
        self.assertEqual(result['blocked'],0)
        self.assertEqual(repo.saved['status'],'complete')
        self.assertEqual(len(repo.commits),1)
        vendor.checkpoint.assert_called_once_with(True)

    def test_empty_reply_stays_unresolved_and_checkpoint_is_preserved(self):
        repo=MemoryAuditRepo([date(2026,10,15),date(2026,10,16),END])
        result,vendor,_=self.run_audit(repo)
        self.assertEqual(result['blocked'],1)
        self.assertEqual(repo.saved['status'],'unresolved')
        self.assertEqual(len(repo.days),3)
        vendor.checkpoint.assert_called_once_with(True)

    def test_database_rollback_never_acknowledges_window(self):
        repo=MemoryAuditRepo([date(2026,10,15),date(2026,10,16),END]); repo.fail=True
        with self.assertRaises(MySQLRepositoryError): self.run_audit(repo,[candle(date(2026,10,19))])
        self.assertNotIn(date(2026,10,19),repo.days)
        self.assertEqual(repo.commits,[])

    def test_native_data_error_is_recorded_not_treated_as_no_trade(self):
        repo=MemoryAuditRepo([date(2026,10,15),date(2026,10,16),END])
        result,_,_=self.run_audit(repo,error=CandleDataError('bad volume'))
        self.assertEqual(result['blocked'],1)
        self.assertEqual(len(repo.errors),1)
        self.assertEqual(repo.commits,[])

    def test_bad_data_requests_still_consume_the_bounded_window_budget(self):
        repo=MemoryAuditRepo([date(2026,10,15),date(2026,10,16),END])
        repo.day_integrity_symbols=lambda plan:['NEW','BAD']
        with patch.dict('os.environ',{'CAPITAL_DAY_GAP_WINDOW_BUDGET':'1'}):
            result,vendor,_=self.run_audit(repo,error=CandleDataError('bad volume'))
        self.assertEqual(vendor.fetch.call_count,1)
        self.assertEqual(result['summary']['query_windows'],1)
        self.assertEqual(result['summary']['repaired_windows'],0)

    def test_explicit_fixed_date_range_is_not_silently_expanded_to_history_goal(self):
        repo=MemoryAuditRepo([])
        runner=BackfillRunner(SimpleNamespace(),repo,FakeControl())
        with patch('app.services.day_candle_integrity_runner.DayEvidence.load',return_value=DayEvidence()), \
             patch('app.services.day_candle_integrity_runner.DayCalendar',side_effect=lambda:sessions()):
            result=DayIntegrityAudit(runner).run([dict(batch_v2=True,fixed_range=True)])
        self.assertEqual(result['summary']['checked_symbols'],0)
        self.assertIsNone(repo.saved)


if __name__=='__main__': unittest.main()
