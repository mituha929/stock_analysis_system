import asyncio
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.ai.ollama_client import AIError
from app.ai.rule_parser import parse_rule
from app.ai.schemas import ParseRequest, generation_schema
from app.ai.validation import validated_rule
from app.rules.fields import BOUNDS, FIELDS, field_metadata
from app.rules.schemas import RuleTestRequest
from app.rules.validation import MAX_ERRORS, validate_tree
from test_ai_rules import draft, fake_client, leaf
from test_rules import request


class TreeValidationTests(unittest.TestCase):
    def assert_error(self, rule, code, path):
        errors = validate_tree(rule)
        self.assertIn((code, path), [(e["code"], e["path"]) for e in errors], errors)

    def test_nested_error_locations_and_multiple_errors(self):
        tree = {"logic": "AND", "rules": [leaf("unknown"), {"logic": "OR", "rules": [leaf("rsi14", ">", 101), leaf(value="100")]}]}
        errors = validate_tree(tree)
        self.assertEqual([(e["code"], e["path"]) for e in errors], [
            ("unsupported_field", "/rule/rules/0/field"),
            ("out_of_range", "/rule/rules/1/rules/0/value"),
            ("invalid_type", "/rule/rules/1/rules/1/value"),
        ])

    def test_each_bounded_field_and_range_endpoints(self):
        for name, (minimum, maximum) in BOUNDS.items():
            with self.subTest(name=name):
                self.assertEqual(validate_tree(leaf(name, ">=", minimum)), [])
                self.assert_error(leaf(name, ">=", minimum - 1), "out_of_range", "/rule/value")
                self.assert_error(leaf(name, "between", [minimum - 1, minimum]), "out_of_range", "/rule/value/0")
                if maximum is not None:
                    self.assertEqual(validate_tree(leaf(name, "between", [minimum, maximum])), [])
                    self.assert_error(leaf(name, "between", [minimum, maximum + 1]), "out_of_range", "/rule/value/1")

    def test_signed_indicators_remain_valid(self):
        for name in ("macd_dif", "macd_signal", "macd_histogram", "roc12", "change_percent", "obv", "bollinger_lower"):
            with self.subTest(name=name):
                self.assertEqual(validate_tree(leaf(name, "between", [-20, 0])), [])
                self.assertEqual(validated_rule(leaf(name, "<", -1)).value, -1)

    def test_strict_literal_types_no_coercion(self):
        for value in ("123", True, None, float("nan"), float("inf"), 10 ** 400, [], [1, 2]):
            with self.subTest(value=value):
                self.assert_error(leaf(value=value), "invalid_type", "/rule/value")
        for value in (0, 1, "true", None):
            self.assert_error(leaf("kd_golden_cross", "==", value), "invalid_type", "/rule/value")
        self.assert_error(leaf("trend", "==", "多頭"), "invalid_enum", "/rule/value")

    def test_field_references_and_operator_types(self):
        self.assertEqual(validate_tree(leaf("ma5", "cross_above", {"field": "ma20"})), [])
        self.assertEqual(validate_tree(leaf("kd_k", "cross_below", {"field": "kd_d"})), [])
        self.assert_error(leaf(value={"field": "invalid"}), "unsupported_field", "/rule/value/field")
        self.assert_error(leaf(value={"field": "trend"}), "incompatible_field", "/rule/value/field")
        self.assert_error(leaf(operator="between", value={"field": "ma20"}), "invalid_range", "/rule/value")
        self.assert_error(leaf(value={"field": "ma20", "sql": "sensitive"}), "extra_property", "/rule/value/sql")
        for rule in (leaf(operator="eval"), leaf(operator=[]), leaf("trend", "cross_above", "bullish"), leaf("kd_golden_cross", ">", True)):
            self.assert_error(rule, "unsupported_operator", "/rule/operator")

    def test_between_invalid_shape_order_and_boolean(self):
        for value in ([20, 10], [True, 10], [1], [1, 2, 3], "1,2", [0, float("inf")]):
            self.assert_error(leaf(operator="between", value=value), "invalid_range", "/rule/value")

    def test_group_shape_and_unknown_keys(self):
        self.assert_error({"logic": "XOR", "rules": [leaf()]}, "invalid_logic", "/rule/logic")
        for children in ([], None, {}):
            self.assert_error({"logic": "AND", "rules": children}, "invalid_children", "/rule/rules")
        self.assert_error({"logic": "AND", "rules": [leaf()], **leaf()}, "extra_property", "/rule/field")
        self.assert_error({"field": "price", "operator": ">"}, "missing_property", "/rule/value")
        self.assert_error({**leaf(), "a/b~c": 1}, "extra_property", "/rule/a~1b~0c")
        for node in (None, [], "rule", 1):
            self.assert_error(node, "invalid_node", "/rule")

    def test_exact_depth_and_node_limits(self):
        tree = leaf()
        for _ in range(9):
            tree = {"logic": "AND", "rules": [tree]}
        self.assertEqual(validate_tree(tree), [])
        self.assertIsNotNone(validated_rule(tree))
        too_deep = {"logic": "AND", "rules": [tree]}
        self.assertIn("too_deep", [e["code"] for e in validate_tree(too_deep)])
        self.assertEqual(validate_tree({"logic": "OR", "rules": [leaf()] * 99}), [])
        self.assert_error({"logic": "OR", "rules": [leaf()] * 100}, "too_many_nodes", "/rule/rules")

    def test_error_list_and_large_trees_are_bounded(self):
        errors = validate_tree({"logic": "AND", "rules": [leaf("unknown")] * 99})
        self.assertEqual(len(errors), MAX_ERRORS)
        self.assertEqual(len(validate_tree({"logic": "OR", "rules": [leaf()] * 10000})), 1)

    def test_legacy_rule_validation_uses_same_literal_bounds(self):
        for rule in (leaf(value=-1), leaf("volume_ratio_20d", ">", -1), leaf("atr14", "between", [-1, 2])):
            with self.assertRaises(ValidationError):
                RuleTestRequest.model_validate({"rule": rule})

    def test_discovery_and_generated_schema_share_constraints(self):
        metadata = {item["field"]: item for item in field_metadata()}
        self.assertEqual(set(metadata), set(FIELDS))
        self.assertEqual(metadata["rsi14"]["maximum"], 100)
        self.assertIsNone(metadata["macd_dif"]["minimum"])
        alternatives = generation_schema()["$defs"]["Rule"]["anyOf"]
        for name, kind in FIELDS.items():
            candidates = [a["properties"] for a in alternatives if name in a["properties"]["field"]["enum"]]
            self.assertEqual(len(candidates), 2 if kind == "number" else 1)
            literal = candidates[0]["value"]["anyOf"][0]
            self.assertEqual(literal["type"], kind)
            self.assertEqual(literal.get("minimum"), metadata[name]["minimum"])
            self.assertEqual(literal.get("maximum"), metadata[name]["maximum"])


