from datetime import datetime, timedelta, timezone
import unittest

from app.indicators.core import calculate_indicators, ema, normalize_candles, rsi, sma


def candles(count: int = 80):
    """Deterministic OHLCV data with rises and pullbacks for V2.1 validation."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = []
    for index in range(count):
        close = 100.0 + index * 0.6 + (index % 7 - 3) * 0.35
        data.append({
            "timestamp": start + timedelta(days=index),
            "open": close - 0.4,
            "high": close + 1.2,
            "low": close - 1.1,
            "close": close,
            "volume": 1_000 + index * 25,
        })
    return data


class IndicatorTests(unittest.TestCase):
    def test_moving_averages_use_sma_seeded_ema(self):
        self.assertEqual(sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        calculated = ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(calculated[:2], [None, None])
        self.assertAlmostEqual(calculated[2], 2.0)
        self.assertAlmostEqual(calculated[3], 3.0)
        self.assertAlmostEqual(calculated[4], 4.0)

    def test_rsi_of_a_continuous_rise_is_100(self):
        self.assertEqual(rsi([float(value) for value in range(1, 20)], 14)[14], 100.0)

    def test_full_v21_set_is_aligned_and_available_when_warmed_up(self):
        result = calculate_indicators(candles())
        self.assertEqual(len(result["candles"]), 80)
        self.assertEqual(result["ma"]["ma5"][:4], [None] * 4)
        self.assertIsNotNone(result["ema"]["ema12"][-1])
        self.assertIsNotNone(result["macd"]["histogram"][-1])
        self.assertIsNotNone(result["dmi_adx"]["adx"][-1])
        self.assertIsNotNone(result["kd"]["k"][-1])
        self.assertIsNotNone(result["rsi"]["rsi6"][-1])
        self.assertIsNotNone(result["rsi"]["rsi12"][-1])
        self.assertIsNotNone(result["rsi"]["rsi14"][-1])
        self.assertIsNotNone(result["roc"]["roc12"][-1])
        self.assertIsNotNone(result["bollinger"]["upper"][-1])
        self.assertIsNotNone(result["atr"]["atr14"][-1])
        self.assertIsNotNone(result["volume"]["ratio_20"][-1])
        self.assertGreater(result["volume"]["obv"][-1], 0)

    def test_normalization_sorts_and_rejects_invalid_ohlc(self):
        unordered = list(reversed(candles(2)))
        self.assertLess(normalize_candles(unordered)[0].timestamp, normalize_candles(unordered)[1].timestamp)
        invalid = candles(1)
        invalid[0]["low"] = invalid[0]["high"] + 1
        with self.assertRaises(ValueError):
            normalize_candles(invalid)


if __name__ == "__main__":
    unittest.main()
