import contextlib
from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from app.services import stock_market_holidays as holidays
from scripts import manage_stock_market_holidays as cli


def payload(year=2026, rows=None):
    rows = rows if rows is not None else [
        [f"{year}-01-01", "中華民國開國紀念日", "依規定放假1日。"],
        [f"{year}-01-02", "國曆新年開始交易日", "國曆新年開始交易。"],
        [f"{year}-02-11", "農曆春節前最後交易日", "2月12日及13日市場無交易。"],
        [f"{year}-02-12", "市場無交易，僅辦理結算交割作業", ""],
    ]
    return dict(stat="ok", date=f"{year}0101", queryYear=year, title=f"{year - 1911} 年市場開休市日期",
                fields=list(holidays.ROW_FIELDS), total=len(rows), data=rows)


def snapshot(year=2026):
    annual = holidays.parse_annual(payload(year), year, holidays.API_BASES[0])
    return dict(version=1, years={str(year): annual})


def manual(**changes):
    row = dict(date="2026-10-23", name="颱風臨時休市", is_closed=True,
               reason="單元測試", source_url="https://www.twse.com.tw/", enabled=True)
    row.update(changes)
    return row


def supplements():
    return dict(version=1, annual_archives={}, closures=[])


def with_archive(year=2017):
    html = f"<table><tr><td>Taipei Exchange Holiday Schedule {year}</td></tr></table>"
    source = f"https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_{year-1911}.htm"
    archive = dict(source_url=source, reviewed_on="2026-10-24", covers_full_year=True,
                   reviewed_normalized_sha256=holidays.archive_digest(html), groups=[
                       dict(dates=[f"{year}-01-01"], name="元旦", description="Memorial Day", kind="closed"),
                   ])
    if year == 2017:
        archive["groups"].append(dict(dates=["2017-02-18", "2017-06-03", "2017-09-30"],
                                      name="補行交易", description="Additional Trading Days on Saturday", kind="special_open"))
    cached = holidays.parse_annual(payload(year, []), year, holidays.API_BASES[0])
    cached["archive"] = dict(source_url=source, html=html, normalized_sha256=holidays.archive_digest(html))
    return dict(version=1, years={str(year): cached}), dict(version=1, annual_archives={str(year): archive}, closures=[])


