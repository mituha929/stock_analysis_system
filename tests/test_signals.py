from datetime import datetime, timedelta, timezone
import unittest

from app.signals import analyze_technical
from app.signals.engine import _action_evaluation


def rising_candles(count: int = 90):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = []
    for index in range(count):
        close = 100 + index * 1.25
        volume = 1_000 + index * 10
        if index == count - 1:
            close += 5
            volume = 8_000
        result.append({"timestamp": start + timedelta(days=index), "open": close - 0.5, "high": close + 1, "low": close - 1, "close": close, "volume": volume})
    return result


def falling_candles(count: int = 90):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "timestamp": start + timedelta(days=index), "open": 220 - index * 1.1 + 0.5,
            "high": 220 - index * 1.1 + 1, "low": 220 - index * 1.1 - 1,
            "close": 220 - index * 1.1, "volume": 1_000 + index * 20,
        }
        for index in range(count)
    ]


class SignalEngineTests(unittest.TestCase):
    def test_bullish_breakout_generates_explainable_scores(self):
        analysis = analyze_technical(rising_candles())
        self.assertEqual(analysis["signals"]["ma_alignment"], "bullish_alignment")
        self.assertEqual(analysis["signals"]["structure"], "breakout")
        self.assertEqual(analysis["signals"]["volume_breakout"], "confirmed")
        self.assertGreaterEqual(analysis["scores"]["direction_score"], 65)
        self.assertIn(analysis["summary"]["agreement"], {"high", "medium"})
        self.assertTrue(analysis["reasons"])

    def test_scores_stay_in_documented_range(self):
        analysis = analyze_technical(rising_candles())
        for name, value in analysis["scores"].items():
            self.assertGreaterEqual(value, 0, name)
            self.assertLessEqual(value, 100, name)
        self.assertGreaterEqual(analysis["action_evaluation"]["action_score"], 0)
        self.assertLessEqual(analysis["action_evaluation"]["action_score"], 100)

    def test_action_evaluation_explains_warning_penalties(self):
        scores = {
            "trend": 100, "momentum": 100, "volume": 15, "structure": 65,
            "direction_score": 80, "risk_score": 0,
        }
        summary = {"agreement_count": 3}
        result = _action_evaluation(scores, summary, ["RSI 頂背離", "MACD 頂背離"])

        self.assertEqual(result["warning_penalty"], 20)
        self.assertEqual(result["agreement_adjustment"], 0)
        self.assertEqual(result["action_score"], 60)
        self.assertEqual(result["action_label"], "中性，暫無明確訊號")
        self.assertEqual(result["scoring_version"], "v1")

    def test_bearish_market_is_not_mislabeled_as_bullish(self):
        analysis = analyze_technical(falling_candles())
        self.assertEqual(analysis["signals"]["ma_alignment"], "bearish_alignment")
        self.assertEqual(analysis["signals"]["structure"], "breakdown")
        self.assertLess(analysis["scores"]["direction_score"], 45)
        self.assertIn(analysis["summary"]["direction"], {"bearish", "strong_bearish"})

    def test_requires_at_least_two_candles(self):
        with self.assertRaises(ValueError):
            analyze_technical(rising_candles(1))


if __name__ == "__main__":
    unittest.main()
