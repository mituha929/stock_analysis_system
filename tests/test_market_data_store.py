"""P2 database checks with the production schema translated to isolated SQLite."""
from decimal import Decimal
from pathlib import Path
import sqlite3
import unittest

from scripts.collect_market_history import daily_jobs, financial_jobs
from scripts.market_data_quality import market_net_difference
from scripts.market_data_store import MarketDataStore, sqlite_schema


class MarketDataStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(sqlite_schema())
        self.store = MarketDataStore(self.db)
        self.jobs = [
            job for job in daily_jobs("2026-10-07")
            if job.market == "TPEx" and job.dataset == "institutional_market"
        ]
        self.raw = Path("fixture.bin")

    def report(self, job, digest, observed, records):
        return {
            "dataset": job.dataset, "market": job.market, "period": job.period, "segment": job.segment,
            "source": {"sha256": digest * 64, "fetched_at": observed},
            "units": dict.fromkeys(("buy_amount", "sell_amount", "net_amount"), "TWD"),
            "records": [
                {"institution_type": name, "values": {
                    "buy_amount": "100", "sell_amount": "30", "net_amount": str(net),
                }}
                for name, net in records
            ],
        }

    def test_versioning_idempotence_older_cache_and_scope(self):
        all_securities, stocks = self.jobs
        versions = (
            ("a", "2026-10-07T20:00:00+08:00", 69),
            ("b", "2026-10-08T09:00:00+08:00", 80),
            ("c", "2026-10-06T20:00:00+08:00", 60),
            ("a", "2026-10-07T20:00:00+08:00", 69),
        )
        outcomes = [
            self.store.import_report(all_securities, self.report(all_securities, digest, seen, [("投信", net)]), self.raw)
            for digest, seen, net in versions
        ]
        self.assertEqual([r["status"] for r in outcomes], ["promoted", "promoted", "archived", "unchanged"])
        self.store.import_report(stocks, self.report(stocks, "a", versions[0][1], [("投信", 70)]), self.raw)
        self.assertEqual(self.db.execute("""
            SELECT segment, current_revision FROM market_data_series ORDER BY segment
        """).fetchall(), [("securities", 2), ("stocks", 1)])
        self.assertEqual(self.db.execute("""
            SELECT revision, net_amount, net_difference_flag, net_difference_twd
            FROM market_data_institutional_market_daily WHERE report_key = ? ORDER BY revision
        """, (all_securities.key,)).fetchall(), [
            (1, "69", 1, "1"), (2, "80", 1, "-10"), (3, "60", 1, "10"),
        ])
        snapshot = self.db.execute("""
            SELECT first_seen_at, published_at, available_at FROM market_data_snapshots
            WHERE report_key = ? AND revision = 2
        """, (all_securities.key,)).fetchone()
        self.assertEqual(snapshot, ("2026-10-08 01:00:00", None, None))

    def test_rejected_and_failed_writes_preserve_current_revision(self):
        job = self.jobs[0]
        initial = self.report(job, "a", "2026-10-07T20:00:00+08:00", [("投信", 70), ("外資", 70)])
        self.store.import_report(job, initial, self.raw)
        seen = "2026-10-08T09:00:00+08:00"
        for records, error in [
            ([("投信", 59), ("外資", 70)], "outside_tolerance"),
            ([("投信", 81), ("外資", 70)], "outside_tolerance"),
            ([("投信", 70)], "coverage_decreased"),
        ]:
            with self.subTest(records=records), self.assertRaisesRegex(ValueError, error):
                self.store.import_report(job, self.report(job, "b", seen, records), self.raw)
        self.db.executescript("""
            CREATE TRIGGER fail_second_row BEFORE INSERT ON market_data_institutional_market_daily
            WHEN NEW.institution_type = '外資' BEGIN SELECT RAISE(ABORT, 'injected database failure'); END;
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected database failure"):
            self.store.import_report(job, self.report(job, "c", seen, [("投信", 69), ("外資", 70)]), self.raw)
        self.assertEqual(self.db.execute("SELECT current_revision FROM market_data_series").fetchall(), [(1,)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM market_data_snapshots").fetchone(), (1,))
        self.assertEqual(self.db.execute("SELECT count(*) FROM market_data_institutional_market_daily").fetchone(), (2,))

    def test_financial_amount_precision_units_and_unknown_mapping(self):
        job = next(job for job in financial_jobs("2026-Q3") if job.dataset == "income")
        report = self.report(job, "d", "2026-11-05T14:00:00+08:00", [])
        values = {"營業收入": "9007199254740993.123456789123", "基本每股盈餘（元）": None}
        report.update(value_basis="year_to_date", records=[{"stock_code": "0050", "name": "fixture", "values": values}],
                      units={"營業收入": "TWD_thousand", "基本每股盈餘（元）": "TWD/share"})
        result = self.store.import_report(job, report, self.raw)
        self.assertEqual((result["rows"], result["facts"]), (1, 2))
        self.assertEqual(self.db.execute("""
            SELECT amount, unit, standard_code FROM market_data_financial_facts WHERE source_label='營業收入'
        """).fetchone(), (values["營業收入"], "TWD_thousand", None))
        self.assertEqual(self.db.execute("""
            SELECT stock_code, period_type, consolidation_scope FROM market_data_financial_statements
        """).fetchone(), ("0050", "year_to_date", None))
        report["records"][0]["values"]["營業收入"] = "9007199254740993.1234567891234"
        with self.assertRaisesRegex(ValueError, "value_exceeds_decimal"):
            self.store.import_report(job, report, self.raw)

    def test_decimal_subtraction_does_not_round_large_operands(self):
        self.assertEqual(market_net_difference({
            "buy_amount": "12345678901234567890123456.000000000001",
            "sell_amount": "12345678901234567890123445",
            "net_amount": "1",
        }), Decimal("10.000000000001"))