class AnnualHolidayParsingTests(unittest.TestCase):
    def test_empty_response_is_unavailable_not_no_holidays(self):
        row = holidays.parse_annual(payload(2016, []), 2016, holidays.API_BASES[0])
        self.assertEqual(row["status"], "unavailable")

    def test_first_last_trading_dates_not_holidays_even_description_mentions_closure(self):
        parsed = holidays.parse_annual(payload(), 2026, holidays.API_BASES[0])
        self.assertEqual([row["kind"] for row in parsed["records"]],
                         ["closed", "trading_marker", "trading_marker", "closed"])

    def test_same_date_settlement_overrides_misleading_last_trading_day_name(self):
        for year, month, days in ((2021, 2, (8, 9)), (2022, 1, (27, 28))):
            rows = [[f'{year}-{month:02d}-{d:02d}', '農曆春節前最後交易日',
                     f'{month}月{d}日市場無交易，僅辦理結算交割作業。'] for d in days]
            result = holidays.parse_annual(payload(year, rows), year, holidays.API_BASES[0])
            self.assertEqual([r['kind'] for r in result['records']], ['closed', 'closed'])

    def test_marker_mentioning_another_date_or_month_stays_open(self):
        for day, description in ((date(2023,1,17), '農曆春節前最後交易。1月18日及1月19日市場無交易。'),
                                 (date(2022,1,26), '1月27日市場無交易。'),
                                 (date(2022,1,27), '2月27日市場無交易。')):
            self.assertEqual(holidays.classify_row('農曆春節前最後交易日', description, day), 'trading_marker')

    def test_existing_snapshot_is_reparsed_without_downloading(self):
        stored = snapshot(2022)
        raw = payload(2022, [['2022-01-27', '農曆春節前最後交易日', '1月27日市場無交易，僅辦理結算交割作業。']])
        stored['years']['2022']['response'] = raw
        stored['years']['2022']['records'] = [dict(date='2022-01-27', kind='trading_marker')]
        with patch.object(holidays, 'urlopen', side_effect=AssertionError('no network')):
            result = holidays.build_calendar(stored, supplements(), {'records': []}, date(2022,12,31), 1)
        self.assertEqual(holidays.calendar_status(result, date(2022,1,27))[0], 'closed')

    def test_schema_wrong_year_total_and_malformed_rows_stop_update(self):
        cases = [dict(payload(), fields=["名稱", "日期", "說明"]),
                 dict(payload(), queryYear=2025), dict(payload(), date="20250101"),
                 dict(payload(), title="114 年市場開休市日期"), dict(payload(), total=999),
                 dict(payload(), stat="查無資料")]
        for row in (["2025-01-01", "元旦", "放假"], ["2026-02-30", "無效日期", "放假"],
                    ["2026-01-01", "只有兩欄"], ["2026-01-01", "", "放假"]):
            cases.append(payload(rows=[row]))
        for item in cases:
            with self.subTest(item=item):
                with self.assertRaises(holidays.StockHolidayError):
                    holidays.parse_annual(item, 2026, holidays.API_BASES[0])

    def test_nonempty_response_with_only_trading_markers_is_invalid(self):
        with self.assertRaises(holidays.StockHolidayError):
            holidays.parse_annual(payload(rows=[["2026-01-02", "國曆新年開始交易日", "開始交易"]]), 2026, "https://www.twse.com.tw/")

    def test_unknown_wording_retained_for_review(self):
        parsed = holidays.parse_annual(payload(rows=[*payload()["data"], ["2026-03-02", "另行公告", "待確認"]]), 2026, "https://www.twse.com.tw/")
        self.assertEqual(parsed["records"][-1]["kind"], "review")

    def test_fetch_checks_year_parameter_hash_and_bounded_download(self):
        response = MagicMock()
        response.read.return_value = json.dumps(payload(), ensure_ascii=False).encode("utf-8")
        with patch.object(holidays, "urlopen") as opener:
            opener.return_value.__enter__.return_value = response
            parsed = holidays.fetch_year(2026)
            self.assertIn("date=20260101", opener.call_args.args[0].full_url)
            self.assertEqual(len(parsed["source_sha256"]), 64)
            response.read.assert_called_once_with(holidays.LIMIT_BYTES + 1)

    def test_both_empty_official_hosts_remain_unavailable(self):
        response = MagicMock()
        response.read.return_value = json.dumps(payload(2017, [])).encode()
        with patch.object(holidays, "urlopen") as opener:
            opener.return_value.__enter__.return_value = response
            result = holidays.fetch_year(2017)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(result["attempts"]), 2)

    def test_primary_network_failure_uses_second_official_host(self):
        response = MagicMock()
        response.read.return_value = json.dumps(payload()).encode()
        context = MagicMock()
        context.__enter__.return_value = response
        with patch.object(holidays, "urlopen", side_effect=[OSError("offline"), context]):
            result = holidays.fetch_year(2026)
        self.assertIn("www.twse.com.tw", result["source_url"])

    def test_all_download_failures_raise(self):
        with patch.object(holidays, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(holidays.StockHolidayError):
                holidays.fetch_year(2026)


class StockHolidayCalendarTests(unittest.TestCase):
    def test_scope_is_shared_by_all_three_stock_markets(self):
        result = holidays.build_calendar(snapshot(), supplements(), {"records": []}, date(2026, 10, 24), 1)
        self.assertEqual(result["markets"], ["TWSE", "TPEx", "ESB"])
        self.assertTrue(all(row["markets"] == result["markets"] for row in result["records"]))
        self.assertFalse(result["runtime_calendar_applied"])

    def test_rolling_window_and_future_annual_dates_are_excluded(self):
        stored = snapshot()
        stored["years"]["2026"] = holidays.parse_annual(payload(rows=[*payload()["data"],
            ["2026-12-25", "行憲紀念日", "放假1日"]]), 2026, holidays.API_BASES[0])
        result = holidays.build_calendar(stored, supplements(), {"records": []}, date(2026, 10, 24))
        self.assertEqual(result["range_start"], "2016-10-24")
        self.assertEqual(result["range_end"], "2026-10-24")
        self.assertNotIn("2026-12-25", result["closed_dates"])
        self.assertEqual(holidays.first_day(date(2024, 2, 29)), date(2014, 2, 28))

    def test_settlement_only_is_closed_but_last_trading_day_is_open(self):
        result = holidays.build_calendar(snapshot(), supplements(), {"records": []}, date(2026, 10, 24), 1)
        self.assertEqual(holidays.calendar_status(result, date(2026, 2, 11))[0], "open")
        self.assertEqual(holidays.calendar_status(result, date(2026, 2, 12))[0], "closed")

    def test_missing_year_weekday_is_unknown_not_open(self):
        result = holidays.build_calendar(snapshot(), supplements(), {"records": []}, date(2026, 10, 24))
        self.assertIn(2017, result["missing_years"])
        self.assertEqual(holidays.calendar_status(result, date(2017, 3, 1))[0], "unknown")

    def test_before_2019_uncovered_saturdays_are_not_assumed_closed(self):
        result = holidays.build_calendar(snapshot(), supplements(), {"records": []}, date(2026, 10, 24))
        self.assertEqual(holidays.calendar_status(result, date(2017, 2, 18))[0], "unknown")
        self.assertNotIn("2017-02-18", result["closed_dates"])
        self.assertEqual(holidays.calendar_status(result, date(2017, 2, 19))[0], "closed")

    def test_reviewed_archive_keeps_saturday_trading_exceptions(self):
        stored, extra = with_archive()
        result = holidays.build_calendar(stored, extra, {"records": []}, date(2026, 10, 24))
        for key in ("2017-02-18", "2017-06-03", "2017-09-30"):
            self.assertEqual(holidays.calendar_status(result, date.fromisoformat(key))[0], "open")
            self.assertNotIn(key, result["closed_dates"])
        self.assertNotIn(2017, result["missing_years"])

    def test_unfetched_archive_does_not_claim_coverage(self):
        stored, extra = with_archive()
        stored["years"]["2017"].pop("archive")
        result = holidays.build_calendar(stored, extra, {"records": []}, date(2026, 10, 24))
        self.assertIn(2017, result["missing_years"])

    def test_manual_open_overrides_weekend_and_disabled_rule_restores_base(self):
        for enabled in (True, False):
            result = holidays.build_calendar(snapshot(), supplements(), {"records": [manual(date="2026-10-24", is_closed=False, enabled=enabled)]}, date(2026, 10, 24), 1)
            self.assertEqual(holidays.calendar_status(result, date(2026, 10, 24))[0], "open" if enabled else "closed")

    def test_manual_closed_overrides_official_trading_marker(self):
        result = holidays.build_calendar(snapshot(), supplements(), {"records": [manual(date="2026-01-02")]}, date(2026, 10, 24), 1)
        self.assertEqual(holidays.calendar_status(result, date(2026, 1, 2))[0], "closed")

    def test_emergency_requires_separate_evidence_and_manual_wins(self):
        extra = dict(supplements(), closures=[manual()])
        result = holidays.build_calendar(snapshot(), extra, {"records": []}, date(2026, 10, 24), 1)
        self.assertIn("2026-10-23", result["emergency_dates"])
        changed = holidays.build_calendar(snapshot(), extra, {"records": [manual(is_closed=False)]}, date(2026, 10, 24), 1)
        self.assertNotIn("2026-10-23", changed["closed_dates"])

    def test_media_and_exchange_origins_are_distinguished(self):
        self.assertEqual(holidays.notice_origin("https://www.cna.com.tw/news/test"), "reported_exchange_notice")
        self.assertEqual(holidays.notice_origin("https://eshop.twse.com.tw/zh/"), "reviewed_exchange_notice")
        self.assertEqual(holidays.notice_origin("https://faketwse.com.tw/"), "reported_exchange_notice")

    def test_weekend_holiday_count_deduplicates_dates(self):
        stored = snapshot()
        stored["years"]["2026"] = holidays.parse_annual(payload(rows=[*payload()["data"], ["2026-10-24", "調整放假", "放假"]]), 2026, holidays.API_BASES[0])
        result = holidays.build_calendar(stored, supplements(), {"records": []}, date(2026, 10, 24), 1)
        self.assertEqual(result["closed_dates"].count("2026-10-24"), 1)
        self.assertEqual(result["weekday_closed_count"] + result["weekend_closed_count"], result["closed_date_count"])

    def test_unlisted_covered_weekday_not_claimed_open_due_to_possible_emergencies(self):
        result = holidays.build_calendar(snapshot(), supplements(), {"records": []}, date(2026, 10, 24), 1)
        self.assertEqual(holidays.calendar_status(result, date(2026, 3, 2))[0], "not_listed")
        self.assertFalse(result["emergency_coverage_complete"])

    def test_invalid_duplicate_manual_dates_and_url_rejected(self):
        for row in (manual(date="2026-02-30"), manual(enabled="false"), manual(is_closed=1),
                    manual(reason=""), manual(source_url="not-a-url"), dict(manual(), markets=["bond"])):
            with self.assertRaises(holidays.StockHolidayError):
                holidays.validate_manual({"records": [row]})
        with self.assertRaises(holidays.StockHolidayError):
            holidays.validate_manual({"records": [manual(), manual()]})

    def test_unknown_annual_row_blocks_classification_for_that_day(self):
        stored = snapshot()
        stored["years"]["2026"] = holidays.parse_annual(payload(rows=[*payload()["data"], ["2026-03-02", "未知", "待確認"]]), 2026, holidays.API_BASES[0])
        result = holidays.build_calendar(stored, supplements(), {"records": []}, date(2026, 10, 24), 1)
        self.assertEqual(holidays.calendar_status(result, date(2026, 3, 2))[0], "unknown")

    def test_conflicting_open_closed_official_rows_stop_build(self):
        stored = snapshot()
        stored["years"]["2026"] = holidays.parse_annual(payload(rows=[*payload()["data"], ["2026-01-02", "元旦補假", "放假"]]), 2026, holidays.API_BASES[0])
        with self.assertRaises(holidays.StockHolidayError):
            holidays.build_calendar(stored, supplements(), {"records": []}, date(2026, 10, 24), 1)


class StockHolidayPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.paths = dict(snapshot_path=root / "official.json", supplements_path=root / "supplements.json",
                          manual_path=root / "manual.json", merged_path=root / "merged.json", report_path=root / "report.md")
        holidays.save_json(self.paths["manual_path"], dict(version=1, records=[manual()]))
        holidays.save_json(self.paths["supplements_path"], supplements())

    def test_refresh_uses_inclusive_annual_range_and_preserves_manual(self):
        before = self.paths["manual_path"].read_bytes()
        def fetch(year):
            return snapshot(year)["years"][str(year)]
        with patch.object(holidays, "fetch_year", side_effect=fetch) as mocked:
            first = holidays.refresh(date(2026, 10, 24), years=1, pause_seconds=0, **self.paths)
            second = holidays.refresh(date(2026, 10, 24), years=1, pause_seconds=0, **self.paths)
        self.assertEqual([call.args[0] for call in mocked.call_args_list], [2025, 2026, 2025, 2026])
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(before, self.paths["manual_path"].read_bytes())

    def test_failed_year_does_not_replace_any_output(self):
        for key in ("snapshot_path", "merged_path", "report_path"):
            self.paths[key].write_text("previous-output", encoding="utf-8")
        holidays.save_json(self.paths["snapshot_path"], snapshot())
        before = {key: path.read_bytes() for key, path in self.paths.items()}
        with patch.object(holidays, "fetch_year", side_effect=holidays.StockHolidayError("network error")):
            with self.assertRaises(holidays.StockHolidayError):
                holidays.refresh(date(2026, 10, 24), years=1, pause_seconds=0, **self.paths)
        self.assertEqual(before, {key: path.read_bytes() for key, path in self.paths.items()})

    def test_formerly_available_year_cannot_be_replaced_with_empty(self):
        holidays.save_json(self.paths["snapshot_path"], snapshot(2025))
        before = self.paths["snapshot_path"].read_bytes()
        with patch.object(holidays, "fetch_year", return_value=holidays.parse_annual(payload(2025, []), 2025, holidays.API_BASES[0])):
            with self.assertRaises(holidays.StockHolidayError):
                holidays.refresh(date(2026, 10, 24), years=1, pause_seconds=0, **self.paths)
        self.assertEqual(before, self.paths["snapshot_path"].read_bytes())

    def test_archive_fetch_rejects_changed_source(self):
        stored, extra = with_archive()
        archive = extra["annual_archives"]["2017"]
        response = MagicMock()
        with patch.object(holidays, "urlopen") as opener:
            opener.return_value.__enter__.return_value = response
            response.read.return_value = stored["years"]["2017"]["archive"]["html"].encode()
            self.assertIn("html", holidays.fetch_archive(2017, archive))
            response.read.return_value = b"<html>blocked</html>"
            with self.assertRaises(holidays.StockHolidayError):
                holidays.fetch_archive(2017, archive)

    def test_build_and_list_are_offline_and_add_is_idempotent(self):
        holidays.save_json(self.paths["snapshot_path"], snapshot())
        with contextlib.ExitStack() as stack:
            for name, path in zip(("SNAPSHOT_PATH", "SUPPLEMENTS_PATH", "MANUAL_PATH", "MERGED_PATH", "REPORT_PATH"), self.paths.values()):
                stack.enter_context(patch.object(cli, name, path))
            stack.enter_context(patch.object(cli, "today", return_value=date(2026, 10, 24)))
            stack.enter_context(patch.object(holidays, "urlopen", side_effect=AssertionError("offline")))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            for _ in range(2):
                self.assertEqual(cli.main(["add", "2026-10-23", "--name", "補登", "--reason", "測試", "--as-of", "2026-10-24", "--years", "1"]), 0)
            saved = holidays.load_manual(self.paths["manual_path"])
            self.assertEqual(len(saved["records"]), 1)
            self.assertEqual(saved["records"][0]["source_url"], "https://www.twse.com.tw/")
            before = {key: path.read_bytes() for key, path in self.paths.items()}
            self.assertEqual(cli.main(["list", "--as-of", "2026-10-24", "--years", "1"]), 0)
            self.assertEqual(before, {key: path.read_bytes() for key, path in self.paths.items()})


if __name__ == "__main__":
    unittest.main()
