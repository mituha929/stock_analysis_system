import json
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app.services.trading_suspensions import Halt, SuspensionCalendar, load_suspensions, parse_official, official_date


class SuspensionTests(unittest.TestCase):
    def test_roc_and_gregorian_dates(self):
        self.assertEqual(official_date("1151015"),date(2026,10,15))
        self.assertEqual(official_date("2026/10/15"),date(2026,10,15))
        with self.assertRaises(ValueError): official_date("115")

    def test_twse_full_day_but_not_resumption_day(self):
        rows=[dict(Code="2330",TradingHaltDate="1151013",TradingHaltTime="080000",
                   TradingResumptionDate="1151015",TradingResumptionTime="080000")]
        records=parse_official("twse",rows,date(2026,10,15))
        self.assertEqual((records[0].start,records[0].end),(date(2026,10,13),date(2026,10,14)))

    def test_partial_day_does_not_skip_that_days_candle(self):
        rows=[dict(Code="2330",TradingHaltDate="1151013",TradingHaltTime="100000",
                   TradingResumptionDate="1151013",TradingResumptionTime="110000")]
        self.assertEqual(parse_official("twse",rows,date(2026,10,15)),[])

    def test_tpex_separate_resume_rows_are_paired(self):
        common=dict(SecuritiesCompanyCode="1788")
        rows=[dict(common,DateOfSuspendedTrading="1150618",TimeOfSuspendedTrading="080000",DateOfResumedTrading="",TimeOfResumedTrading=""),
              dict(common,DateOfSuspendedTrading="",TimeOfSuspendedTrading="",DateOfResumedTrading="1150622",TimeOfResumedTrading="080000")]
        item=parse_official("tpex_history",rows,date(2026,10,15))[0]
        self.assertEqual(item.end,date(2026,6,21))
        self.assertEqual(item.resumed_on,date(2026,6,22))

    def test_unpaired_old_tpex_event_is_not_indefinite(self):
        row=dict(SecuritiesCompanyCode="1788",DateOfSuspendedTrading="1150618",TimeOfSuspendedTrading="080000",DateOfResumedTrading="",TimeOfResumedTrading="")
        self.assertEqual(parse_official("tpex_history",[row],date(2026,10,15))[0].end,date(2026,6,18))

    def test_only_explicit_suspension_not_disposal_or_margin(self):
        rows=[dict(Date="1151015",SecuritiesCompanyCode="2067",SuspensionOfTrading="Ｙ"),
              dict(Date="1151015",SecuritiesCompanyCode="3064",SuspensionOfTrading="",AlteredTrading="Ｙ")]
        self.assertEqual([r.symbol for r in parse_official("tpex_status",rows,date(2026,10,15))],["2067"])

    def test_missing_field_or_html_is_not_successful_empty_list(self):
        with self.assertRaises(KeyError): parse_official("tpex_status",[{}],date.today())
        with self.assertRaises(ValueError): parse_official("twse",{"error":"blocked"},date.today())

    def test_week_month_with_any_active_day_must_fetch(self):
        calendar=SuspensionCalendar([Halt(0,"2330",date(2026,10,13),date(2026,10,14),"official",date.today())])
        self.assertIsNone(calendar.full_window(0,"2330",date(2026,10,12),date(2026,10,15)))
        self.assertIsNone(calendar.full_window(0,"2330",date(2026,10,1),date(2026,10,15)))
        self.assertEqual(calendar.full_window(5,"2330",date(2026,10,13),date(2026,10,14)),"official")
        self.assertIsNone(calendar.full_window(6,"2330",date(2026,10,13),date(2026,10,14)))

    def test_stale_record_is_not_skip_evidence(self):
        calendar=SuspensionCalendar([Halt(0,"2330",date(2026,10,13),date(2026,10,14),"official",date(2026,1,1))])
        self.assertIsNone(calendar.full_window(0,"2330",date(2026,10,13),date(2026,10,14)))

    @patch.dict("os.environ",{"CAPITAL_SUSPENSION_SYNC_ENABLED":"true"})
    def test_one_daily_refresh_and_failure_preserves_old_cache_without_skips(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/"halts.json"
            fetch=Mock(return_value=[])
            calendar=load_suspensions(path=path,fetcher=fetch,today=date(2026,10,14))
            self.assertEqual(fetch.call_count,3)
            load_suspensions(path=path,fetcher=fetch,today=date(2026,10,14))
            self.assertEqual(fetch.call_count,3)
            failure=Mock(side_effect=OSError("offline"))
            calendar=load_suspensions(path=path,fetcher=failure,today=date(2026,10,15))
            self.assertEqual(calendar.status["state"],"partial")
            self.assertTrue(all(not r["fresh"] for r in calendar.status["sources"].values()))
            saved=json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["sources"]["twse"]["fetched_on"],"2026-10-14")
            load_suspensions(path=path,fetcher=failure,today=date(2026,10,15))
            self.assertEqual(failure.call_count,3)

    @patch.dict("os.environ",{"CAPITAL_SUSPENSION_SYNC_ENABLED":"true"})
    def test_preclose_snapshot_is_refreshed_after_close(self):
        today=date.today()
        def records(url):
            if "tpex_cmode" in url:
                return [dict(Date=today.strftime("%Y%m%d"),SecuritiesCompanyCode="2067",SuspensionOfTrading="Y")]
            return []
        with TemporaryDirectory() as folder,patch("app.services.trading_suspensions.datetime") as clock:
            path=Path(folder)/"halts.json"; fetch=Mock(side_effect=records)
            clock.now.return_value=datetime.combine(today,datetime.min.time()).replace(hour=10)
            first=load_suspensions(path=path,fetcher=fetch)
            self.assertEqual(first.records,{})
            clock.now.return_value=datetime.combine(today,datetime.min.time()).replace(hour=18)
            second=load_suspensions(path=path,fetcher=fetch)
            self.assertEqual(fetch.call_count,6)
            self.assertIn((1,"2067"),second.records)
