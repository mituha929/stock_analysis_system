import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from app.services.stock_master_sources import (
    SOURCES, TAIPEI, TWSE_FIELDS, TPEX_FIELDS, StockRecord, StockSnapshot, StockSourceError,
    fetch_snapshots, parse_official_date, parse_snapshot, read_official, validate_code, validate_snapshots,
)
from app.services.stock_master_repository import (
    AmbiguousStockProfile, StockMasterRepository, StockSyncError, json_value,
)
from app.services.stock_master_scheduler import StockMasterSchedule, WeeklyStockMasterScheduler
from app.services.mysql_instrument_repository import MySQLSettings, MySQLRepositoryError

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
TODAY = NOW.astimezone(TAIPEI).date()


def source_row(market="TWSE", code="2330", listed="19940905", **changes):
    fields = TWSE_FIELDS if market == "TWSE" else TPEX_FIELDS
    row = dict.fromkeys(fields, "")
    if market == "TWSE":
        row.update({"出表日期": "1151022", "公司代號": code, "公司簡稱": "台積電", "上市日期": listed,
                    "成立日期": "19870221", "產業別": "24", "營利事業統一編號": "22099131"})
    else:
        row.update({"Date": "1151022", "SecuritiesCompanyCode": code, "CompanyAbbreviation": "環球晶",
                    "DateOfListing": listed, "DateOfIncorporation": "20111018", "SecuritiesIndustryCode": "24",
                    "UnifiedBusinessNo.": "28113286"})
    row.update(changes)
    return row


def parsed(rows, market="TWSE", **kwargs):
    return parse_snapshot(json.dumps(rows, ensure_ascii=False), market, TODAY, minimum=1, **kwargs)


def snapshots():
    return [parsed([source_row()]), parsed([source_row("TPEx", "6488", "20150925")], "TPEx")]


