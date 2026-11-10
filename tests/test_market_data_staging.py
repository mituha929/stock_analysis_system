"""Verify quality boundaries and transaction behavior using isolated SQLite."""
import json
from pathlib import Path
import sqlite3
import unittest

from scripts.collect_market_history import daily_jobs, inspect
from scripts.stage_market_data import SCHEMA, stage_report


class MarketDataStagingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript(SCHEMA)
        self.jobs = [
            job for job in daily_jobs("2026-10-07")
            if job.market == "TPEx" and job.dataset == "institutional_market"
        ]

    def report(self, *deltas, digest="a" * 64):
        fields = ("buy_amount", "sell_amount", "net_amount")
        return {
            "dataset": "institutional_market", "market_scope": "fixture",
            "source": {"sha256": digest}, "units": dict.fromkeys(fields, "TWD"),
            "records": [
                {"institution_type": str(index), "values": dict(zip(fields, ("100", "30", str(70 - delta))))}
                for index, delta in enumerate(deltas)
            ],
        }

    def test_boundary_flags_scope_and_replay(self):
        securities, stocks = self.jobs
        original = self.report(-10, 0, 10)
        self.assertTrue(stage_report(self.connection, securities, original, Path("raw.bin"))["inserted"])
        self.assertFalse(stage_report(self.connection, securities, original, Path("raw.bin"))["inserted"])
        stage_report(self.connection, stocks, self.report(0), Path("raw.bin"))
        rows = self.connection.execute("""
            SELECT segment, net_difference_flag, net_difference_twd, record_json
            FROM staging_reports JOIN staging_records USING (report_key, raw_sha256)
            ORDER BY segment, record_key
        """).fetchall()
        self.assertEqual([row[:3] for row in rows], [
            ("", 1, "-10"), ("", 0, "0"), ("", 1, "10"), ("stocks", 0, "0"),
        ])
        self.assertEqual([json.loads(row[3])["values"]["net_amount"] for row in rows], ["80", "70", "60", "70"])

    def test_invalid_batch_never_partially_overwrites_previous_snapshot(self):
        job = self.jobs[0]
        stage_report(self.connection, job, self.report(0), Path("raw.bin"))
        for delta in (-11, 11):
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError, "outside_tolerance"):
                stage_report(self.connection, job, self.report(0, delta, digest="b" * 64), Path("raw.bin"))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM staging_records").fetchone(), (1,))
        self.assertEqual(self.connection.execute("SELECT raw_sha256 FROM staging_reports").fetchone(), ("a" * 64,))

    def test_official_response_cannot_silently_return_other_scope(self):
        securities, stocks = self.jobs
        raw = json.dumps({"stat": "ok", "date": "20261007", "tables": [{
            "prod": "0", "fields": ["單位名稱", "買進金額(元)", "賣出金額(元)", "買賣超(元)"],
            "data": [["投信", "100", "30", "70"]],
        }]}).encode()
        self.assertEqual(inspect(securities, {"http_status": 200}, raw)["status"], "verified")
        self.assertEqual(inspect(stocks, {"http_status": 200}, raw)["reason"], "institutional_market_scope_mismatch")
