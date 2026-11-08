"""Permanent queue exclusion is independent of vendor listing/period rules."""
import json
import unittest
from unittest.mock import Mock, patch
from datetime import date

from app.services.backfill_eligibility import excluded_backfill_symbols, backfill_symbol_predicate
from app.services.capital_history_connection import CapitalHistoryError
from app.services.incremental_candle_repository import MySQLCandleRepository


class BackfillSymbolPolicyTests(unittest.TestCase):
    def test_default_is_exact_tslq_not_the_users_typo_or_a_category(self):
        self.assertEqual(excluded_backfill_symbols(),frozenset({'TSLQ'}))
        self.assertEqual(backfill_symbol_predicate('i'),"i.symbol NOT IN ('TSLQ')")
        self.assertEqual(backfill_symbol_predicate(),"symbol NOT IN ('TSLQ')")

    def test_invalid_config_cannot_silently_enable_all_symbols_or_inject_sql(self):
        samples=['{}','bad',json.dumps(['tslq']),json.dumps(['TSLQ','TSLQ']),json.dumps(['*']),
                 json.dumps(["TSLQ'); DELETE FROM candles; --"]),json.dumps([None]),json.dumps([{}])]
        try:
            for raw in samples:
                with self.subTest(raw=raw),patch('app.services.backfill_eligibility.EXCLUDED_SYMBOLS_PATH') as path:
                    excluded_backfill_symbols.cache_clear(); path.read_text.return_value=raw
                    with self.assertRaises(CapitalHistoryError): backfill_symbol_predicate('i')
        finally:
            excluded_backfill_symbols.cache_clear()

    def test_empty_list_is_explicit_reenable_and_invalid_sql_alias_is_rejected(self):
        with patch('app.services.backfill_eligibility.excluded_backfill_symbols',return_value=frozenset()):
            self.assertEqual(backfill_symbol_predicate('i'),'1=1')
        with self.assertRaises(ValueError): backfill_symbol_predicate('i; DROP TABLE candles')


class QueueQueryTests(unittest.TestCase):
    def repo(self):
        repo=MySQLCandleRepository(); connection=Mock(); cursor=connection.cursor.return_value
        cursor.fetchall.return_value=[]; cursor.fetchone.return_value=None
        for guard in (patch.object(repo,'_connect_database',return_value=connection),
                      patch.object(repo,'load_listing_policies',return_value={}),
                      patch.object(repo,'ensure_incremental_storage',return_value=('candles','candle_backfill_jobs')),
                      patch.object(repo,'ensure_batch_storage',return_value=('candles','candle_backfill_jobs'))):
            guard.start(); self.addCleanup(guard.stop)
        return repo,cursor

    def scope(self):
        return dict(market_no=0,interval='day',start_date=date(2026,10,1),end_date=date(2026,10,16),
                    candle_table='candles',job_table='candle_backfill_jobs',instrument_table='instruments')

    def test_incremental_and_force_only_symbol_still_have_sql_exclusion(self):
        repo,cursor=self.repo()
        result=repo.plan_incremental(0,'day',date(2026,10,16),1200,'candles','candle_backfill_jobs','instruments',
                                     force=True,only_symbols=['TSLQ'])
        self.assertEqual(result['queued_jobs'],0)
        first=cursor.execute.call_args_list[0]
        self.assertIn("i.symbol NOT IN ('TSLQ')",first.args[0])
        self.assertEqual(first.args[1][-1],'TSLQ')
        cursor.executemany.assert_not_called()

    def test_batch_and_daily_derived_planners_use_same_exclusion(self):
        for options in ({},{'refresh_bars':10},{'refresh_bars':5,'aggregate_from_day':True}):
            repo,cursor=self.repo()
            interval='week' if options.get('aggregate_from_day') else 'day'
            result=repo.plan_batch(0,interval,date(2026,10,16),1200,'candles','candle_backfill_jobs','instruments',
                                   force=True,only_symbols=['TSLQ'],**options)
            self.assertEqual(result['queued_jobs'],0)
            self.assertIn("i.symbol NOT IN ('TSLQ')",cursor.execute.call_args_list[0].args[0])
            cursor.executemany.assert_not_called()

    def test_existing_jobs_and_failed_retries_cannot_bypass_policy(self):
        repo,cursor=self.repo()
        self.assertIsNone(repo.claim_incremental_job(dict(self.scope(),only_symbols=['TSLQ']),'test'))
        self.assertIn("i.symbol NOT IN ('TSLQ')",cursor.execute.call_args.args[0])
        self.assertEqual(repo.failed_symbols(0,'week','candle_backfill_jobs','instruments'),[])
        self.assertIn("i.symbol NOT IN ('TSLQ')",cursor.execute.call_args.args[0])
        repo.recover_incremental_jobs(self.scope())
        self.assertIn("j.symbol NOT IN ('TSLQ')",cursor.execute.call_args.args[0])

    def test_pending_work_count_ignores_previously_queued_tslq(self):
        repo,cursor=self.repo(); cursor.fetchone.return_value=(0,None)
        result=repo.batch_pending([dict(self.scope(),batch_v2=True,planned_on='2026-10-20')])
        self.assertEqual(result,(0,None))
        self.assertIn("i.symbol NOT IN ('TSLQ')",cursor.execute.call_args.args[0])

    def test_legacy_claim_and_reset_cannot_bypass_policy(self):
        repo,cursor=self.repo(); scope=self.scope(); cursor.rowcount=0
        self.assertIsNone(repo.claim_backfill_job('day',scope['start_date'],scope['end_date'],include_failed=True))
        self.assertIn("symbol NOT IN ('TSLQ')",cursor.execute.call_args.args[0])
        self.assertEqual(repo.reset_running_backfill_jobs('day',scope['start_date'],scope['end_date']),0)
        self.assertIn("symbol NOT IN ('TSLQ')",cursor.execute.call_args.args[0])


if __name__=='__main__':
    unittest.main()