class StockSourceTests(unittest.TestCase):
    def test_roc_and_gregorian_dates(self):
        for text, expected in (("19940905", date(1994, 9, 5)), ("0830905", date(1994, 9, 5)),
                               ("83/9/5", date(1994, 9, 5)), ("115-10-22", TODAY),
                               ("2026-10-22", TODAY), ("2026/10/22", TODAY)):
            self.assertEqual(parse_official_date(text), expected)

    def test_missing_date_optional_only(self):
        for text in (None, "", " ", "--", "－"):
            self.assertIsNone(parse_official_date(text, optional=True))
            with self.assertRaises(StockSourceError):
                parse_official_date(text)

    def test_invalid_dates_not_repaired(self):
        for text in (19940905, True, "20260230", "1151332", "00000000", "2026-99-01", "2026-10", "昨天"):
            with self.subTest(text=text), self.assertRaises(StockSourceError):
                parse_official_date(text, optional=True)

    def test_code_is_string_and_retains_zero(self):
        self.assertEqual(validate_code("0050"), "0050")
        for value in (50, 2330, "233", "1234567", " 2330", "TSEA", "2330' OR 1=1"):
            with self.assertRaises(StockSourceError):
                validate_code(value)

    def test_twse_listing_not_incorporation(self):
        record = parsed([source_row()]).records[0]
        self.assertEqual(record.listing_date, date(1994, 9, 5))
        self.assertEqual((record.name, record.industry), ("台積電", "半導體業"))

    def test_tpex_date_field_is_otc_not_listing_on_twse(self):
        record = snapshots()[1].records[0]
        self.assertEqual((record.market, record.listing_date), ("TPEx", date(2015, 9, 25)))

    def test_schema_added_removed_or_renamed_fails_entire_batch(self):
        for row in (source_row(extra="new"), {k: v for k, v in source_row().items() if k != "上市日期"},
                    {**{k: v for k, v in source_row().items() if k != "上市日期"}, "掛牌日期": "19940905"}):
            with self.assertRaisesRegex(StockSourceError, "欄位已變更"):
                parsed([source_row(code="1101"), row])

    def test_null_date_remains_unknown(self):
        snapshot = parsed([source_row(), source_row(code="1234", listed="")])
        self.assertIsNone(snapshot.records[1].listing_date)
        self.assertTrue(snapshot.warnings)

    def test_all_dates_missing_is_anomalous_not_empty_overwrite(self):
        with self.assertRaises(StockSourceError):
            parsed([source_row(listed="")])

    def test_future_date_preserved_for_separate_plans(self):
        snapshot = parsed([source_row(), source_row(code="1234", listed="20261101")])
        self.assertEqual(snapshot.records[1].listing_date, date(2026, 11, 1))

    def test_duplicate_codes_and_bad_rows_reject_batch(self):
        for rows in ([source_row(), source_row()], [None], [], {}, [{"error": "unavailable"}]):
            with self.assertRaises(StockSourceError):
                parsed(rows)

    def test_duplicate_json_keys_rejected(self):
        payload = json.dumps([source_row()]).replace('"2330"', '"2330", "公司代號":"1101"')
        with self.assertRaises(StockSourceError):
            parse_snapshot(payload, "TWSE", TODAY, minimum=1)

    def test_report_dates_old_future_or_inconsistent_rejected(self):
        for rows in ([source_row(**{"出表日期": "1151023"})], [source_row(**{"出表日期": "1151001"})],
                     [source_row(), source_row(code="1234", **{"出表日期": "1151021"})]):
            with self.assertRaises(StockSourceError):
                parsed(rows)

    def test_industry_unknown_stored_null_with_raw_code(self):
        record = parsed([source_row(**{"產業別": "99"})]).records[0]
        self.assertIsNone(record.industry)
        self.assertEqual(record.industry_code, "99")

    def test_finance_mapping_is_market_specific(self):
        listed = parsed([source_row(**{"產業別": "17"})]).records[0]
        otc = parsed([source_row("TPEx", "6488", **{"SecuritiesIndustryCode": "17"})], "TPEx").records[0]
        self.assertEqual((listed.industry, otc.industry), ("金融保險", "金融業"))

    def test_tdr_six_digit_code_not_dropped(self):
        record = parsed([source_row(code="910322", **{"產業別": "91"})]).records[0]
        self.assertEqual(record.code, "910322")
        self.assertIsNone(record.industry)

    def test_both_markets_required_and_duplicate_current_market_rejected(self):
        items = snapshots()
        with self.assertRaises(StockSourceError):
            validate_snapshots(items[:1], TODAY)
        duplicate = replace(items[1], records=(replace(items[0].records[0], market="TPEx"),))
        with self.assertRaises(StockSourceError):
            validate_snapshots([items[0], duplicate], TODAY)

    def test_future_transfer_can_coexist_with_current_other_market(self):
        items = snapshots()
        planned = replace(items[0], records=(replace(items[1].records[0], market="TWSE", listing_date=date(2026, 11, 1)),))
        validate_snapshots([planned, items[1]], TODAY)

    def test_public_fetch_is_source_driven_and_whole_batch(self):
        reader = MagicMock(side_effect=[json.dumps([source_row()]), json.dumps([source_row("TPEx", "6488", "20150925")])])
        items = fetch_snapshots(reader=reader, now=NOW, minimum=1, include_etfs=False)
        self.assertEqual(len(items), 2)
        self.assertEqual([call.args[0] for call in reader.call_args_list], list(SOURCES.values()))
        with self.assertRaises(StockSourceError):
            fetch_snapshots(reader=lambda url: "[]", now=NOW)

    def test_http_block_or_redirect_not_bypassed(self):
        from urllib.error import HTTPError
        with patch("app.services.stock_master_sources.urlopen", side_effect=HTTPError(SOURCES["TWSE"],403,"Forbidden",{},None)) as opened:
            with self.assertRaisesRegex(StockSourceError, "HTTP 403"):
                read_official(SOURCES["TWSE"])
            opened.assert_called_once()
        response = MagicMock()
        response.geturl.return_value = "https://www.tpex.org.tw/errors"
        with patch("app.services.stock_master_sources.urlopen") as opened:
            opened.return_value.__enter__.return_value = response
            with self.assertRaises(StockSourceError):
                read_official(SOURCES["TPEx"])