class ValidationAPITests(unittest.TestCase):
    def test_validate_success_without_external_services(self):
        with patch("app.ai.ollama_client.OllamaClient.chat") as model, patch("app.api.rules.analysis") as analysis, patch("app.api.screener.screen") as screen:
            status, result = asyncio.run(request("POST", "/api/v1/ai/rules/validate", {"rule": leaf(value={"field": "ma20"})}))
        self.assertEqual(status, 200)
        self.assertEqual(result, {"valid": True, "rule": leaf(value={"field": "ma20"}), "requires_confirmation": True})
        model.assert_not_called()
        analysis.assert_not_called()
        screen.assert_not_called()

    def test_validate_failure_structured_422_without_raw_output(self):
        status, result = asyncio.run(request("POST", "/api/v1/ai/rules/validate", {"rule": leaf(value="secret-input")}))
        self.assertEqual(status, 422)
        self.assertEqual(result["detail"]["errors"][0]["path"], "/rule/value")
        self.assertNotIn("secret-input", json.dumps(result))
        self.assertNotIn("rule", result)

    def test_validate_null_missing_and_extra_envelope(self):
        for payload in ({}, {"rule": None}, {"rule": leaf(), "execute": True}):
            status, _ = asyncio.run(request("POST", "/api/v1/ai/rules/validate", payload))
            self.assertEqual(status, 422)

    def test_parse_errors_use_same_paths(self):
        with patch("app.ai.ollama_client.OllamaClient.chat", return_value=json.dumps(draft(leaf("rsi14", "between", [10, 101])))):
            status, result = asyncio.run(request("POST", "/api/v1/ai/rules/parse", {"text": "測試"}))
        self.assertEqual(status, 422)
        self.assertEqual(result["detail"]["errors"][0]["path"], "/rule/value/1")
        self.assertEqual(result["detail"]["errors"][0]["code"], "out_of_range")

    def test_draft_envelope_errors_and_quote_locations(self):
        cases = [(draft(assumptions="secret"), "/assumptions"),
                 (draft(assumptions=[{"source_text": "原文沒有", "interpreted_as": "股價100"}]), "/assumptions/0/source_text")]
        for output, path in cases:
            with self.assertRaises(AIError) as caught:
                parse_rule(ParseRequest(text="測試"), fake_client(output))
            self.assertEqual(caught.exception.errors[0]["path"], path)

    def test_invalid_partial_rule_is_not_silently_discarded(self):
        with self.assertRaises(AIError):
            parse_rule(ParseRequest(text="測試"), fake_client(draft(leaf("unknown"), unresolved_conditions=["不支援"])))


if __name__ == "__main__":
    unittest.main()
