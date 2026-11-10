"""Schedule, retry, coverage, and resumability checks against isolated SQLite."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts.collect_market_history import chip_jobs, daily_jobs, financial_jobs
from scripts.market_data_store import MarketDataStore, sqlite_schema
from scripts.market_data_workflow import (
    WORKFLOW_DDL, backfill_page, checkpoints, fetch_report, run_jobs, schedule_plan, scheduled_page,
    source_id, workflow_lock,
)
from scripts.stage_market_data import load_report


class MarketDataWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(sqlite_schema() + sqlite_schema(WORKFLOW_DDL))
        self.store = MarketDataStore(self.db)
        self.calendar = {"range_start": "2026-09-01", "range_end": "2026-10-26",
                         "closed_dates": ["2026-10-09"]}
        self.job = next(j for j in daily_jobs("2026-10-07")
                        if j.market == "TPEx" and j.dataset == "institutional_market")

    def reader(self, records, *, fetched="2026-10-07T22:00:00+08:00"):
        reader = Mock(folder=Path("."))
        raw = json.dumps({"stat": "ok", "date": "20261007", "tables": [{
            "prod": "0", "fields": ["單位名稱", "買進金額(元)", "賣出金額(元)", "買賣超(元)"], "data": records,
        }]}).encode()
        reader.read.return_value = ({
            "http_status": 200, "source_url": self.job.url, "parameters": self.job.parameters,
            "sha256": hashlib.sha256(raw).hexdigest(), "cache_key": "fixture", "fetched_at": fetched,
        }, raw)
        return reader

    def test_calendar_windows_and_successful_slot_skips(self):
        now = datetime.fromisoformat("2026-10-12T09:05:00+08:00")
        due, deferred = schedule_plan(now, self.calendar, {"daily"})
        self.assertEqual(deferred, [])
        self.assertEqual({job.period for job, _ in due}, {"2026-10-08"})
        key = due[0][0].key
        progress = {key: ("promoted", "2026-10-11 14:00:00", "2026-10-11 14:00:00", 1)}
        self.assertNotIn(key, {job.key for job in scheduled_page(due, progress, now, 50)})
        month, _ = schedule_plan(datetime.fromisoformat("2026-10-31T20:00:00+08:00"), self.calendar, {"revenue"})
        self.assertEqual({(job.period, refresh) for job, refresh in month}, {("2026-08", True), ("2026-09", True)})
        quarter, _ = schedule_plan(datetime.fromisoformat("2026-10-01T21:00:00+08:00"), self.calendar, {"financial"})
        self.assertEqual({(job.period, refresh) for job, refresh in quarter},
                         {("2026-Q1", True), ("2026-Q2", True), ("2026-Q3", True)})
        due, deferred = schedule_plan(datetime.fromisoformat("2026-11-10T18:00:00+08:00"), self.calendar, {"daily"})
        self.assertEqual(due, [])
        self.assertEqual(deferred[0]["reason"], "trading_calendar_coverage_required")

    def test_financial_deadline_followup_and_overlapping_quarters(self):
        def periods(day):
            now = datetime.fromisoformat(f"{day}T21:00:00+08:00")
            planned, _ = schedule_plan(now, self.calendar, {"financial"})
            self.assertTrue(all(refresh for _, refresh in planned))
            self.assertEqual(len(planned), len({job.key for job, _ in planned}))
            return {job.period for job, _ in planned}

        self.assertIn("2026-Q3", periods("2026-11-30"))
        self.assertIn("2026-Q3", periods("2026-12-01"))
        self.assertEqual(periods("2026-12-02"), set())
        self.assertTrue({"2026-Q4", "2027-Q1"} <= periods("2027-04-01"))

    def test_financial_partial_report_is_saved_without_claiming_coverage(self):
        job = next(financial_jobs("2026-Q3"))
        digest = "a" * 64
        report = {
            "dataset": job.dataset, "market": job.market, "period": job.period, "segment": job.segment,
            "source": {"source_url": job.url, "sha256": digest, "fetched_at": "2026-11-10T21:00:00+08:00"},
            "records": [{"stock_code": "2330", "name": "測試", "values": {"營業收入": "1"}}],
            "units": {"營業收入": "TWD_thousand"}, "value_basis": "year_to_date",
        }
        with patch("scripts.market_data_workflow.fetch_report", return_value=({"sha256": digest}, b"", 1)), \
                patch("scripts.market_data_workflow.verify_report", return_value=(report, Path("sample"))):
            result, = run_jobs(self.store, Mock(), [job], live=True, approved=[source_id(job)],
                               coverage={job.key: ["2330", "1234"]})["results"]
        self.assertEqual((result["status"], result["missing_rows"]), ("promoted", 1))
        self.assertFalse(checkpoints(self.store)[job.key][3])
        self.assertTrue(report["latest_quarter_partial"])

    def test_market_margin_amount_requires_matching_dated_stock_balances(self):
        day = "2026-11-10"
        jobs = list(chip_jobs(day, include_board=True))
        board_job = next(job for job in jobs if job.segment == "amount")
        stock_job = next(job for job in jobs if job.market == "TPEx" and job.dataset == "margin_market"
                         and not job.segment)
        fields = ["代號", "名稱", "前資餘額(張)"] + ["欄"] * 17
        fields[6], fields[10], fields[14] = "資餘額", "前券餘額(張)", "券餘額"
        row = ["6488", "測試", "10", "0", "0", "0", "12", "0", "0", "0",
               "3", "0", "0", "0", "4", "0", "0", "0", "0", ""]
        stock = {"date": "20261110", "tables": [{"fields": fields, "data": [row]}]}
        board = {key: [{"date": "11/10", "count": value}] for key, value in (
            ("marginPurchaseShares", "12"), ("marginShortSaleShares", "4"), ("marginPurchaseValue", "999"),
        )}

        def read(job):
            payload = board if job == board_job else stock
            self.assertIn(job, (board_job, stock_job))
            raw = json.dumps(payload).encode()
            return ({"http_status": 200, "source_url": job.url, "parameters": job.parameters,
                     "sha256": hashlib.sha256(raw).hexdigest(), "cache_key": "fixture",
                     "fetched_at": "2026-11-11T18:00:00+08:00"}, raw)

        reader = Mock(folder=Path("."), **{"read.side_effect": read})
        report, path = load_report(reader, board_job)
        self.assertEqual(report["records"][0]["values"]["balance"], "999")
        self.assertEqual(self.store.import_report(board_job, report, path)["status"], "promoted")
        board["marginShortSaleShares"][0]["count"] = "5"
        with self.assertRaisesRegex(ValueError, "date_crosscheck_failed"):
            load_report(reader, board_job)

    def test_live_retry_limits_blocking_and_unapproved_sources(self):
        reader = Mock()
        pause = Mock()
        reader.read.side_effect = [({"http_status": 503}, b"err")] * 2 + [({"http_status": 200}, b"ok")]
        self.assertEqual(fetch_report(reader, self.job, live=True, sleep=pause)[1:], (b"ok", 3))
        self.assertEqual(pause.call_count, 2)
        for response in ({"http_status": 429}, {"http_status": 503, "source_blocked": True}):
            reader.reset_mock(side_effect=True)
            reader.read.return_value = response, b""
            self.assertEqual(fetch_report(reader, self.job, live=True, sleep=pause)[2], 1)
            reader.read.assert_called_once()
        reader.reset_mock()
        result = run_jobs(self.store, reader, [self.job], live=True)
        reader.read.assert_not_called()
        self.assertEqual(result["results"][0]["error"], "source_use_not_approved")
        result = run_jobs(self.store, reader, [self.job], live=True, approved=[source_id(self.job)])
        reader.read.assert_not_called()
        self.assertEqual(result["results"][0]["error"], "coverage_unverified")

    def test_coverage_failure_keeps_current_and_backfill_can_resume(self):
        expected = {self.job.key: ["投信", "外資"]}
        approved = [source_id(self.job)]
        incomplete = self.reader([["投信", "100", "30", "70"]])
        failure = run_jobs(self.store, incomplete, [self.job], live=True, approved=approved, coverage=expected)
        self.assertEqual(failure["results"][0]["missing_rows"], 1)
        self.assertEqual(backfill_page([self.job], checkpoints(self.store), 1), [self.job])
        complete = self.reader([["投信", "100", "30", "70"], ["外資", "100", "30", "69"]])
        result = run_jobs(self.store, complete, [self.job], live=True, approved=approved, coverage=expected)
        self.assertEqual(result["results"][0]["flagged_rows"], 1)
        self.assertEqual(backfill_page([self.job], checkpoints(self.store), 1), [])
        newer = self.reader([["投信", "200", "30", "170"]], fetched="2026-10-08T22:00:00+08:00")
        run_jobs(self.store, newer, [self.job], live=True, approved=approved, coverage=expected)
        run_jobs(self.store, complete, [self.job])
        self.assertEqual(self.db.execute("SELECT current_revision FROM market_data_series").fetchone(), (1,))
        self.assertEqual(self.db.execute("SELECT coverage_verified FROM market_data_checkpoints").fetchone(), (1,))

    def test_single_runner_lock_releases_after_failure(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            db = Path(folder) / "test.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "injected"), workflow_lock(self.store, db):
                with self.assertRaisesRegex(ValueError, "lock_unavailable"), workflow_lock(self.store, db):
                    self.fail("second writer must not acquire the lock")
                raise RuntimeError("injected")
            with workflow_lock(self.store, db):
                pass
