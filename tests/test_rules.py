import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from app.main import app
from app.rules.context import build_context
from app.rules.engine import evaluate
from app.rules.schemas import RuleTestRequest
from app.api.routes import analysis
from app.signals import analyze_technical


def leaf(field="price", operator=">", value=100):
    return {"field": field, "operator": operator, "value": value}


def parse(rule):
    return RuleTestRequest.model_validate({"rule": rule}).rule


def candles(count=65):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"timestamp": start + timedelta(days=i), "open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i, "volume": 1000 + i * 10} for i in range(count)]


async def request(method, path, body=None):
    """Exercise FastAPI validation and serialization without an HTTP dependency."""
    output = []
    async def receive():
        return {"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}
    async def send(message):
        output.append(message)
    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 123), "server": ("test", 80), "root_path": ""}, receive, send)
    return output[0]["status"], json.loads(b"".join(m.get("body", b"") for m in output[1:]))


class RuleEngineTests(unittest.TestCase):
    def test_numeric_operators_and_inclusive_between(self):
        for op, value, expected in ((">", 100, False), (">=", 100, True), ("<", 101, True), ("<=", 100, True), ("==", 100, True), ("!=", 100, False), ("between", [100, 110], True), ("between", [90, 99], False)):
            with self.subTest(op=op, value=value):
                self.assertEqual(evaluate(parse(leaf(operator=op, value=value)), {"price": 100})["matched"], expected)

    def test_moving_reference_cross_uses_both_current_values(self):
        rule = parse(leaf(operator="cross_above", value={"field": "ma20"}))
        # Price exceeded the old MA, but remains below the new MA: no cross.
        self.assertFalse(evaluate(rule, {"price": 105, "ma20": 110}, {"price": 99, "ma20": 100})["matched"])
        self.assertTrue(evaluate(rule, {"price": 111, "ma20": 110}, {"price": 100, "ma20": 100})["matched"])

    def test_cross_below_and_no_repeat(self):
        rule = parse(leaf(operator="cross_below"))
        self.assertTrue(evaluate(rule, {"price": 99}, {"price": 100})["matched"])
        self.assertFalse(evaluate(rule, {"price": 98}, {"price": 99})["matched"])
        self.assertEqual(evaluate(rule, {"price": 99})["status"], "insufficient_data")

    def test_nested_logic_counts_every_leaf_without_short_circuit(self):
        tree = {"logic": "AND", "rules": [leaf(), {"logic": "OR", "rules": [leaf(value=200), leaf("rsi14", "<=", 70)]}]}
        result = evaluate(parse(tree), {"price": 110, "rsi14": 50})
        self.assertTrue(result["matched"])
        self.assertEqual((result["matched_count"], result["total_rules"]), (2, 3))
        self.assertEqual(result["details"][2]["path"], "rule.1.1")

    def test_missing_ne_nan_and_boolean_are_not_numeric_matches(self):
        for value in (None, float("nan"), float("inf"), True):
            with self.subTest(value=value):
                result = evaluate(parse(leaf(operator="!=")), {"price": value})
                self.assertFalse(result["matched"])
                self.assertEqual(result["unknown_count"], 1)

    def test_three_valued_group_logic(self):
        for logic, price, expected in (("OR", 110, "matched"), ("OR", 90, "insufficient_data"), ("AND", 90, "not_matched"), ("AND", 110, "insufficient_data")):
            tree = {"logic": logic, "rules": [leaf(), leaf("rsi14", "<", 70)]}
            self.assertEqual(evaluate(parse(tree), {"price": price})["status"], expected)

    def test_boolean_and_trend(self):
        self.assertTrue(evaluate(parse(leaf("macd_golden_cross", "==", False)), {"macd_golden_cross": False})["matched"])
        self.assertTrue(evaluate(parse(leaf("trend", "==", "bullish")), {"trend": "bullish"})["matched"])

    def test_invalid_rules(self):
        invalid = [leaf("invented"), leaf(value=True), leaf(value="100"), leaf(value=float("nan")), leaf(operator="eval"), leaf(operator="between", value=[1]), leaf(operator="between", value=[2, 1]), leaf(operator="between", value=[False, 10]), leaf("risk_score", ">", 101), leaf("rsi14", "between", [-1, 50]), leaf("trend", "==", "up"), leaf("macd_golden_cross", ">", True), leaf("macd_golden_cross", "==", 1), leaf(value={"field": "trend"}), leaf(value={"field": "unknown"}), {**leaf(), "extra": 1}, {"logic": "AND", "rules": []}]
        for rule in invalid:
            with self.subTest(rule=rule), self.assertRaises(ValidationError):
                parse(rule)

    def test_tree_limits(self):
        tree = leaf()
        for _ in range(10):
            tree = {"logic": "AND", "rules": [tree]}
        for invalid in (tree, {"logic": "OR", "rules": [leaf()] * 100}):
            with self.assertRaises(ValidationError):
                parse(invalid)