class StockScheduleTests(unittest.TestCase):
    def test_disabled_default_does_not_connect_or_start_thread(self):
        factory = MagicMock()
        scheduler = WeeklyStockMasterScheduler(StockMasterSchedule(), factory)
        scheduler.start()
        self.assertEqual(scheduler.tick(NOW)["status"], "disabled")
        self.assertIsNone(scheduler.thread)
        factory.assert_not_called()

    def test_taipei_weekly_due_and_missed_catchup(self):
        config = StockMasterSchedule(True)
        self.assertEqual(config.due_key(NOW), "weekly:2026-10-18")
        before = datetime(2026,10,25,11,59,tzinfo=timezone.utc)
        self.assertEqual(config.due_key(before), "weekly:2026-10-18")
        self.assertEqual(config.due_key(before + timedelta(minutes=1)), "weekly:2026-10-25")

    def test_success_cached_no_extra_database_poll(self):
        repo = MagicMock()
        repo.synchronize.return_value = {"status": "completed"}
        factory = MagicMock(return_value=repo)
        scheduler = WeeklyStockMasterScheduler(StockMasterSchedule(True), factory)
        scheduler.tick(NOW)
        scheduler.tick(NOW + timedelta(seconds=60))
        factory.assert_called_once()
        scheduler.tick(NOW + timedelta(days=7))
        self.assertEqual(factory.call_count, 2)

    def test_failure_rate_limited(self):
        repo = MagicMock()
        repo.synchronize.side_effect = RuntimeError("offline")
        scheduler = WeeklyStockMasterScheduler(StockMasterSchedule(True), lambda: repo)
        self.assertEqual(scheduler.tick(NOW)["status"], "failed")
        self.assertEqual(scheduler.tick(NOW + timedelta(minutes=1))["status"], "retry_wait")
        scheduler.tick(NOW + timedelta(hours=1))
        self.assertEqual(repo.synchronize.call_count, 2)

    def test_config_validation(self):
        for changes in ({"CAPITAL_STOCK_MASTER_WEEKDAY":"7"}, {"CAPITAL_STOCK_MASTER_TIME":"25:00"},
                        {"CAPITAL_STOCK_MASTER_RETRY_SECONDS":"1"}, {"CAPITAL_STOCK_MASTER_WEEKLY_ENABLED":"maybe"}):
            with patch.dict("os.environ", changes), self.assertRaises(ValueError):
                StockMasterSchedule.from_env()


class StockRepositoryUnitTests(unittest.TestCase):
    def repo(self):
        repo = StockMasterRepository(MySQLSettings("fake",3306,"fake","unit-only","unit"))
        con, cur = MagicMock(), MagicMock()
        con.cursor.return_value = cur
        repo._connector = MagicMock()
        repo._connect_database = MagicMock(return_value=con)
        return repo, con, cur

    def test_busy_lock_never_fetches_or_mutates_tables(self):
        repo, con, cur = self.repo()
        cur.fetchone.return_value = (0,)
        fetch = MagicMock()
        result = repo.synchronize(fetcher=fetch, now=NOW)
        self.assertEqual(result["status"], "busy")
        fetch.assert_not_called()
        self.assertFalse(any("INSERT" in call.args[0] for call in cur.execute.call_args_list))

    def test_fetch_failure_persisted_without_profile_transaction(self):
        repo, con, cur = self.repo()
        cur.fetchone.side_effect = [(1,), (1,)]
        with patch("app.services.stock_master_repository._ensure_storage"):
            with self.assertRaises(StockSyncError) as ctx:
                repo.synchronize(fetcher=MagicMock(side_effect=StockSourceError("TWSE schema changed")), now=NOW)
        self.assertFalse(ctx.exception.commit_uncertain)
        self.assertRegex(ctx.exception.run_id, r"^[0-9a-f]{32}$")
        self.assertTrue(any("status='failed'" in call.args[0] for call in cur.execute.call_args_list))
        con.start_transaction.assert_not_called()

    def test_success_is_one_profile_transaction_with_final_log(self):
        repo, con, cur = self.repo()
        cur.fetchone.side_effect = [(1,), None, (1,)]
        cur.rowcount = 1
        with patch("app.services.stock_master_repository._ensure_storage"), patch("app.services.stock_master_repository.apply_snapshots", return_value={"fetched_count":2}) as apply:
            result = repo.synchronize(fetcher=snapshots, now=NOW)
        self.assertEqual(result["status"], "completed")
        con.start_transaction.assert_called_once()
        apply.assert_called_once()

    def test_database_error_does_not_leak_connection_details(self):
        repo, con, cur = self.repo()
        repo._connect_database.side_effect = RuntimeError("secret-password")
        with self.assertRaises(StockSyncError) as ctx:
            repo.synchronize()
        self.assertNotIn("secret-password", str(ctx.exception))

    def test_utc_storage_serialized_as_taipei(self):
        self.assertEqual(json_value(datetime(2026,10,22,12)), "2026-10-22T20:00:00+08:00")


