import asyncio
import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.schemas.screener import ScreenerRequest, SortSpec
from app.services.screener_service import screen
from app.services.screener_repository import ScreenerRepository
from app.services.mysql_instrument_repository import MySQLRepositoryError, MySQLSettings
from app.screening.ranking import rank_results
from app.schemas.models import Candle
from app.api.screener import scan_lock
from app.main import app
from test_rules import candles, request


def payload(**kwargs):
    return ScreenerRequest.model_validate({"rule": {"field": "price", "operator": ">", "value": 0}, **kwargs})


class FakeRepository:
    def __init__(self):
        self.rows = [{"symbol": s, "market_no": 0, "name": s, "category_no": 1, "category_name": "測試"} for s in ("1101", "2330", "9999")]
        self.data = {s: [Candle(**c) for c in candles(65)] for s in ("1101", "2330")}
        self.calls = []

    def instruments(self, markets, categories, table, symbols=None):
        return [r for r in self.rows if r["market_no"] in markets and (not categories or r["category_no"] in categories) and (symbols is None or r["symbol"] in symbols)]

    def candles(self, symbol, interval, table, limit):
        self.calls.append((symbol, limit))
        return self.data.get(symbol, [])[-limit:], None


class ScreenerTests(unittest.TestCase):
    def test_market_scan_counts_and_missing_data(self):
        result = screen(payload(), FakeRepository())
        self.assertTrue(result["complete"])
        self.assertEqual(result["universe_count"], 3)
        self.assertEqual(result["analyzed_count"], 2)
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(result["excluded_counts"]["missing_data"], 1)
        self.assertEqual([i["rank"] for i in result["items"]], [1, 2])
        self.assertIn("latest_candle_at", result["items"][0])
        self.assertTrue(result["items"][0]["reasons"])

    def test_prefilter_prevents_full_analysis_reads(self):
        repo = FakeRepository()
        result = screen(payload(prefilter={"min_price": 10000}), repo)
        self.assertEqual(result["analyzed_count"], 0)
        self.assertEqual(result["excluded_counts"]["prefiltered"], 2)
        self.assertTrue(all(limit == 1 for _, limit in repo.calls))

    def test_explicit_symbols_and_unknown_report(self):
        result = screen(payload(universe={"kind": "symbols", "symbols": ["2330", "2330", "8888"]}), FakeRepository())
        self.assertEqual(result["universe_count"], 1)
        self.assertEqual(result["outside_universe_count"], 1)
        self.assertEqual(result["excluded"][0]["symbol"], "8888")

    def test_empty_watchlist_does_not_expand_to_all_market(self):
        result = screen(payload(universe={"kind": "watchlist"}), FakeRepository(), [])
        self.assertEqual(result["universe_count"], 0)
        self.assertEqual(result["items"], [])
        result = screen(payload(universe={"kind": "watchlist"}), FakeRepository(), ["2330"])
        self.assertEqual(result["items"][0]["symbol"], "2330")

    def test_category_filter_and_symbol_deduplication(self):
        repo = FakeRepository()
        repo.rows.append({**repo.rows[0], "market_no": 1})
        result = screen(payload(), repo)
        self.assertEqual(result["universe_count"], 3)
        self.assertEqual(result["items"][0]["markets"], [0, 1])
        self.assertEqual(screen(payload(universe={"category_nos": [99]}), repo)["universe_count"], 0)

    def test_null_score_and_missing_indicator_handling(self):
        repo = FakeRepository()
        repo.data["2330"] = repo.data["2330"][:10]
        result = screen(payload(), repo)
        item = next(i for i in result["items"] if i["symbol"] == "2330")
        self.assertIsNone(item["direction_score"])
        self.assertEqual(item["rank"], 2)
        result = screen(payload(rule={"field": "ma20", "operator": ">", "value": 0}), repo)
        self.assertEqual(result["excluded_counts"]["insufficient_data"], 1)

    def test_rule_not_matched_separate_from_missing(self):
        result = screen(payload(rule={"field": "price", "operator": ">", "value": 99999}), FakeRepository())
        self.assertEqual(result["excluded_counts"]["not_matched"], 2)
        self.assertEqual(result["matched_count"], 0)

    def test_limit_applied_after_sorting_entire_scope(self):
        result = screen(payload(limit=1, sort=[{"field": "symbol", "direction": "desc"}]), FakeRepository())
        self.assertEqual(result["processed_count"], 3)
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(result["items"][0]["symbol"], "2330")
        self.assertTrue(result["truncated"])

    def test_timeout_explicit_partial_and_remaining(self):
        ticks = iter([0, 0, 2, 2])
        result = screen(payload(timeout_seconds=1), FakeRepository(), clock=lambda: next(ticks))
        self.assertFalse(result["complete"])
        self.assertFalse(result["ranking_complete"])
        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(result["remaining_symbols"], ["2330", "9999"])

    def test_database_error_is_not_empty_success(self):
        repo = FakeRepository()
        repo.candles = MagicMock(side_effect=MySQLRepositoryError("offline"))
        with self.assertRaises(MySQLRepositoryError):
            screen(payload(), repo)

    def test_invalid_input(self):
        for change in ({"prefilter": {"min_price": 20, "max_price": 10}}, {"universe": {"kind": "symbols"}}, {"universe": {"markets": [5]}}, {"history_bars": 1}, {"sort": [{"field": "invalid", "direction": "asc"}]}, {"rule": {"field": "invalid", "operator": ">", "value": 0}}, {"universe": {"symbols": ["2330"]}}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                payload(**change)

    def test_ranking_lexicographic_ties_and_nulls(self):
        rows = [{"symbol": "b", "market_no": 0, "matched_count": 2, "direction_score": 80, "risk_score": 60},
                {"symbol": "a", "market_no": 0, "matched_count": 2, "direction_score": 80, "risk_score": 50},
                {"symbol": "c", "market_no": 0, "matched_count": 1, "direction_score": 99, "risk_score": 0}]
        self.assertEqual([i["symbol"] for i in rank_results(rows, payload().sort)], ["a", "b", "c"])
        rows[0]["risk_score"] = None
        for direction in ("asc", "desc"):
            self.assertEqual(rank_results(rows, [SortSpec(field="risk_score", direction=direction)])[-1]["symbol"], "b")

    def test_http_scan_and_busy(self):
        with patch("app.api.screener.ScreenerRepository") as factory:
            factory.return_value.__enter__.return_value = FakeRepository()
            status, result = asyncio.run(request("POST", "/api/v1/screener/run", payload().model_dump(mode="json")))
            self.assertEqual(status, 200)
            self.assertTrue(result["complete"])
            scan_lock.acquire()
            try:
                self.assertEqual(asyncio.run(request("POST", "/api/v1/screener/run", payload().model_dump(mode="json")))[0], 409)
            finally:
                scan_lock.release()

    def test_http_invalid_and_database_error_release_lock(self):
        with patch("app.api.screener.ScreenerRepository") as factory:
            status, _ = asyncio.run(request("POST", "/api/v1/screener/run", {"rule": {"field": "oops", "operator": ">", "value": 0}}))
            self.assertEqual(status, 422)
            factory.assert_not_called()
            factory.side_effect = MySQLRepositoryError("offline")
            status, _ = asyncio.run(request("POST", "/api/v1/screener/run", payload().model_dump(mode="json")))
            self.assertEqual(status, 503)
            self.assertFalse(scan_lock.locked())

    def test_openapi_exposes_request_and_categories(self):
        self.assertIn("ScreenerRequest", app.openapi()["components"]["schemas"])
        with patch("app.api.screener.ScreenerRepository") as factory:
            factory.return_value.__enter__.return_value = FakeRepository()
            status, result = asyncio.run(request("GET", "/api/v1/screener/categories"))
            self.assertEqual(status, 200)
            self.assertEqual(result["items"][0]["instrument_count"], 3)


class ScreenerRepositoryTests(unittest.TestCase):
    def test_readonly_session_parameters_and_cleanup(self):
        settings = MySQLSettings("localhost", 3306, "test", "unused", "test")
        repo = ScreenerRepository(settings)
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchall.return_value = []
        with patch.object(repo, "_connector"), patch.object(repo, "_connect_database", return_value=connection):
            with repo as session:
                session.instruments([0, 1], [1], "candles", ["2330"])
                sql, params = cursor.execute.call_args.args
                self.assertIn("is_active=TRUE", sql)
                self.assertIn("category_no NOT IN (94,98)", sql)
                self.assertEqual(params, (0, 1, 1, "2330"))
                self.assertNotIn("2330", sql)
                session.candles("2330", "day", "candles", 360)
                self.assertEqual(cursor.execute.call_args.args[1], ("2330", "day", 360))
        connection.start_transaction.assert_called_once_with(isolation_level="REPEATABLE READ", consistent_snapshot=True, readonly=True)
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()
        connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
