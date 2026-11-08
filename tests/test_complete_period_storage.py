import unittest
from datetime import date,datetime,timezone
from unittest.mock import Mock,patch

from app.services.backfill_policy import complete_aggregate
from app.services.mysql_instrument_repository import MySQLSettings,MySQLCandleRepository
from app.services.incremental_candle_repository import MySQLCandleRepository as IncrementalRepository


class CompletePeriodTests(unittest.TestCase):
    def test_current_month_and_early_written_old_month_are_excluded(self):
        now=date(2026,10,16)
        self.assertFalse(complete_aggregate("month",date(2026,10,1),datetime(2026,10,15),now))
        self.assertFalse(complete_aggregate("month",date(2026,9,1),datetime(2026,9,20),now))
        self.assertTrue(complete_aggregate("month",date(2026,9,1),datetime(2026,9,30,6),now))
        self.assertFalse(complete_aggregate("month",date(2026,9,1),None,now))

    def test_week_uses_last_trading_day_not_calendar_sunday(self):
        # 2026-10-09 holiday: 10-08 is this week's final trading day.
        self.assertFalse(complete_aggregate("week",date(2026,10,5),datetime(2026,10,8,5,59),date(2026,10,8)))
        self.assertTrue(complete_aggregate("week",date(2026,10,5),datetime(2026,10,8,6),date(2026,10,8)))
        self.assertFalse(complete_aggregate("week",date(2026,10,5),datetime(2026,10,8,6),date(2026,10,7)))

    def test_analysis_reads_keep_raw_rows_but_filter_partial_snapshots(self):
        repo=MySQLCandleRepository(MySQLSettings("unused",3306,"test","test","test"))
        connection=Mock(); cursor=connection.cursor.return_value
        def row(stamp,written): return (stamp,10,12,9,11,100,written)
        cursor.fetchall.return_value=[row(datetime(2026,10,1),datetime(2026,10,15)),
            row(datetime(2026,9,1),datetime(2026,10,1)),row(datetime(2026,8,3),datetime(2026,8,20))]
        with patch.object(repo,"_connector"),patch.object(repo,"_connect_database",return_value=connection),patch("app.services.backfill_policy.completed_end",return_value=date(2026,9,30)):
            candles,_=repo.get_candles_with_metadata("2330","month")
        self.assertEqual([c.timestamp.date() for c in candles],[date(2026,9,1)])
        self.assertTrue(all(call.args[0].lstrip().startswith("SELECT") for call in cursor.execute.call_args_list))

    def test_initialization_counts_only_complete_snapshots_in_bounded_chunks(self):
        cursor=Mock()
        cursor.fetchmany.side_effect=[[("2330",datetime(2026,9,1),datetime(2026,9,20)),
                                     ("2330",datetime(2026,8,3),datetime(2026,9,1))],[]]
        counts=IncrementalRepository.aggregate_extents(cursor,"candles","month",["2330"],date(2026,9,30))
        self.assertEqual(counts["2330"]["candle_count"],1)
        self.assertEqual(cursor.fetchmany.call_args.args,(256,))
        cursor.fetchall.assert_not_called()