class RuleContextAndAPITests(unittest.TestCase):
    def response(self, count=65):
        with patch("app.api.routes.MySQLCandleRepository") as repo:
            data = analyze_technical(candles(count))["indicators"]["candles"]
            repo.return_value.get_candles_with_metadata.return_value = (data, None)
            repo.return_value.get_instrument_name.return_value = "測試股票"
            return analysis("2330")

    def test_indicator_and_score_mapping(self):
        response = self.response()
        current, previous = build_context(response)
        self.assertEqual(current["ma20"], response.indicators["ma"]["ma20"][-1])
        self.assertEqual(previous["ma20"], response.indicators["ma"]["ma20"][-2])
        self.assertEqual(current["trend_score"], response.scores["trend"])
        self.assertEqual(current["direction_score"], response.scores["direction_score"])
        self.assertIsNotNone(previous["direction_score"])
        self.assertEqual(current["trend"], response.summary["direction"])

    def test_warmup_missing_fields_remain_unknown(self):
        current, previous = build_context(self.response(19))
        for field in ("ma20", "risk_score", "trend", "breakout_20d_high"):
            self.assertIsNone(current[field])
        current, previous = build_context(self.response(20))
        self.assertIsNotNone(current["risk_score"])
        self.assertIsNone(previous["risk_score"])

    def test_boolean_cross_is_not_taken_from_legacy_signal_string(self):
        response = self.response()
        response.indicators["macd"]["dif"][-2:] = [99, 105]
        response.indicators["macd"]["signal"][-2:] = [100, 110]
        response.signals["macd_cross"] = "golden_cross"
        current, _ = build_context(response)
        self.assertFalse(current["macd_golden_cross"])

    def test_http_success_and_discovery(self):
        with patch("app.api.rules.analysis", return_value=self.response()) as mocked:
            status, result = asyncio.run(request("POST", "/api/v1/rules/test/2330", {"rule": leaf(value={"field": "ma20"}), "history_interval": "week", "candle_table": "odd_lot_candle"}))
            self.assertEqual(status, 200)
            self.assertTrue(result["matched"])
            self.assertIn("latest_candle_at", result)
            self.assertEqual(result["total_rules"], 1)
            mocked.assert_called_once_with("2330", history_interval="week", candle_table="odd_lot_candle")
        status, result = asyncio.run(request("GET", "/api/v1/rules/fields"))
        self.assertEqual(status, 200)
        self.assertIn("price", [f["field"] for f in result["fields"]])

    def test_http_validation_precedes_database(self):
        for body in ({"rule": leaf("unknown")}, {"rule": leaf(), "history_interval": "bad"}, {"rule": leaf(), "candle_table": "candles;DROP"}):
            with patch("app.api.rules.analysis") as mocked:
                status, _ = asyncio.run(request("POST", "/api/v1/rules/test/2330", body))
                self.assertEqual(status, 422)
                mocked.assert_not_called()

    def test_http_database_missing_and_failure(self):
        from app.services.mysql_instrument_repository import MySQLRepositoryError
        with patch("app.api.routes.MySQLCandleRepository") as repo:
            repo.return_value.get_candles_with_metadata.return_value = ([], None)
            status, _ = asyncio.run(request("POST", "/api/v1/rules/test/2330", {"rule": leaf()}))
            self.assertEqual(status, 404)
            repo.side_effect = MySQLRepositoryError("database unavailable")
            status, _ = asyncio.run(request("POST", "/api/v1/rules/test/2330", {"rule": leaf()}))
            self.assertEqual(status, 503)

    def test_openapi_contains_recursive_request(self):
        schema = app.openapi()
        self.assertIn("/api/v1/rules/test/{symbol}", schema["paths"])
        # Once RuleGroup is used in responses, FastAPI may emit Input/Output names.
        components = schema["components"]["schemas"]
        alternatives = components["RuleTestRequest"]["properties"]["rule"]["anyOf"]
        groups = [item["$ref"].split("/")[-1] for item in alternatives if "RuleGroup" in item.get("$ref", "")]
        self.assertEqual(len(groups), 1)
        group = components[groups[0]]
        self.assertEqual(group["properties"]["logic"]["enum"], ["AND", "OR"])
        self.assertIn({"$ref": "#/components/schemas/" + groups[0]}, group["properties"]["rules"]["items"]["anyOf"])


if __name__ == "__main__":
    unittest.main()
