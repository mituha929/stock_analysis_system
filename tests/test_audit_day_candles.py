"""Assessment-only tooling must not inherit the worker's queue mutations."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from audit_day_candles import date_ranges, load_policies, require_ledger, ReadSnapshotRepository


class AuditOnlyTests(unittest.TestCase):
    def test_date_ranges_sorted_unique_and_only_adjacent_dates_coalesced(self):
        self.assertEqual(date_ranges([date(2026,10,22),date(2026,10,20),date(2026,10,20),date(2026,10,19)]),
                         '2026-10-19～2026-10-20, 2026-10-22')
        self.assertEqual(date_ranges([]),'')

    def test_read_snapshot_context_never_commits_between_symbols(self):
        reader=ReadSnapshotRepository.__new__(ReadSnapshotRepository); reader.connection=Mock()
        q=reader.connection.cursor.return_value
        with reader.transaction(dictionary=True) as cursor:
            cursor.execute('SELECT 1')
        q.close.assert_called_once(); reader.connection.commit.assert_not_called()
        reader.close(); reader.connection.rollback.assert_called_once(); reader.connection.close.assert_called_once()

    def reader(self,batches):
        reader=Mock(); q=Mock(); q.fetchone.return_value={'n':1}; q.fetchall.side_effect=batches
        @contextmanager
        def transaction(**kwargs): yield q
        reader.transaction=transaction; reader._load_stock_listing_policies.return_value={'1234':{'history_start':date(2020,1,1)}}
        return reader,q

    def test_policy_reader_never_reconciles_queue_or_refreshes_metadata_db(self):
        cached=dict(symbol='17272',name='中華化二',listing_date=date(2026,10,20),maturity_date=date(2031,10,20),
            termination_date=date(2026,10,22),trading_status='terminated',source_url='https://www.tpex.org.tw/official',listing_source_url=None)
        reader,q=self.reader([[cached],[{'symbol':'17272','name':'中華化二'}]])
        fresh=dict(cached,listing_date=None,maturity_date=None,termination_date=None,trading_status='unknown')
        policies=load_policies(reader,'candles',1,[fresh])
        self.assertEqual(policies['17272']['history_start'],date(2026,10,20))
        self.assertEqual(policies['17272']['termination_date'],date(2026,10,22))
        reader.load_listing_policies.assert_not_called(); reader.refresh_lifecycle.assert_not_called()
        reader.reconcile_lifecycle_reviews.assert_not_called()
        self.assertTrue(all(c.args[0].startswith('SELECT') for c in q.execute.call_args_list))

    def test_bond_identity_mismatch_is_not_guessed(self):
        reader,q=self.reader([[],[{'symbol':'17272','name':'不同商品'}]])
        fresh=dict(symbol='17272',name='中華化二',listing_date=date(2026,10,20),maturity_date=None,
            termination_date=None,trading_status='unknown',source_url='https://www.tpex.org.tw/official')
        self.assertNotIn('17272',load_policies(reader,'candles',1,[fresh]))

    def test_writes_refused_if_ledger_not_innodb_or_has_extra_triggers(self):
        for rows in ([],[('candle_day_integrity','MyISAM'),('candle_day_gaps','InnoDB')]):
            writer,q=self.reader([rows])
            with self.assertRaises(RuntimeError): require_ledger(writer)
        writer,q=self.reader([[('candle_day_integrity','InnoDB'),('candle_day_gaps','InnoDB')],[('unexpected_trigger',)]])
        with self.assertRaises(RuntimeError): require_ledger(writer)
        self.assertTrue(all(c.args[0].startswith('SELECT') for c in q.execute.call_args_list))