async def api_get(path):
    from app.main import app
    parts = urlsplit(path)
    output = []
    async def receive():
        return {"type":"http.request","body":b"","more_body":False}
    async def send(message):
        output.append(message)
    await app({"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":"GET","scheme":"http",
               "path":parts.path,"raw_path":parts.path.encode(),"query_string":parts.query.encode(),"headers":[],
               "client":("127.0.0.1",123),"server":("test",80),"root_path":""}, receive, send)
    return output[0]["status"], json.loads(b"".join(item.get("body",b"") for item in output[1:]))


class StockProfileAPITests(unittest.TestCase):
    def setUp(self):
        from app.main import app
        from app.api.stock_profiles import repository
        self.repo = MagicMock()
        app.dependency_overrides[repository] = lambda: self.repo
        self.addCleanup(lambda: app.dependency_overrides.pop(repository, None))

    def test_both_url_versions_and_query_market(self):
        self.repo.profile.return_value = {"code":"2330","listing_date":"1994-09-05"}
        for path in ("/api/stocks/2330/profile", "/api/v1/stocks/2330/profile"):
            status, body = asyncio.run(api_get(path))
            self.assertEqual(status, 200)
            self.assertEqual(body["listing_date"], "1994-09-05")
        self.assertEqual(asyncio.run(api_get("/api/stocks/2330/profile?market=TWSE"))[0],200)
        self.repo.profile.assert_called_with("2330", "TWSE")

    def test_not_found_ambiguous_or_database_unavailable(self):
        for error, expected in ((KeyError("2330"),404),(AmbiguousStockProfile("select market"),409),(MySQLRepositoryError("secret connection details"),503)):
            self.repo.profile.side_effect = error
            status, body = asyncio.run(api_get("/api/stocks/2330/profile"))
            self.assertEqual(status,expected)
            self.assertNotIn("secret",str(body))

    def test_invalid_code_and_market_rejected(self):
        for path in ("/api/stocks/233/profile","/api/stocks/TSEA/profile","/api/stocks/2330/profile?market=NYSE"):
            self.assertEqual(asyncio.run(api_get(path))[0],422)
        self.repo.profile.assert_not_called()

    def test_history_plans_logs_and_pagination(self):
        self.repo.history.return_value = {"total":0,"items":[]}
        self.repo.listing_plans.return_value = {"items":[]}
        self.repo.sync_runs.return_value = {"items":[]}
        for path in ("/api/stocks/2330/history?limit=10&offset=2", "/api/stocks/2330/listing-plans", "/api/stocks/sync-runs"):
            self.assertEqual(asyncio.run(api_get(path))[0],200)
        self.repo.history.assert_called_with("2330",10,2)
        self.assertEqual(asyncio.run(api_get("/api/stocks/2330/history?limit=10000"))[0],422)


if __name__ == "__main__":
    unittest.main()
