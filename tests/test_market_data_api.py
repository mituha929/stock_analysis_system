"""P5 ASGI authorization, pagination and exact values with isolated SQLite."""
import asyncio
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI

from app.api import dashboard_market_data, market_data
from app.schemas.models import Candle
from app.services.capital_dll_quote_service import CapitalApiError
from app.services.taiwan_market_indices import IndexSnapshots
from app.services.market_data_queries import MarketDataQueries
from scripts.collect_market_history import daily_jobs, financial_jobs, revenue_jobs
from scripts.market_data_store import MarketDataStore, sqlite_schema
from scripts.market_data_workflow import WORKFLOW_DDL

TOKEN = "market-data-fixture-token-for-test"


async def request(app, url, *, token=TOKEN, method="GET", client="test", extra_headers=()):
    path, _, query = url.partition("?")
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    headers.extend(extra_headers)
    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": headers,
        "scheme": "http", "server": ("test", 80), "client": (client, 123), "root_path": "",
    }, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(body)


class MarketDataAPITests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.executescript(sqlite_schema() + sqlite_schema(WORKFLOW_DDL))
        self.store = MarketDataStore(self.db)
        self.app = FastAPI()
        self.app.include_router(market_data.router)
        self.app.dependency_overrides[market_data.repository] = lambda: MarketDataQueries(self.db)
        self.addCleanup(market_data.recent_requests.clear)
        market_data.recent_requests.clear()
        env = patch.dict("os.environ", {"CAPITAL_MARKET_DATA_API_ENABLED": "true", "CAPITAL_MARKET_DATA_API_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)

    def add_report(self, job, records, units, digest, *, value_basis=None, fetched="2026-11-05T13:00:00+08:00"):
        self.store.import_report(job, {
            "dataset": job.dataset, "market": job.market, "period": job.period, "segment": job.segment,
            "source": {"source_url": job.url, "sha256": digest * 64, "fetched_at": fetched},
            "units": units, "records": records, "value_basis": value_basis,
        }, Path("internal-file.bin"))

    def get(self, url, **kwargs):
        return asyncio.run(request(self.app, url, **kwargs))

    def test_authorization_validation_and_rate_limit(self):
        url = "/api/v1/data/status"
        self.assertEqual(self.get(url, token=None)[0], 401)
        with patch.dict("os.environ", {"CAPITAL_MARKET_DATA_API_ENABLED": "false"}):
            self.assertEqual(self.get(url)[0], 404)
        self.assertEqual(self.get(url, method="POST")[0], 405)
        self.assertEqual(self.get(url + "?limit=101")[0], 422)
        for _ in range(59):
            self.assertEqual(self.get(url)[0], 200)
        self.assertEqual(self.get(url)[0], 429)

    def test_market_scope_flags_and_pagination(self):
        jobs = [job for job in daily_jobs("2026-10-07")
                if job.market == "TPEx" and job.dataset == "institutional_market"]
        units = dict.fromkeys(("buy_amount", "sell_amount", "net_amount"), "TWD")
        for job, digest, net in zip(jobs, ("a", "b"), ("69", "70")):
            self.add_report(job, [
                {"institution_type": kind, "values": {"buy_amount": "100", "sell_amount": "30", "net_amount": net}}
                for kind in ("投信", "外資")
            ], units, digest)
        base = "/api/v1/markets/TPEX/institutional?start=2026-10-07&end=2026-10-07"
        self.assertEqual(self.get(base)[0], 422)
        status, page = self.get(base + "&scope=securities&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(page["next_offset"], 1)
        row = page["data"][0]
        self.assertEqual((row["values"]["net_amount"], row["net_difference_flag"], row["net_difference_twd"]),
                         ("69", True, "1"))
        self.assertFalse(row["is_complete"])
        self.assertNotIn("internal-file", json.dumps(page))
        _, page2 = self.get(base + "&scope=securities&limit=1&offset=1")
        self.assertNotEqual(page2["data"][0]["institution_type"], row["institution_type"])
        _, stocks = self.get(base + "&scope=stocks")
        self.assertEqual(stocks["data"][0]["values"]["net_amount"], "70")

    def test_revenue_financial_and_remaining_stock_routes(self):
        code = "0050"
        daily = {j.dataset: j for j in daily_jobs("2026-11-04") if j.market == "TWSE"}
        for dataset, values, unit in (
            ("institutional_stock", {"foreign_net_shares_excluding_foreign_dealers": "-3",
             "investment_trust_net_shares": "0", "dealer_net_shares": "2"}, "shares"),
            ("margin", {"financing_previous_balance": "10", "financing_balance": "11",
             "short_previous_balance": "1", "short_balance": "0"}, "lots"),
        ):
            self.add_report(daily[dataset], [{"stock_code": code, "name": "測試", "values": values}],
                            dict.fromkeys(values, unit), "c")
        revenue = next(revenue_jobs("2026-09"))
        values = dict.fromkeys(("revenue", "previous_month_revenue", "previous_year_same_month_revenue",
                                "mom_percent", "yoy_percent", "year_to_date_revenue",
                                "previous_year_to_date_revenue", "year_to_date_yoy_percent"))
        values["revenue"] = "9007199254740993"
        self.add_report(revenue, [{"stock_code": code, "name": "測試", "values": values}],
                        dict.fromkeys(values, "TWD_thousand"), "d")
        statement = next(financial_jobs("2026-Q3"))
        facts = {"營業收入": "9007199254740993", "基本每股盈餘（元）": "1.25"}
        self.add_report(statement, [{"stock_code": code, "name": "測試", "values": facts}],
                        {"營業收入": "TWD_thousand", "基本每股盈餘（元）": "TWD/share"}, "e", value_basis="year_to_date")
        base = f"/api/v1/stocks/{code}"
        day_query = "?start=2026-11-04&end=2026-11-04"
        for path in ("/institutional" + day_query, "/margin" + day_query, "/revenue?months=1",
                     "/financials?year=2026&quarter=3&statement=income_statement"):
            status, page = self.get(base + path)
            self.assertEqual(status, 200)
            self.assertEqual(page["data"][0]["stock_code"], code)
            self.assertIsNone(page["data"][0]["published_at"])
        self.assertEqual(page["data"][0]["values"], facts)
        self.assertEqual(page["data"][0]["unit"]["基本每股盈餘（元）"], "TWD/share")
        self.assertEqual(self.get(base + "/margin?start=2020-01-01&end=2026-11-04")[0], 422)

    def test_dashboard_preview_is_local_and_preserves_report_scope(self):
        jobs = [job for job in daily_jobs("2026-10-07")
                if job.market == "TPEx" and job.dataset == "institutional_market"]
        units = dict.fromkeys(("buy_amount", "sell_amount", "net_amount"), "TWD")
        for job, digest, net in zip(jobs, ("a", "b"), ("69", "70")):
            self.add_report(job, [
                {"institution_type": "投信", "values": {"buy_amount": "100", "sell_amount": "30", "net_amount": net}}
            ], units, digest)
        stock_values = dict.fromkeys(("foreign_net_shares_excluding_foreign_dealers",
                                     "investment_trust_net_shares", "dealer_net_shares"), "10")
        for date, market in (("2026-10-06", "TPEx"), ("2026-10-07", "TWSE")):
            job = next(job for job in daily_jobs(date)
                       if job.market == market and job.dataset == "institutional_stock")
            self.add_report(job, [{"stock_code": "1234", "name": "測試", "values": stock_values}],
                            dict.fromkeys(stock_values, "shares"), "c")
        self.app.include_router(dashboard_market_data.router)
        url = "/api/local/dashboard/market-summary/TPEx?dataset=institutional_market"
        local = [(b"host", b"127.0.0.1:8000")]
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            db_file = Path(folder) / "dashboard.sqlite3"
            disk = sqlite3.connect(db_file)
            try:
                self.db.backup(disk)
            finally:
                disk.close()
            with patch.object(dashboard_market_data, "LOCAL_DB", db_file):
                self.assertEqual(self.get(url, client="192.0.2.1", extra_headers=local)[0], 403)
                self.assertEqual(self.get(url, client="127.0.0.1",
                                          extra_headers=local + [(b"sec-fetch-site", b"cross-site")])[0], 403)
                self.assertEqual(self.get(url, client="127.0.0.1",
                                          extra_headers=local + [(b"x-forwarded-for", b"192.0.2.1")])[0], 403)
                status, securities = self.get(url + "&scope=securities", client="127.0.0.1", extra_headers=local)
                self.assertEqual((status, securities["periods"]), (200, ["2026-10-07"]))
                self.assertTrue(securities["data"][0]["net_difference_flag"])
                _, stocks = self.get(url + "&scope=stocks", client="127.0.0.1", extra_headers=local)
                self.assertFalse(stocks["data"][0]["net_difference_flag"])
                stock_url = "/api/local/dashboard/market-data/1234"
                status, stock = self.get(stock_url, client="127.0.0.1", extra_headers=local)
                self.assertEqual((status, stock["market"], stock["periods"]),
                                 (200, "TWSE", ["2026-10-07", "2026-10-06"]))
                _, older = self.get(stock_url + "?period=2026-10-06", client="127.0.0.1", extra_headers=local)
                self.assertEqual(older["market"], "TPEx")

    def test_index_view_requests_capital_daily_candles_for_both_markets(self):
        self.app.include_router(dashboard_market_data.router)
        self.app.state.capital_dll_async_lock = asyncio.Lock()
        capital = Mock()
        self.app.state.index_snapshots = IndexSnapshots(capital)
        local = {"client": "127.0.0.1", "extra_headers": [(b"host", b"127.0.0.1:8000")]}
        end = date(2026, 11, 10)
        capital.fetch_historical_candles.return_value = [
            Candle(timestamp=datetime.fromisoformat(f"2026-11-{day:02d}T00:00:00+08:00"),
                   open=325.41, high=338.52, low=320.00, close=close, volume=123)
            for day, close in ((10, 332.97), (9, 321.97))
        ]
        for market, symbol in (("TWSE", "TSEA"), ("TPEx", "OTCA")):
            with self.subTest(market=market):
                url = f"/api/local/dashboard/market-summary/{market}"
                with patch("app.services.taiwan_market_indices.daily_aggregate_end", return_value=end):
                    status, result = self.get(url, **local)
                    _, older = self.get(url + "?period=2026-11-09", **local)
                capital.fetch_historical_candles.assert_called_once_with(symbol, "day", date(2026, 10, 11), end)
                capital.fetch_historical_candles.reset_mock()
                self.assertEqual((status, result["symbol"], result["source"], result["period"]),
                                 (200, symbol, "群益 API", "2026-11-10"))
                self.assertEqual(result["data"][0]["values"],
                                 {"open": "325.41", "high": "338.52", "low": "320.00", "close": "332.97", "change": "11.00"})
                self.assertIsNone(older["data"][0]["values"]["change"])

        capital.fetch_historical_candles.side_effect = CapitalApiError("offline")
        with patch("app.services.taiwan_market_indices.daily_aggregate_end", return_value=date(2026, 11, 11)):
            url = "/api/local/dashboard/market-summary/TWSE"
            _, stale = self.get(url, **local)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["period"], "2026-11-10")
            self.app.state.index_snapshots = IndexSnapshots(capital)
            self.assertEqual(self.get(url, **local)[0], 503)
